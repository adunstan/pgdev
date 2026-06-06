# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test for timeline switch.

Ensure that a cascading standby is able to follow a newly-promoted standby
on a new timeline.
"""


def test_004_timeline_switch(create_pg):
    # Initialize primary node
    node_primary = create_pg("primary", allows_streaming=True)

    # Take backup
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Create two standbys linking to it
    node_standby_1 = create_pg("standby_1", start=False)
    node_standby_1.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby_1.start()
    node_standby_2 = create_pg("standby_2", start=False)
    node_standby_2.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby_2.start()

    # Create some content on primary
    node_primary.safe_sql(
        "CREATE TABLE tab_int AS SELECT generate_series(1,1000) AS a")

    # Cleanly stop and remove primary.  A clean stop is required so as all
    # the records generated on the primary are received and flushed by the two
    # standbys.
    node_primary.stop()

    # promote standby 1 using "pg_promote", switching it to a new timeline
    result = node_standby_1.sql("SELECT pg_promote(wait_seconds => 300)")
    assert result.psqlout == "t", "promotion of standby with pg_promote"

    # Switch standby 2 to replay from standby 1.  During the timeline switch,
    # the WAL receiver process on standby 2 should not be stopped, and the
    # new primary connection string should not be visible
    # in pg_stat_wal_receiver.
    secret = "dont_show_me"
    # PostgresServer.connstr() returns a value with embedded single quotes and
    # a dbname, which cannot be wrapped inside a single-quoted primary_conninfo
    # GUC.  Build the conninfo from the node's host/port directly, as an
    # unquoted key=value string.
    # Include application_name so wait_for_catchup can find this standby in
    # pg_stat_replication on the newly-promoted node_standby_1.
    connstr_1 = f"host={node_standby_1.host} port={node_standby_1.port}"
    node_standby_2.append_conf(f"""
primary_conninfo='{connstr_1} password={secret} application_name={node_standby_2.name}'
""")

    # Rotate logfile before restarting, for the log checks done below.
    # The framework uses a single log file, so capture the current log
    # position to use as an offset for the post-restart log checks.
    log_offset = node_standby_2.log_position()
    node_standby_2.restart()

    # Wait for walreceiver to reconnect after the restart.  We want to
    # verify that after reconnection, the walreceiver stays alive during
    # the timeline switch.
    assert node_standby_2.poll_query_until(
        "SELECT EXISTS(SELECT 1 FROM pg_stat_wal_receiver)")
    wr_pid_before_switch = node_standby_2.safe_sql(
        "SELECT pid FROM pg_stat_wal_receiver")

    # Insert some data in standby 1 and check its presence in standby 2
    # to ensure that the timeline switch has been done.
    node_standby_1.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(1001,2000))")
    node_standby_1.wait_for_catchup(node_standby_2)

    result = node_standby_2.safe_sql("SELECT count(*) FROM tab_int")
    assert result == "2000", "check content of standby 2"

    # Check the logs, WAL receiver should not have been stopped while
    # transitioning to its new timeline.  There is no need to rely on an
    # offset in this check of the server logs: a new log file is used on
    # node restart when primary_conninfo is updated above.
    assert not node_standby_2.log_contains(
        "FATAL: .* terminating walreceiver process due to administrator command",
        offset=log_offset,
    ), "WAL receiver should not be stopped across timeline jumps"

    # Verify that the walreceiver process stayed alive across the timeline
    # switch, check its PID.
    wr_pid_after_switch = node_standby_2.safe_sql(
        "SELECT pid FROM pg_stat_wal_receiver")

    assert wr_pid_before_switch == wr_pid_after_switch, \
        "WAL receiver PID matches across timeline jumps"

    raw_conninfo_count = node_standby_2.safe_sql(
        f"SELECT count(*) FROM pg_stat_wal_receiver WHERE conninfo LIKE '%{secret}%'"
    )

    assert raw_conninfo_count == "0", \
        "pg_stat_wal_receiver.conninfo not updated across timeline jumps"

    # Ensure that a standby is able to follow a primary on a newer timeline
    # when WAL archiving is enabled.

    # Initialize primary node
    node_primary_2 = create_pg(
        "primary_2", start=False, allows_streaming=True, has_archiving=True)
    node_primary_2.append_conf("""
wal_keep_size = 512MB
""")
    node_primary_2.start()

    # Take backup
    node_primary_2.backup(backup_name)

    # Create standby node
    node_standby_3 = create_pg("standby_3", start=False)
    node_standby_3.init_from_backup(node_primary_2, backup_name, has_streaming=True)

    # Restart primary node in standby mode and promote it, switching it
    # to a new timeline.
    node_primary_2.set_standby_mode()
    node_primary_2.restart()
    node_primary_2.promote()

    # Start standby node, create some content on primary and check its presence
    # in standby, to ensure that the timeline switch has been done.
    node_standby_3.start()
    node_primary_2.safe_sql("CREATE TABLE tab_int AS SELECT 1 AS a")
    node_primary_2.wait_for_catchup(node_standby_3)

    result_2 = node_standby_3.safe_sql("SELECT count(*) FROM tab_int")
    assert result_2 == "1", "check content of standby 3"
