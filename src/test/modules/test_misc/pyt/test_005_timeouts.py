# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test timeouts that will cause FATAL errors.

This test relies on injection points to await a timeout occurrence.  Relying
on sleep proved to be unstable on the buildfarm.  It's difficult to rely on
the NOTICE injection point because the backend under FATAL error can behave
differently.
"""

import os

import pytest


def test_005_timeouts(create_pg):
    # Skip unless this is an injection-points build.
    if os.environ.get("enable_injection_points", "no") != "yes":
        pytest.skip("Injection points not supported by this build")

    node = create_pg("master", start=False)
    node.start()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    if (
        node.safe_sql(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'injection_points'"
        )
        == "0"
    ):
        pytest.skip("Extension injection_points not installed")

    node.safe_sql("CREATE EXTENSION injection_points;")

    #
    # 1. Test of the transaction timeout
    #
    node.safe_sql("SELECT injection_points_attach('transaction-timeout', 'wait');")

    # A persistent backend that issues the blocking query.  The async query
    # never returns normally: the backend is FATAL'd by the timeout, so the
    # session is just closed afterwards without draining results.
    psql_session = node.connect()

    psql_session.do("SET transaction_timeout to '10ms';")

    psql_session.do_async(
        "BEGIN; DO ' begin loop PERFORM pg_sleep(0.001); end loop; end ';"
    )

    # Wait until the backend enters the timeout injection point.  Will raise
    # here if anything goes wrong.
    node.wait_for_event("client backend", "transaction-timeout")

    log_offset = node.log_position()

    # Remove the injection point.
    node.safe_sql("SELECT injection_points_wakeup('transaction-timeout');")

    # Check that the timeout was logged.
    node.wait_for_log("terminating connection due to transaction timeout", log_offset)

    psql_session.close()

    #
    # 2. Test of the idle in transaction timeout
    #
    node.safe_sql(
        "SELECT injection_points_attach("
        "'idle-in-transaction-session-timeout', 'wait');"
    )

    # We begin a transaction and then hang on the line.
    psql_session.reconnect()
    psql_session.do("SET idle_in_transaction_session_timeout to '10ms';\nBEGIN;\n")

    # Wait until the backend enters the timeout injection point.
    node.wait_for_event("client backend", "idle-in-transaction-session-timeout")

    log_offset = node.log_position()

    # Remove the injection point.
    node.safe_sql(
        "SELECT injection_points_wakeup('idle-in-transaction-session-timeout');"
    )

    # Check that the timeout was logged.
    node.wait_for_log(
        "terminating connection due to idle-in-transaction timeout", log_offset
    )

    psql_session.close()

    #
    # 3. Test of the idle session timeout
    #
    node.safe_sql("SELECT injection_points_attach('idle-session-timeout', 'wait');")

    # We just initialize the GUC and wait.  No transaction is required.
    psql_session.reconnect()
    psql_session.do("SET idle_session_timeout to '10ms';\n")

    # Wait until the backend enters the timeout injection point.
    node.wait_for_event("client backend", "idle-session-timeout")

    log_offset = node.log_position()

    # Remove the injection point.
    node.safe_sql("SELECT injection_points_wakeup('idle-session-timeout');")

    # Check that the timeout was logged.
    node.wait_for_log("terminating connection due to idle-session timeout", log_offset)

    psql_session.close()
