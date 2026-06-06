# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test postmaster start and stop state machine."""

import os
import socket
import struct

from libpq.errors import ConnectionError as PqConnectionError
from pypg.util import TIMEOUT_DEFAULT


def _raw_connect(node):
    """Open a raw socket to the server's unix socket.

    For the unix-socket-only framework, connects directly to
    ``<host>/.s.PGSQL.<port>``.
    """
    path = os.path.join(node.host, f".s.PGSQL.{node.port}")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    return sock


def test_003_start_stop(create_pg):
    #
    # Test that dead-end backends don't prevent the server from shutting
    # down.
    #
    # Dead-end backends can linger until they reach authentication_timeout.
    # We use a long authentication_timeout and a much shorter timeout for the
    # "pg_ctl stop" operation, to test that if dead-end backends are killed at
    # fast shut down. If they're not, "pg_ctl stop" will error out before the
    # authentication timeout kicks in and cleans up the dead-end backends.
    authentication_timeout = TIMEOUT_DEFAULT

    # Don't fail due to hitting the max value allowed for authentication_timeout.
    if not authentication_timeout < 600:
        authentication_timeout = 600

    stop_timeout = authentication_timeout // 2

    # Initialize the server with low connection limits, to test dead-end
    # backends.
    node = create_pg("main", start=False)
    node.append_conf("max_connections = 5")
    node.append_conf("max_wal_senders = 0")
    node.append_conf("autovacuum_max_workers = 1")
    node.append_conf("max_worker_processes = 1")
    node.append_conf(
        "log_connections = 'receipt,authentication,authorization'"
    )
    node.append_conf("log_min_messages = debug2")
    node.append_conf(f"authentication_timeout = '{authentication_timeout} s'")
    node.append_conf("trace_connection_negotiation=on")
    node.start()

    raw_connections = []

    # Open a lot of TCP (or Unix domain socket) connections to use up all
    # the connection slots. Beyond a certain number (roughly 2x
    # max_connections), they will be "dead-end backends".
    for i in range(0, 21):
        sock = _raw_connect(node)

        # On a busy system, the server might reject connections if postmaster
        # cannot accept() them fast enough. The exact limit and behavior
        # depends on the platform. To make this reliable, we attempt SSL
        # negotiation on each connection before opening next one. The server
        # will reject the SSL negotiations, but when it does so, we know that
        # the backend has been launched and we should be able to open another
        # connection.

        # SSLRequest packet consists of packet length followed by
        # NEGOTIATE_SSL_CODE.
        negotiate_ssl_code = struct.pack("!Ihh", 8, 1234, 5679)
        sock.send(negotiate_ssl_code)

        # Read reply. We expect the server to reject it with 'N'
        reply = sock.recv(1)
        assert reply == b"N", f"dead-end connection {i}"

        raw_connections.append(sock)

    # When all the connection slots are in use, new connections will fail
    # before even looking up the user. Hence you now get "sorry, too many
    # clients already" instead of "role does not exist" error. Test that to
    # ensure that we have used up all the slots.
    try:
        node.connect("postgres", user="invalid_user").close()
        raised = None
    except PqConnectionError as exc:
        raised = str(exc)
    assert raised is not None and "sorry, too many clients already" in raised, (
        "connection is rejected when all slots are in use"
    )

    # Open one more connection, to really ensure that we have at least one
    # dead-end backend.
    sock = _raw_connect(node)

    # Test that the dead-end backends don't prevent the server from stopping.
    # Use pg_ctl directly so a short stop timeout can be enforced.
    node._close_sessions()
    node.pg_bin.command_ok(
        ["pg_ctl", "-D", node.data_dir, "-m", "fast", "-w",
         "-t", str(stop_timeout), "stop"],
        "fast stop with dead-end backends",
    )
    node._running = False

    node.start()
    node.connect("postgres").close()  # works after restart

    # Clean up
    for s in raw_connections:
        s.close()
    sock.close()

    node.stop()
