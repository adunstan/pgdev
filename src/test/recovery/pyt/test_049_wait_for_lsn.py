# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Checks waiting for the LSN using the WAIT FOR command.  Tests standby modes
(standby_replay/standby_write/standby_flush) on the standby and primary_flush
mode on the primary.

All SQL runs in-process through libpq Sessions (no psql subprocess).  Backends
that block in a WAIT/replay-wait call use a dedicated node.connect() session and
the async API (do_async/get_async_result/wait_for_completion).
"""

import re


# Saved primary_conninfo across stop_walreceiver()/resume_walreceiver().
_saved_primary_conninfo = None


def _stop_walreceiver(node):
    """Stop the walreceiver on *node* by clearing primary_conninfo.

    Waits until pg_stat_wal_receiver becomes empty.  Used to freeze the
    walreceiver-tracked positions (writtenUpto, flushedUpto) so a fencepost
    test can rely on them not advancing.  The previous value is saved for
    _resume_walreceiver().
    """
    global _saved_primary_conninfo
    _saved_primary_conninfo = node.safe_sql(
        "SELECT pg_catalog.quote_literal(setting) "
        "FROM pg_settings WHERE name = 'primary_conninfo';"
    )
    node.safe_sql("ALTER SYSTEM SET primary_conninfo = '';")
    node.safe_sql("SELECT pg_reload_conf();")
    assert node.poll_query_until(
        "SELECT NOT EXISTS (SELECT * FROM pg_stat_wal_receiver);"
    )


def _resume_walreceiver(node):
    """Restart the walreceiver on *node* by restoring primary_conninfo.

    Restores the value captured by _stop_walreceiver() and waits until the
    walreceiver reconnects.  Must be paired with a prior _stop_walreceiver().
    """
    node.safe_sql(
        f"ALTER SYSTEM SET primary_conninfo = {_saved_primary_conninfo};")
    node.safe_sql("SELECT pg_reload_conf();")
    assert node.poll_query_until(
        "SELECT EXISTS (SELECT * FROM pg_stat_wal_receiver);"
    )


def _check_wait_for_lsn_fencepost(node, mode, current_lsn, label):
    """Verify the wait predicate "target <= currentLSN" at the boundary.

    Given *current_lsn* (the frozen position for *mode*), check that:
      target == current        -> success (predicate is <=)
      target == current - 1    -> success
      target == current + 1    -> timeout
    Returns (lsn_minus, lsn_plus) so the caller can reuse them.
    """
    lsn_minus = node.safe_sql(f"SELECT ('{current_lsn}'::pg_lsn - 1)::text")
    lsn_plus = node.safe_sql(f"SELECT ('{current_lsn}'::pg_lsn + 1)::text")

    for target_lsn, expected, desc, timeout in (
        (current_lsn, "success", "target == current succeeds", "5s"),
        (lsn_minus, "success", "target == current - 1 succeeds", "5s"),
        (lsn_plus, "timeout", "target == current + 1 times out", "500ms"),
    ):
        output = node.safe_sql(
            f"WAIT FOR LSN '{target_lsn}' "
            f"WITH (MODE '{mode}', timeout '{timeout}', no_throw);"
        )
        assert output == expected, f"{label}: {desc}"

    return lsn_minus, lsn_plus


def test_049_wait_for_lsn(create_pg):
    # Initialize primary node
    node_primary = create_pg("primary", allows_streaming=True)

    # And some content and take a backup
    node_primary.safe_sql(
        "CREATE TABLE wait_test AS SELECT generate_series(1,10) AS a")
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Create a streaming standby with a 1 second delay from the backup
    node_standby = create_pg("standby", start=False)
    delay = 1
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.append_conf(f"""
recovery_min_apply_delay = '{delay}s'
""")
    node_standby.start()

    # 1. Make sure that WAIT FOR works: add new content to primary and memorize
    # primary's insert LSN, then wait for that LSN to be replayed on standby.
    node_primary.safe_sql(
        "INSERT INTO wait_test VALUES (generate_series(11, 20))")
    lsn1 = node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()")
    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn1}' WITH (timeout '1d');\n"
        f"SELECT pg_lsn_cmp(pg_last_wal_replay_lsn(), '{lsn1}'::pg_lsn);")

    # Make sure the current LSN on standby is at least as big as the LSN we
    # observed on primary's before.
    assert int(output.split("\n")[-1]) >= 0, \
        "standby reached the same LSN as primary after WAIT FOR"

    # 2. Check that new data is visible after calling WAIT FOR
    node_primary.safe_sql(
        "INSERT INTO wait_test VALUES (generate_series(21, 30))")
    lsn2 = node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()")
    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn2}';\n"
        "SELECT count(*) FROM wait_test;")

    # Make sure the count(*) on standby reflects the recent changes on primary
    assert output.split("\n")[-1] == "30", \
        "standby reached the same LSN as primary"

    # 3. Check that WAIT FOR works with standby_write, standby_flush, and
    # primary_flush modes.
    node_primary.safe_sql(
        "INSERT INTO wait_test VALUES (generate_series(31, 40))")
    lsn_write = node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()")
    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn_write}' WITH (MODE 'standby_write', timeout '1d');\n"
        "SELECT pg_lsn_cmp((SELECT written_lsn FROM pg_stat_wal_receiver), "
        f"'{lsn_write}'::pg_lsn);")
    assert int(output.split("\n")[-1]) >= 0, \
        "standby wrote WAL up to target LSN after WAIT FOR with MODE 'standby_write'"

    node_primary.safe_sql(
        "INSERT INTO wait_test VALUES (generate_series(41, 50))")
    lsn_flush = node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()")
    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn_flush}' WITH (MODE 'standby_flush', timeout '1d');\n"
        f"SELECT pg_lsn_cmp(pg_last_wal_receive_lsn(), '{lsn_flush}'::pg_lsn);")
    assert int(output.split("\n")[-1]) >= 0, \
        "standby flushed WAL up to target LSN after WAIT FOR with MODE 'standby_flush'"

    # Check primary_flush mode on primary
    node_primary.safe_sql(
        "INSERT INTO wait_test VALUES (generate_series(51, 60))")
    lsn_primary_flush = node_primary.safe_sql(
        "SELECT pg_current_wal_insert_lsn()")
    output = node_primary.safe_sql(
        f"WAIT FOR LSN '{lsn_primary_flush}' "
        "WITH (MODE 'primary_flush', timeout '1d');\n"
        "SELECT pg_lsn_cmp(pg_current_wal_flush_lsn(), "
        f"'{lsn_primary_flush}'::pg_lsn);")
    assert int(output.split("\n")[-1]) >= 0, \
        "primary flushed WAL up to target LSN after WAIT FOR with MODE 'primary_flush'"

    # 4. Check that waiting for unreachable LSN triggers the timeout.  The
    # unreachable LSN must be well in advance.  So WAL records issued by the
    # concurrent autovacuum could not affect that.
    lsn3 = node_primary.safe_sql(
        "SELECT pg_current_wal_insert_lsn() + 10000000000")
    node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn2}' WITH (timeout '10ms');")
    res = node_standby.sql(
        f"WAIT FOR LSN '{lsn3}' WITH (timeout '1000ms');")
    assert res.error_message is not None
    assert re.search(r"timed out while waiting for target LSN",
                     res.error_message), \
        "get timeout on waiting for unreachable LSN"

    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn2}' WITH (timeout '0.1s', no_throw);")
    assert output == "success", \
        "WAIT FOR returns correct status after successful waiting"
    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn3}' WITH (timeout '10ms', no_throw);")
    assert output == "timeout", "WAIT FOR returns correct status after timeout"

    # 4a. Check that aborting a subtransaction during WAIT FOR LSN cleans up the
    # shared wait-state.  Poll pg_stat_activity before canceling the first WAIT
    # FOR to ensure that the backend has registered itself in the waiters heap.
    # After rolling back to the savepoint, a second WAIT FOR in the same backend
    # must be able to register itself again.
    subxact_lsn = node_primary.safe_sql(
        "SELECT pg_current_wal_insert_lsn() + 10000000000")
    subxact_appname = "wait_for_lsn_subxact_cleanup"
    subxact_session = node_primary.connect("postgres")
    try:
        # Send the setup statements individually so the first WAIT FOR LSN can
        # be issued asynchronously: it blocks (the target LSN is unreachable)
        # and will be canceled below.
        subxact_session.do(f"SET application_name = '{subxact_appname}'")
        subxact_session.do("BEGIN")
        subxact_session.do("SAVEPOINT wait_cleanup")
        subxact_session.do_async(
            f"WAIT FOR LSN '{subxact_lsn}' WITH (MODE 'primary_flush')")
        assert node_primary.poll_query_until(
            "SELECT count(*) = 1 FROM pg_stat_activity "
            f"WHERE application_name = '{subxact_appname}' "
            "AND wait_event = 'WaitForWalFlush'"
        ), "WAIT FOR LSN did not enter the primary_flush wait path"
        subxact_cancelled = node_primary.safe_sql(
            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
            f"WHERE application_name = '{subxact_appname}' "
            "AND wait_event = 'WaitForWalFlush'")
        assert subxact_cancelled == "t", \
            "canceled WAIT FOR LSN in subtransaction"

        # The cancel interrupts the blocking WAIT FOR LSN, leaving the
        # transaction in an aborted state.
        subxact_cancel_res = subxact_session.get_async_result()
        assert subxact_cancel_res.error_message is not None
        assert re.search(r"canceling statement due to user request",
                         subxact_cancel_res.error_message), \
            "query cancel interrupted WAIT FOR LSN in subtransaction"

        # Roll back to the savepoint so a second WAIT FOR LSN can register again
        # in the same backend; with no_throw it returns 'timeout' rather than
        # erroring.
        subxact_session.do("ROLLBACK TO wait_cleanup")
        subxact_timeout = subxact_session.query_oneval(
            f"WAIT FOR LSN '{subxact_lsn}' "
            "WITH (MODE 'primary_flush', timeout '10ms', no_throw)")
        assert subxact_timeout == "timeout", \
            "second WAIT FOR LSN timed out after savepoint rollback"

        # The backend survived the cancel without disconnecting: the connection
        # is still usable.
        assert subxact_session.query_oneval("SELECT 1") == "1", \
            "WAIT FOR LSN after savepoint rollback did not disconnect"
        subxact_session.do("COMMIT")
    finally:
        subxact_session.close()

    # 5. Check mode validation: standby modes error on primary, primary mode
    # errors on standby, and primary_flush works on primary.  Also check that
    # WAIT FOR triggers an error if called within a function, procedure,
    # anonymous DO block, or inside a transaction with an isolation level higher
    # than READ COMMITTED.

    # Test standby_flush on primary - should error
    res = node_primary.sql(
        f"WAIT FOR LSN '{lsn3}' WITH (MODE 'standby_flush');")
    assert res.error_message and re.search(
        r"recovery is not in progress", res.error_message), \
        "get an error when running standby_flush on the primary"

    # Test primary_flush on standby - should error
    res = node_standby.sql(
        f"WAIT FOR LSN '{lsn3}' WITH (MODE 'primary_flush');")
    assert res.error_message and re.search(
        r"recovery is in progress", res.error_message), \
        "get an error when running primary_flush on the standby"

    res = node_standby.sql(
        "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT 1; "
        f"WAIT FOR LSN '{lsn3}';")
    assert res.error_message and re.search(
        r"WAIT FOR must be called without an active or registered snapshot",
        res.error_message), \
        "get an error when running in a transaction with an isolation level higher than REPEATABLE READ"

    # Test wrapping WAIT FOR into function, procedure, and anonymous DO block --
    # should error
    node_primary.safe_sql("""
CREATE FUNCTION pg_wal_replay_wait_wrap(target_lsn pg_lsn) RETURNS void AS $$
  BEGIN
    EXECUTE format('WAIT FOR LSN %L;', target_lsn);
  END
$$
LANGUAGE plpgsql;

CREATE PROCEDURE pg_wal_replay_wait_proc(target_lsn pg_lsn) AS $$
  BEGIN
    EXECUTE format('WAIT FOR LSN %L;', target_lsn);
  END
$$
LANGUAGE plpgsql;
""")

    node_primary.wait_for_catchup(node_standby)
    res = node_standby.sql(f"SELECT pg_wal_replay_wait_wrap('{lsn3}');")
    assert res.error_message and re.search(
        r"WAIT FOR can only be executed as a top-level statement",
        res.error_message), \
        "get an error when running within a function"

    res = node_standby.sql(f"CALL pg_wal_replay_wait_proc('{lsn3}');")
    assert res.error_message and re.search(
        r"WAIT FOR can only be executed as a top-level statement",
        res.error_message), \
        "get an error when running within a procedure"

    res = node_standby.sql(
        f"DO $$ BEGIN EXECUTE format('WAIT FOR LSN %L;', '{lsn3}'); END $$;")
    assert res.error_message and re.search(
        r"WAIT FOR can only be executed as a top-level statement",
        res.error_message), \
        "get an error when running within a DO block"

    # 6. Check parameter validation error cases on standby before promotion
    test_lsn = node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()")

    # Test negative timeout
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' WITH (timeout '-1000ms');")
    assert res.error_message and re.search(
        r"timeout cannot be negative", res.error_message), \
        "get error for negative timeout"

    # Test unknown parameter with WITH clause
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' WITH (unknown_param 'value');")
    assert res.error_message and re.search(
        r'option "unknown_param" not recognized', res.error_message), \
        "get error for unknown parameter"

    # Test duplicate TIMEOUT parameter with WITH clause
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' WITH (timeout '1000', timeout '2000');")
    assert res.error_message and re.search(
        r"conflicting or redundant options", res.error_message), \
        "get error for duplicate TIMEOUT parameter"

    # Test duplicate NO_THROW parameter with WITH clause
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' WITH (no_throw, no_throw);")
    assert res.error_message and re.search(
        r"conflicting or redundant options", res.error_message), \
        "get error for duplicate NO_THROW parameter"

    # Test syntax error - options without WITH keyword
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' (timeout '100ms');")
    assert res.error_message and re.search(
        r"syntax error", res.error_message), \
        "get syntax error when options specified without WITH keyword"

    # Test syntax error - missing LSN
    res = node_standby.sql("WAIT FOR TIMEOUT 1000;")
    assert res.error_message and re.search(
        r"syntax error", res.error_message), \
        "get syntax error for missing LSN"

    # Test invalid LSN format
    res = node_standby.sql("WAIT FOR LSN 'invalid_lsn';")
    assert res.error_message and re.search(
        r"invalid input syntax for type pg_lsn", res.error_message), \
        "get error for invalid LSN format"

    # Test invalid timeout format
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' WITH (timeout 'invalid');")
    assert res.error_message and re.search(
        r"invalid timeout value", res.error_message), \
        "get error for invalid timeout format"

    # Test new WITH clause syntax
    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn2}' WITH (timeout '0.1s', no_throw);")
    assert output == "success", "WAIT FOR WITH clause syntax works correctly"

    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn3}' WITH (timeout 100, no_throw);")
    assert output == "timeout", \
        "WAIT FOR WITH clause returns correct timeout status"

    # Test WITH clause error case - invalid option
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' WITH (invalid_option 'value');")
    assert res.error_message and re.search(
        r'option "invalid_option" not recognized', res.error_message), \
        "get error for invalid WITH clause option"

    # Test invalid MODE value
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' WITH (MODE 'invalid');")
    assert res.error_message and re.search(
        r'unrecognized value for WAIT option "mode": "invalid"',
        res.error_message), \
        "get error for invalid MODE value"

    # Test duplicate MODE parameter
    res = node_standby.sql(
        f"WAIT FOR LSN '{test_lsn}' "
        "WITH (MODE 'standby_replay', MODE 'standby_write');")
    assert res.error_message and re.search(
        r"conflicting or redundant options", res.error_message), \
        "get error for duplicate MODE parameter"

    # 7a. Check the scenario of multiple standby_replay waiters.  We make 5
    # background sessions each waiting for a corresponding insertion.  When
    # waiting is finished, stored procedures log if there are as many visible
    # rows as should be.
    node_primary.safe_sql("""
CREATE FUNCTION log_count(i int) RETURNS void AS $$
  DECLARE
    count int;
  BEGIN
    SELECT count(*) FROM wait_test INTO count;
    IF count >= 31 + i THEN
      RAISE LOG 'count %', i;
    END IF;
  END
$$
LANGUAGE plpgsql;

CREATE FUNCTION log_wait_done(prefix text, i int) RETURNS void AS $$
  BEGIN
    RAISE LOG '% %', prefix, i;
  END
$$
LANGUAGE plpgsql;
""")

    node_standby.safe_sql("SELECT pg_wal_replay_pause();")

    psql_sessions = []
    for i in range(5):
        node_primary.safe_sql(f"INSERT INTO wait_test VALUES ({i});")
        lsn = node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()")
        sess = node_standby.connect("postgres")
        psql_sessions.append(sess)
        sess.do_async(
            f"WAIT FOR LSN '{lsn}';\n"
            f"SELECT log_count({i});")

    log_offset = node_standby.log_position()
    node_standby.safe_sql("SELECT pg_wal_replay_resume();")
    for i in range(5):
        node_standby.wait_for_log(f"count {i}", log_offset)
        psql_sessions[i].wait_for_completion()
        psql_sessions[i].close()

    # multiple standby_replay waiters reported consistent data

    # 7b. Check the scenario of multiple standby_write waiters.
    # Stop walreceiver to ensure waiters actually block.
    _stop_walreceiver(node_standby)

    # Generate WAL on primary (standby won't receive it yet)
    write_lsns = []
    for i in range(5):
        node_primary.safe_sql(f"INSERT INTO wait_test VALUES (100 + {i});")
        write_lsns.append(
            node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()"))

    # Start standby_write waiters (they will block since walreceiver is stopped)
    write_sessions = []
    for i in range(5):
        sess = node_standby.connect("postgres")
        write_sessions.append(sess)
        sess.do_async(
            f"WAIT FOR LSN '{write_lsns[i]}' "
            "WITH (MODE 'standby_write', timeout '1d');\n"
            f"SELECT log_wait_done('write_done', {i});")

    # Verify waiters are blocked
    assert node_standby.poll_query_until(
        "SELECT count(*) = 5 FROM pg_stat_activity "
        "WHERE wait_event = 'WaitForWalWrite'")

    # Restore walreceiver to unblock waiters
    write_log_offset = node_standby.log_position()
    _resume_walreceiver(node_standby)

    # Wait for all waiters to complete and close sessions
    for i in range(5):
        node_standby.wait_for_log(f"write_done {i}", write_log_offset)
        write_sessions[i].wait_for_completion()
        write_sessions[i].close()

    # Verify on standby that WAL was written up to the target LSN
    output = node_standby.safe_sql(
        "SELECT pg_lsn_cmp((SELECT written_lsn FROM pg_stat_wal_receiver), "
        f"'{write_lsns[4]}'::pg_lsn);")
    assert int(output) >= 0, \
        "multiple standby_write waiters: standby wrote WAL up to target LSN"

    # 7c. Check the scenario of multiple standby_flush waiters.
    # Stop walreceiver to ensure waiters actually block.
    _stop_walreceiver(node_standby)

    # Generate WAL on primary (standby won't receive it yet)
    flush_lsns = []
    for i in range(5):
        node_primary.safe_sql(f"INSERT INTO wait_test VALUES (200 + {i});")
        flush_lsns.append(
            node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()"))

    # Start standby_flush waiters (they will block since walreceiver is stopped)
    flush_sessions = []
    for i in range(5):
        sess = node_standby.connect("postgres")
        flush_sessions.append(sess)
        sess.do_async(
            f"WAIT FOR LSN '{flush_lsns[i]}' "
            "WITH (MODE 'standby_flush', timeout '1d');\n"
            f"SELECT log_wait_done('flush_done', {i});")

    # Verify waiters are blocked
    assert node_standby.poll_query_until(
        "SELECT count(*) = 5 FROM pg_stat_activity "
        "WHERE wait_event = 'WaitForWalFlush'")

    # Restore walreceiver to unblock waiters
    flush_log_offset = node_standby.log_position()
    _resume_walreceiver(node_standby)

    # Wait for all waiters to complete and close sessions
    for i in range(5):
        node_standby.wait_for_log(f"flush_done {i}", flush_log_offset)
        flush_sessions[i].wait_for_completion()
        flush_sessions[i].close()

    # Verify on standby that WAL was flushed up to the target LSN
    output = node_standby.safe_sql(
        "SELECT pg_lsn_cmp(pg_last_wal_receive_lsn(), "
        f"'{flush_lsns[4]}'::pg_lsn);")
    assert int(output) >= 0, \
        "multiple standby_flush waiters: standby flushed WAL up to target LSN"

    # 7d. Check the scenario of mixed standby mode waiters (standby_replay,
    # standby_write, standby_flush) running concurrently.  We start 6 sessions:
    # 2 for each mode, all waiting for the same target LSN.  We stop the
    # walreceiver and pause replay to ensure all waiters block.  Then we resume
    # replay and restart the walreceiver to verify they unblock and complete
    # correctly.

    # Stop walreceiver first to ensure we can control the flow without hanging
    # (stopping it after pausing replay can hang if the startup process is
    # paused).
    _stop_walreceiver(node_standby)

    # Pause replay
    node_standby.safe_sql("SELECT pg_wal_replay_pause();")

    # Generate WAL on primary
    node_primary.safe_sql(
        "INSERT INTO wait_test VALUES (generate_series(301, 310));")
    mixed_target_lsn = node_primary.safe_sql(
        "SELECT pg_current_wal_insert_lsn()")

    # Start 6 waiters: 2 for each mode
    mixed_sessions = []
    mixed_modes = ("standby_replay", "standby_write", "standby_flush")
    for i in range(6):
        sess = node_standby.connect("postgres")
        mixed_sessions.append(sess)
        sess.do_async(
            f"WAIT FOR LSN '{mixed_target_lsn}' "
            f"WITH (MODE '{mixed_modes[i % 3]}', timeout '1d');\n"
            f"SELECT log_wait_done('mixed_done', {i});")

    # Verify all waiters are blocked
    assert node_standby.poll_query_until(
        "SELECT count(*) = 6 FROM pg_stat_activity "
        "WHERE wait_event LIKE 'WaitForWal%'")

    # Resume replay (waiters should still be blocked as no WAL has arrived)
    mixed_log_offset = node_standby.log_position()
    node_standby.safe_sql("SELECT pg_wal_replay_resume();")
    assert node_standby.poll_query_until(
        "SELECT NOT pg_is_wal_replay_paused();")

    # Restore walreceiver to allow WAL to arrive
    _resume_walreceiver(node_standby)

    # Wait for all sessions to complete and close them
    for i in range(6):
        node_standby.wait_for_log(f"mixed_done {i}", mixed_log_offset)
        mixed_sessions[i].wait_for_completion()
        mixed_sessions[i].close()

    # Verify all modes reached the target LSN
    output = node_standby.safe_sql(
        "SELECT pg_lsn_cmp((SELECT written_lsn FROM pg_stat_wal_receiver), "
        f"'{mixed_target_lsn}'::pg_lsn) >= 0 AND "
        f"pg_lsn_cmp(pg_last_wal_receive_lsn(), '{mixed_target_lsn}'::pg_lsn) >= 0 AND "
        f"pg_lsn_cmp(pg_last_wal_replay_lsn(), '{mixed_target_lsn}'::pg_lsn) >= 0;")
    assert output == "t", \
        "mixed mode waiters: all modes completed and reached target LSN"

    # 7e. Check the scenario of multiple primary_flush waiters on primary.
    # We start 5 background sessions waiting for different LSNs with
    # primary_flush mode.  Each waiter logs when done.
    primary_flush_lsns = []
    for i in range(5):
        node_primary.safe_sql(f"INSERT INTO wait_test VALUES (400 + {i});")
        primary_flush_lsns.append(
            node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()"))

    primary_flush_log_offset = node_primary.log_position()

    # Start primary_flush waiters
    primary_flush_sessions = []
    for i in range(5):
        sess = node_primary.connect("postgres")
        primary_flush_sessions.append(sess)
        sess.do_async(
            f"WAIT FOR LSN '{primary_flush_lsns[i]}' "
            "WITH (MODE 'primary_flush', timeout '1d');\n"
            f"SELECT log_wait_done('primary_flush_done', {i});")

    # The WAL should already be flushed, so waiters should complete quickly
    for i in range(5):
        node_primary.wait_for_log(
            f"primary_flush_done {i}", primary_flush_log_offset)
        primary_flush_sessions[i].wait_for_completion()
        primary_flush_sessions[i].close()

    # Verify on primary that WAL was flushed up to the target LSN
    output = node_primary.safe_sql(
        "SELECT pg_lsn_cmp(pg_current_wal_flush_lsn(), "
        f"'{primary_flush_lsns[4]}'::pg_lsn);")
    assert int(output) >= 0, \
        "multiple primary_flush waiters: primary flushed WAL up to target LSN"

    # 8. Check that the standby promotion terminates all standby wait modes.
    # Start waiting for unreachable LSNs with standby_replay, standby_write, and
    # standby_flush modes, then promote.  Check the log for the relevant error
    # messages.  Also, check that waiting for already replayed LSN doesn't cause
    # an error even after promotion.
    lsn4 = node_primary.safe_sql(
        "SELECT pg_current_wal_insert_lsn() + 10000000000")
    lsn5 = node_primary.safe_sql("SELECT pg_current_wal_insert_lsn()")

    # Start background sessions waiting for unreachable LSN with all modes
    wait_modes = ("standby_replay", "standby_write", "standby_flush")
    wait_sessions = []
    for i in range(3):
        sess = node_standby.connect("postgres")
        wait_sessions.append(sess)
        sess.do_async(
            f"WAIT FOR LSN '{lsn4}' WITH (MODE '{wait_modes[i]}');")

    # Make sure standby will be promoted at least at the primary insert LSN we
    # have just observed.  Use pg_switch_wal() to force the insert LSN to be
    # written then wait for standby to catchup.
    node_primary.safe_sql("SELECT pg_switch_wal();")
    node_primary.wait_for_catchup(node_standby)

    log_offset = node_standby.log_position()
    node_standby.promote()

    # Wait for all three sessions to get the error (each mode has distinct
    # message)
    node_standby.wait_for_log(
        r"Recovery ended before target LSN.*was written", log_offset)
    node_standby.wait_for_log(
        r"Recovery ended before target LSN.*was flushed", log_offset)
    node_standby.wait_for_log(
        r"Recovery ended before target LSN.*was replayed", log_offset)

    # promotion interrupted all wait modes
    for sess in wait_sessions:
        sess.close()

    node_standby.safe_sql(f"WAIT FOR LSN '{lsn5}';")
    # wait for already replayed LSN exits immediately even after promotion

    output = node_standby.safe_sql(
        f"WAIT FOR LSN '{lsn4}' WITH (timeout '10ms', no_throw);")
    assert output == "not in recovery", \
        "WAIT FOR returns correct status after standby promotion"

    node_standby.stop()
    node_primary.stop()

    # Sessions will be cleaned up automatically when they go out of scope.

    # 9. Archive-only standby tests: verify standby_write/standby_flush work
    # without a walreceiver.  These exercise the replay-position floor in
    # GetCurrentLSNForWaitType().
    #
    # We set up a separate primary with archiving and an archive-only standby
    # (has_restoring, no has_streaming), so no walreceiver ever starts and the
    # shared walreceiver positions (writtenUpto, flushedUpto) stay at their
    # zero-initialized values.

    arc_primary = create_pg("arc_primary", has_archiving=True,
                            allows_streaming=True)

    arc_primary.safe_sql(
        "CREATE TABLE arc_test AS SELECT generate_series(1,10) AS a")

    arc_backup_name = "arc_backup"
    arc_primary.backup(arc_backup_name)

    # Generate WAL that will be archived and replayed on the standby.
    arc_primary.safe_sql(
        "INSERT INTO arc_test VALUES (generate_series(11, 20))")
    arc_target_lsn = arc_primary.safe_sql(
        "SELECT pg_current_wal_insert_lsn()")

    # Force WAL to be archived by switching segments, then wait for archiving.
    arc_segment = arc_primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())")
    arc_primary.safe_sql("SELECT pg_switch_wal()")
    assert arc_primary.poll_query_until(
        f"SELECT last_archived_wal >= '{arc_segment}' FROM pg_stat_archiver"
    ), "Timed out waiting for WAL archiving on arc_primary"

    # Create an archive-only standby: has_restoring but NOT has_streaming.
    # No primary_conninfo means no walreceiver will start.
    arc_standby = create_pg("arc_standby", start=False)
    arc_standby.init_from_backup(arc_primary, arc_backup_name,
                                 has_restoring=True)
    arc_standby.start()

    # Wait for the standby to replay past our target LSN via archive recovery.
    assert arc_standby.poll_query_until(
        f"SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '{arc_target_lsn}') >= 0"
    ), "Timed out waiting for archive replay on arc_standby"

    # Sanity: verify no walreceiver is running.
    output = arc_standby.safe_sql(
        "SELECT count(*) FROM pg_stat_wal_receiver")
    assert output == "0", "arc_standby has no walreceiver"

    # 9a. Getter fallback: standby_write/standby_flush succeed immediately when
    # the target LSN has already been replayed, even though writtenUpto and
    # flushedUpto are zero.  GetCurrentLSNForWaitType() returns
    # Max(walrcv_pos, replay), so replay >= target satisfies the check on the
    # first loop iteration without ever sleeping.
    output = arc_standby.safe_sql(
        f"WAIT FOR LSN '{arc_target_lsn}' "
        "WITH (MODE 'standby_write', timeout '3s', no_throw);")
    assert output == "success", \
        "standby_write succeeds on archive-only standby (getter fallback)"

    output = arc_standby.safe_sql(
        f"WAIT FOR LSN '{arc_target_lsn}' "
        "WITH (MODE 'standby_flush', timeout '3s', no_throw);")
    assert output == "success", \
        "standby_flush succeeds on archive-only standby (getter fallback)"

    # 9b. Replay waker: standby_write/standby_flush waiters that go to sleep
    # (target > replay at entry) are woken when replay catches up.  This tests
    # that PerformWalRecovery() calls WaitLSNWakeup for STANDBY_WRITE and
    # STANDBY_FLUSH, not just STANDBY_REPLAY.
    #
    # Pause replay, archive more WAL, start background waiters, then resume
    # replay and verify the waiters complete.
    arc_standby.safe_sql("SELECT pg_wal_replay_pause()")

    # Generate more WAL and archive it.
    arc_primary.safe_sql(
        "INSERT INTO arc_test VALUES (generate_series(21, 30))")
    arc_target_lsn2 = arc_primary.safe_sql(
        "SELECT pg_current_wal_insert_lsn()")

    arc_segment2 = arc_primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())")
    arc_primary.safe_sql("SELECT pg_switch_wal()")
    assert arc_primary.poll_query_until(
        f"SELECT last_archived_wal >= '{arc_segment2}' FROM pg_stat_archiver"
    ), "Timed out waiting for WAL archiving on arc_primary (round 2)"

    # Start background waiters.  With replay paused, target > replay, so they
    # will sleep on WaitLatch.  They can only be woken by the replay-loop
    # WaitLSNWakeup calls.
    arc_write_session = arc_standby.connect("postgres")
    arc_write_session.do_async(
        f"WAIT FOR LSN '{arc_target_lsn2}' "
        "WITH (MODE 'standby_write', timeout '1d', no_throw);")

    arc_flush_session = arc_standby.connect("postgres")
    arc_flush_session.do_async(
        f"WAIT FOR LSN '{arc_target_lsn2}' "
        "WITH (MODE 'standby_flush', timeout '1d', no_throw);")

    # Verify both waiters are blocked.
    assert arc_standby.poll_query_until(
        "SELECT count(*) = 2 FROM pg_stat_activity "
        "WHERE wait_event LIKE 'WaitForWal%'"
    ), "Timed out waiting for arc_standby waiters to block"

    # Resume replay.  The startup process should wake the STANDBY_WRITE and
    # STANDBY_FLUSH waiters as it replays past arc_target_lsn2.
    arc_standby.safe_sql("SELECT pg_wal_replay_resume()")

    arc_write_out = arc_write_session.get_async_result()
    arc_flush_out = arc_flush_session.get_async_result()
    arc_write_session.close()
    arc_flush_session.close()

    assert arc_write_out.psqlout == "success", \
        "standby_write waiter woken by replay on archive-only standby"
    assert arc_flush_out.psqlout == "success", \
        "standby_flush waiter woken by replay on archive-only standby"

    arc_standby.stop()
    arc_primary.stop()

    # 10. Fresh-shmem walreceiver startup (29e7dbf5e4d).
    # RequestXLogStreaming() initializes writtenUpto/flushedUpto to the
    # segment-aligned receiveStart only when receiveStart was invalid.
    # Restart the standby with the primary stopped, so the walreceiver cannot
    # connect and advance these values past the initial one before we observe
    # it.
    rcv_primary = create_pg("rcv_primary", allows_streaming=True)
    # No background WAL during our probes.
    rcv_primary.append_conf("autovacuum = off")
    rcv_primary.restart()
    rcv_primary.safe_sql(
        "CREATE TABLE rcv_test AS SELECT generate_series(1,10) AS a")

    rcv_backup = "rcv_backup"
    rcv_primary.backup(rcv_backup)

    rcv_standby = create_pg("rcv_standby", start=False)
    rcv_standby.init_from_backup(rcv_primary, rcv_backup, has_streaming=True)
    rcv_standby.start()

    # Switch WAL segments mid-stream so the replay ends mid-segment after the
    # upcoming standby restart.  That guarantees the initial value <
    # final replay LSN.
    rcv_primary.safe_sql(
        "INSERT INTO rcv_test VALUES (generate_series(11, 100))")
    rcv_primary.safe_sql("SELECT pg_switch_wal()")
    rcv_primary.safe_sql(
        "INSERT INTO rcv_test VALUES (generate_series(101, 110))")
    rcv_primary.wait_for_catchup(rcv_standby)

    # Restart the standby with the primary down: WalRcvData is initialized, but
    # the walreceiver cannot connect and update writtenUpto/flushedUpto.  So,
    # the initial flushedUpto stays observable via pg_last_wal_receive_lsn().
    rcv_standby.stop()
    rcv_primary.stop()
    rcv_standby.start()

    assert rcv_standby.poll_query_until(
        "SELECT pg_last_wal_receive_lsn() IS NOT NULL;"
    ), "walreceiver initial value did not become visible"

    # Freeze the replay so the (received, replay] window stays observable.
    rcv_standby.safe_sql("SELECT pg_wal_replay_pause()")
    assert rcv_standby.poll_query_until(
        "SELECT pg_get_wal_replay_pause_state() = 'paused'"
    ), "Timed out waiting for rcv_standby replay to pause"

    rcv_receive = rcv_standby.safe_sql("SELECT pg_last_wal_receive_lsn()")
    rcv_replay = rcv_standby.safe_sql("SELECT pg_last_wal_replay_lsn()")
    rcv_gap = rcv_standby.safe_sql(
        f"SELECT pg_wal_lsn_diff('{rcv_replay}'::pg_lsn, "
        f"'{rcv_receive}'::pg_lsn) > 0")
    assert rcv_gap == "t", \
        "replay sits ahead of initial walreceiver flush position"

    rcv_receive_offset = rcv_standby.safe_sql(
        f"SELECT mod(pg_wal_lsn_diff('{rcv_receive}'::pg_lsn, '0/0'::pg_lsn), "
        "setting::numeric)::int "
        "FROM pg_settings WHERE name = 'wal_segment_size'")
    assert rcv_receive_offset == "0", \
        "initial walreceiver flush position is segment-aligned"

    # WAIT FOR an rcv_replay LSN succeeds in standby_write / standby_flush modes
    # thanks to GetCurrentLSNForWaitType() taking replay LSN as the floor.
    # We observe flushedUpto directly via pg_last_wal_receive_lsn().
    # writtenUpto is covered indirectly: without the replay-position floor,
    # standby_write would wait at the seeded segment-start position and time
    # out.
    for rcv_mode in ("standby_write", "standby_flush"):
        output = rcv_standby.safe_sql(
            f"WAIT FOR LSN '{rcv_replay}' "
            f"WITH (MODE '{rcv_mode}', timeout '5s', no_throw);")
        assert output == "success", \
            f"{rcv_mode} succeeds for already-replayed LSN after standby restart"

    # Restore primary and resume replay so section 11 can reuse the clusters.
    # Generate fresh WAL after reconnecting so the walreceiver advances its
    # flush position past the replay position before we freeze both frontiers.
    rcv_standby.safe_sql("SELECT pg_wal_replay_resume()")
    rcv_primary.start()
    rcv_primary.safe_sql(
        "INSERT INTO rcv_test VALUES (generate_series(111, 120))")
    rcv_primary.wait_for_catchup(rcv_standby)

    # 11. Off-by-one boundary checks for the wait predicate target <=
    # currentLSN.  Stop the walreceiver before pausing replay (stopping after
    # pause can hang -- see section 7d) so both replay and walreceiver positions
    # are frozen.
    _stop_walreceiver(rcv_standby)
    rcv_standby.safe_sql("SELECT pg_wal_replay_pause()")
    assert rcv_standby.poll_query_until(
        "SELECT pg_get_wal_replay_pause_state() = 'paused'"
    ), "Timed out waiting for rcv_standby replay to pause"

    # 11a. standby_replay exact fencepost.  The replay position is frozen, so
    # this probes the standby_replay predicate directly.
    replay_lsn = rcv_standby.safe_sql("SELECT pg_last_wal_replay_lsn()")
    _, replay_lsn_plus = _check_wait_for_lsn_fencepost(
        rcv_standby, "standby_replay", replay_lsn, "standby_replay")

    # 11b. standby_flush exact fencepost.  pg_last_wal_receive_lsn() exposes the
    # flushed walreceiver position even after walreceiver exits, so this probes
    # the standby_flush predicate directly.  standby_write has no stable
    # SQL-visible boundary once walreceiver is stopped; it is covered by the
    # replay-floor and waiter wakeup tests above.
    flush_lsn = rcv_standby.safe_sql("SELECT pg_last_wal_receive_lsn()")
    flush_covers_replay = rcv_standby.safe_sql(
        f"SELECT pg_wal_lsn_diff('{flush_lsn}'::pg_lsn, "
        f"'{replay_lsn}'::pg_lsn) >= 0")
    assert flush_covers_replay == "t", \
        "standby_flush boundary is not masked by replay floor"

    _check_wait_for_lsn_fencepost(
        rcv_standby, "standby_flush", flush_lsn, "standby_flush")

    # 11c. A sleeping waiter at current + 1 wakes once replay advances past it.
    # Start the waiter while replay is still paused so it is guaranteed to sleep
    # at replay_lsn_plus regardless of whether flush_lsn > replay_lsn.  Then
    # resume replay and restart the walreceiver to deliver new WAL.
    rcv_primary.safe_sql(
        "INSERT INTO rcv_test VALUES (generate_series(200, 210))")

    boundary_session = rcv_standby.connect("postgres")
    boundary_session.do_async(
        f"WAIT FOR LSN '{replay_lsn_plus}' "
        "WITH (MODE 'standby_replay', timeout '1d', no_throw);")

    assert rcv_standby.poll_query_until(
        "SELECT count(*) > 0 FROM pg_stat_activity "
        "WHERE wait_event = 'WaitForWalReplay'"
    ), "Boundary waiter did not sleep"

    rcv_standby.safe_sql("SELECT pg_wal_replay_resume()")
    _resume_walreceiver(rcv_standby)
    boundary_out = boundary_session.get_async_result()
    boundary_session.close()
    assert boundary_out.psqlout == "success", \
        "standby_replay: waiter at current + 1 wakes when replay advances"

    rcv_standby.stop()
    rcv_primary.stop()

    # 12. Timeline switch on a cascade standby.  A WAIT FOR LSN waiter on a
    # cascade standby must survive its upstream's promotion: the cascade
    # walreceiver reconnects on the new timeline and replay continues across the
    # boundary.
    tl_primary = create_pg("tl_primary", allows_streaming=True)
    tl_primary.append_conf("autovacuum = off")
    tl_primary.restart()
    tl_primary.safe_sql(
        "CREATE TABLE tl_test AS SELECT generate_series(1, 10) AS a")

    tl_backup = "tl_backup"
    tl_primary.backup(tl_backup)

    tl_standby1 = create_pg("tl_standby1", start=False)
    tl_standby1.init_from_backup(tl_primary, tl_backup, has_streaming=True)
    tl_standby1.start()

    # standby2 cascades from standby1.
    tl_backup2 = "tl_backup2"
    tl_standby1.backup(tl_backup2)

    tl_standby2 = create_pg("tl_standby2", start=False)
    tl_standby2.init_from_backup(tl_standby1, tl_backup2, has_streaming=True)
    tl_standby2.start()

    tl_primary.safe_sql(
        "INSERT INTO tl_test VALUES (generate_series(11, 20))")
    tl_primary.wait_for_catchup(tl_standby1)
    tl_standby1.wait_for_catchup(tl_standby2)

    # Target LSN well past current insert LSN, so reaching it requires WAL
    # produced on the new timeline.  Pause replay on standby2 to guarantee the
    # waiter is asleep when the switch happens.
    tl_target = tl_primary.safe_sql(
        "SELECT (pg_current_wal_insert_lsn() + 65536)::text")

    tl_standby2.safe_sql("SELECT pg_wal_replay_pause()")
    assert tl_standby2.poll_query_until(
        "SELECT pg_get_wal_replay_pause_state() = 'paused'"
    ), "Timed out waiting for tl_standby2 replay to pause"

    tl_session = tl_standby2.connect("postgres")
    tl_session.do_async(
        f"WAIT FOR LSN '{tl_target}' "
        "WITH (MODE 'standby_replay', timeout '1d', no_throw);")

    assert tl_standby2.poll_query_until(
        "SELECT count(*) > 0 FROM pg_stat_activity "
        "WHERE wait_event = 'WaitForWalReplay'"
    ), "Cascade waiter did not sleep before promotion"

    # Promote standby1 to TLI 2; produce enough WAL on the new timeline to push
    # past tl_target and force a segment switch.
    tl_standby1.promote()
    tl_standby1.safe_sql(
        "INSERT INTO tl_test VALUES (generate_series(21, 1020))")
    tl_standby1.safe_sql("SELECT pg_switch_wal()")

    tl_standby2.safe_sql("SELECT pg_wal_replay_resume()")

    assert tl_standby2.poll_query_until(
        "SELECT received_tli > 1 FROM pg_stat_wal_receiver"
    ), "tl_standby2 did not follow upstream timeline switch"

    tl_out = tl_session.get_async_result()
    tl_session.close()
    assert tl_out.psqlout == "success", \
        "WAIT FOR LSN survives upstream promotion and timeline switch on cascade standby"

    tl_standby2.stop()
    tl_standby1.stop()
    tl_primary.stop()
