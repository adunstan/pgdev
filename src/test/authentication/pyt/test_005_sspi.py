# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests targeting SSPI on Windows.

These tests require Windows (without PG_TEST_USE_UNIX_SOCKETS).  This framework
is always Unix-socket-only and this host is not Windows, so the test skips
cleanly here.  The test body below the skip is included for completeness.
"""

import sys

import pytest


def test_005_sspi(create_pg):
    # SSPI tests require Windows (without PG_TEST_USE_UNIX_SOCKETS).
    if sys.platform != "win32":
        pytest.skip(
            "SSPI tests require Windows (without PG_TEST_USE_UNIX_SOCKETS)"
        )

    # Initialize primary node
    node = create_pg("primary", start=False)
    node.append_conf("log_connections = authentication\n")
    node.start()

    huge_pages_status = node.safe_sql("SHOW huge_pages_status;")
    assert huge_pages_status != "unknown", "check huge_pages_status"

    # SSPI is set up by default.  Make sure it interacts correctly with
    # require_auth.
    node.connect_ok(
        "require_auth=sspi",
        "SSPI authentication required, works with SSPI auth",
    )
    node.connect_fails(
        "require_auth=!sspi",
        "SSPI authentication forbidden, fails with SSPI auth",
        expected_stderr=r'authentication method requirement "!sspi" failed: server requested SSPI authentication',
    )
    node.connect_fails(
        "require_auth=scram-sha-256",
        "SCRAM authentication required, fails with SSPI auth",
        expected_stderr=r'authentication method requirement "scram-sha-256" failed: server requested SSPI authentication',
    )
