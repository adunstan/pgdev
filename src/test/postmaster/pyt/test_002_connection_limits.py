# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test connection limits, i.e. max_connections, reserved_connections and
superuser_reserved_connections.
"""

import re
import struct

import pytest

from libpq.errors import ConnectionError as PqConnectionError


def _session_as_user(node, user):
    """Open a fresh session as *user* (held open to occupy a slot)."""
    return node.connect("postgres", user=user)


def _connect_fails_wait(node, user, test_name, expected_stderr):
    """Like connect_fails(), except that we also wait for the failed backend
    to have exited.

    This test needs to wait for client processes to exit because the error
    message for a failed connection is reported before the backend has
    detached from shared memory. If we didn't wait, subsequent tests might hit
    connection limits spuriously.
    """
    log_location = node.log_position()

    try:
        node.connect("postgres", user=user).close()
        raised = None
    except PqConnectionError as exc:
        raised = str(exc)
    assert raised is not None and re.search(expected_stderr, raised), (
        f"{test_name}\nexpected /{expected_stderr}/, got: {raised!r}"
    )

    node.wait_for_log(
        r"DEBUG:  (00000: )?client backend.*exited with exit code 1",
        log_location,
    )
    # "$test_name: client backend process exited"


def test_002_connection_limits(create_pg):
    # Initialize the server with specific low connection limits.  With the
    # framework's "-A trust" initdb every local role is trusted, so no extra
    # pg_hba entries are needed for the three regress_* roles.
    node = create_pg("primary", start=False)
    node.append_conf("max_connections = 6")
    node.append_conf("reserved_connections = 2")
    node.append_conf("superuser_reserved_connections = 1")
    node.append_conf(
        "log_connections = 'receipt,authentication,authorization'"
    )
    node.append_conf("log_min_messages=debug2")
    node.start()

    if not node.raw_connect_works():
        pytest.skip("this test requires working raw_connect()")

    node.safe_sql("CREATE USER regress_regular LOGIN")
    node.safe_sql("CREATE USER regress_reserved LOGIN")
    node.safe_sql("GRANT pg_use_reserved_connections TO regress_reserved")
    node.safe_sql("CREATE USER regress_superuser LOGIN SUPERUSER")

    # With the limits we set in postgresql.conf, we can establish:
    # - 3 connections for any user with no special privileges
    # - 2 more connections for users belonging to "pg_use_reserved_connections"
    # - 1 more connection for superuser

    # Restart the server to ensure that any backends launched for the
    # initialization steps are gone. Otherwise they could still be using up
    # connection slots and mess with our expectations.
    node.restart()

    sessions = []
    raw_connections = []

    sessions.append(_session_as_user(node, "regress_regular"))
    sessions.append(_session_as_user(node, "regress_regular"))
    sessions.append(_session_as_user(node, "regress_regular"))
    _connect_fails_wait(
        node,
        "regress_regular",
        "regular connections limit",
        expected_stderr=(
            r'FATAL:  remaining connection slots are reserved for roles '
            r'with privileges of the "pg_use_reserved_connections" role'
        ),
    )

    sessions.append(_session_as_user(node, "regress_reserved"))
    sessions.append(_session_as_user(node, "regress_reserved"))
    _connect_fails_wait(
        node,
        "regress_reserved",
        "reserved_connections limit",
        expected_stderr=(
            r"FATAL:  remaining connection slots are reserved for roles "
            r"with the SUPERUSER attribute"
        ),
    )

    sessions.append(_session_as_user(node, "regress_superuser"))
    _connect_fails_wait(
        node,
        "regress_superuser",
        "superuser_reserved_connections limit",
        expected_stderr=r"FATAL:  sorry, too many clients already",
    )

    # We can still open TCP (or Unix domain socket) connections, but beyond a
    # certain number (roughly 2x max_connections), they will be "dead-end
    # backends".
    for i in range(0, 21):
        sock = node.raw_connect()

        # On a busy system, the server might reject connections if postmaster
        # cannot accept() them fast enough. To make this reliable, we attempt
        # SSL negotiation on each connection before opening the next one. The
        # server will reject the SSL negotiations, but when it does so, we
        # know that the backend has been launched and we should be able to
        # open another connection.

        # SSLRequest packet consists of packet length followed by
        # NEGOTIATE_SSL_CODE.
        negotiate_ssl_code = struct.pack("!Ihh", 8, 1234, 5679)
        sock.send(negotiate_ssl_code)

        # Read reply. We expect the server to reject it with 'N'
        reply = sock.recv(1)
        assert reply == b"N", f"dead-end connection {i}"

        raw_connections.append(sock)

    # TODO: test that query cancellation is still possible. A dead-end backend
    # can process a query cancellation packet.

    # Clean up
    for session in sessions:
        session.close()
    for sock in raw_connections:
        sock.close()

    node.stop()
