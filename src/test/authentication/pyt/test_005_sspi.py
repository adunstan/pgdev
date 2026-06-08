# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests targeting SSPI on Windows.

These tests require Windows with TCP, so the module is skipped whenever the
framework uses Unix-domain sockets -- i.e. on every non-Windows host, and on
Windows when PG_TEST_USE_UNIX_SOCKETS is set.  That leaves it running only on
Windows over TCP, matching the Perl test's ``!$windows_os || $use_unix_sockets``
skip condition.
"""

import pytest

from pypg.util import USE_UNIX_SOCKETS

pytestmark = pytest.mark.skipif(
    USE_UNIX_SOCKETS,
    reason="SSPI tests require Windows (without PG_TEST_USE_UNIX_SOCKETS)",
)


def test_005_sspi(create_pg):
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
