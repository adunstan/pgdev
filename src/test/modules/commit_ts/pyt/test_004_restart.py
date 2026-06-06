# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test preservation of commit timestamps across restarts."""

import re


def test_004_restart(create_pg):
    node_primary = create_pg("primary", start=False, allows_streaming=True)
    node_primary.append_conf("track_commit_timestamp = on")
    node_primary.start()

    res = node_primary.sql("SELECT pg_xact_commit_timestamp('0');")
    assert res.error_message is not None, \
        "getting ts of InvalidTransactionId reports error"
    assert re.search(r"cannot retrieve commit timestamp for transaction",
                     res.error_message), \
        "expected error from InvalidTransactionId"

    res = node_primary.sql("SELECT pg_xact_commit_timestamp('1');")
    assert res.error_message is None, \
        "getting ts of BootstrapTransactionId succeeds"
    assert res.psqlout == "", "timestamp of BootstrapTransactionId is null"

    res = node_primary.sql("SELECT pg_xact_commit_timestamp('2');")
    assert res.error_message is None, \
        "getting ts of FrozenTransactionId succeeds"
    assert res.psqlout == "", "timestamp of FrozenTransactionId is null"

    # Since FirstNormalTransactionId will've occurred during initdb, long before
    # we enabled commit timestamps, it'll be null since we have no cts data for
    # it but cts are enabled.
    assert node_primary.safe_sql("SELECT pg_xact_commit_timestamp('3');") \
        == "", "committs for FirstNormalTransactionId is null"

    node_primary.safe_sql(
        "CREATE TABLE committs_test(x integer, y timestamp with time zone);")

    xid = node_primary.safe_sql("""
        BEGIN;
        INSERT INTO committs_test(x, y) VALUES (1, current_timestamp);
        SELECT pg_current_xact_id()::xid;
        COMMIT;
    """)

    before_restart_ts = node_primary.safe_sql(
        f"SELECT pg_xact_commit_timestamp('{xid}');")
    assert before_restart_ts != "" and before_restart_ts != "null", \
        "commit timestamp recorded"

    node_primary.stop("immediate")
    node_primary.start()

    after_crash_ts = node_primary.safe_sql(
        f"SELECT pg_xact_commit_timestamp('{xid}');")
    assert after_crash_ts == before_restart_ts, \
        "timestamps before and after crash are equal"

    node_primary.stop("fast")
    node_primary.start()

    after_restart_ts = node_primary.safe_sql(
        f"SELECT pg_xact_commit_timestamp('{xid}');")
    assert after_restart_ts == before_restart_ts, \
        "timestamps before and after restart are equal"

    # Now disable commit timestamps
    node_primary.append_conf("track_commit_timestamp = off")
    node_primary.stop("fast")

    # Start the server, which generates a XLOG_PARAMETER_CHANGE record where
    # the parameter change is registered.
    node_primary.start()

    # Now restart again the server so as no XLOG_PARAMETER_CHANGE record are
    # replayed with the follow-up immediate shutdown.
    node_primary.restart()

    # Move commit timestamps across page boundaries.  Things should still
    # be able to work across restarts with those transactions committed while
    # track_commit_timestamp is disabled.
    node_primary.safe_sql("""CREATE PROCEDURE consume_xid(cnt int)
AS $$
DECLARE
    i int;
    BEGIN
        FOR i in 1..cnt LOOP
            EXECUTE 'SELECT pg_current_xact_id()';
            COMMIT;
        END LOOP;
    END;
$$
LANGUAGE plpgsql;
""")
    node_primary.safe_sql("CALL consume_xid(2000)")

    res = node_primary.sql(f"SELECT pg_xact_commit_timestamp('{xid}');")
    assert res.error_message is not None, \
        "no commit timestamp from enable tx when cts disabled"
    assert re.search(r"could not get commit timestamp data",
                     res.error_message), \
        "expected error from enabled tx when committs disabled"

    # Do a tx while cts disabled
    xid_disabled = node_primary.safe_sql("""
        BEGIN;
        INSERT INTO committs_test(x, y) VALUES (2, current_timestamp);
        SELECT pg_current_xact_id();
        COMMIT;
    """)

    # Should be inaccessible
    res = node_primary.sql(
        f"SELECT pg_xact_commit_timestamp('{xid_disabled}');")
    assert res.error_message is not None, "no commit timestamp when disabled"
    assert re.search(r"could not get commit timestamp data",
                     res.error_message), \
        "expected error from disabled tx when committs disabled"

    # Re-enable, restart and ensure we can still get the old timestamps
    node_primary.append_conf("track_commit_timestamp = on")

    # An immediate shutdown is used here.  At next startup recovery will
    # replay transactions which committed when track_commit_timestamp was
    # disabled, and the facility should be able to work properly.
    node_primary.stop("immediate")
    node_primary.start()

    after_enable_ts = node_primary.safe_sql(
        f"SELECT pg_xact_commit_timestamp('{xid}');")
    assert after_enable_ts == "", "timestamp of enabled tx null after re-enable"

    after_enable_disabled_ts = node_primary.safe_sql(
        f"SELECT pg_xact_commit_timestamp('{xid_disabled}');")
    assert after_enable_disabled_ts == "", \
        "timestamp of disabled tx null after re-enable"

    node_primary.stop()
