# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Checks for recovery_min_apply_delay and recovery pause."""

import time


def test_005_replay_delay(create_pg):
    # Initialize primary node
    node_primary = create_pg("primary", allows_streaming=True)

    # And some content
    node_primary.safe_sql(
        "CREATE TABLE tab_int AS SELECT generate_series(1, 10) AS a")

    # Take backup
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Create streaming standby from backup
    node_standby = create_pg("standby", start=False)
    delay = 3
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.append_conf(f"""
recovery_min_apply_delay = '{delay}s'
""")
    node_standby.start()

    # Make new content on primary and check its presence in standby depending
    # on the delay applied above. Before doing the insertion, get the
    # current timestamp that will be used as a comparison base. Even on slow
    # machines, this allows to have a predictable behavior when comparing the
    # delay between data insertion moment on primary and replay time on standby.
    primary_insert_time = time.time()
    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(11, 20))")

    # Now wait for replay to complete on standby. We're done waiting when the
    # standby has replayed up to the previously saved primary LSN.
    until_lsn = node_primary.safe_sql("SELECT pg_current_wal_lsn()")

    assert node_standby.poll_query_until(
        f"SELECT (pg_last_wal_replay_lsn() - '{until_lsn}'::pg_lsn) >= 0"
    ), "standby never caught up"

    # This test is successful if and only if the LSN has been applied with at
    # least the configured apply delay.
    assert time.time() - primary_insert_time >= delay, \
        "standby applies WAL only after replication delay"

    # Check that recovery can be paused or resumed expectedly.
    node_standby2 = create_pg("standby2", start=False)
    node_standby2.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby2.start()

    # Recovery is not yet paused.
    assert node_standby2.safe_sql(
        "SELECT pg_get_wal_replay_pause_state()") == "not paused", \
        "pg_get_wal_replay_pause_state() reports not paused"

    # Request to pause recovery and wait until it's actually paused.
    node_standby2.safe_sql("SELECT pg_wal_replay_pause()")
    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(21,30))")
    assert node_standby2.poll_query_until(
        "SELECT pg_get_wal_replay_pause_state() = 'paused'"
    ), "Timed out while waiting for recovery to be paused"

    # Even if new WAL records are streamed from the primary,
    # recovery in the paused state doesn't replay them.
    receive_lsn = node_standby2.safe_sql("SELECT pg_last_wal_receive_lsn()")
    replay_lsn = node_standby2.safe_sql("SELECT pg_last_wal_replay_lsn()")
    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(31,40))")
    assert node_standby2.poll_query_until(
        f"SELECT '{receive_lsn}'::pg_lsn < pg_last_wal_receive_lsn()"
    ), "Timed out while waiting for new WAL to be streamed"
    assert node_standby2.safe_sql(
        "SELECT pg_last_wal_replay_lsn()") == replay_lsn, \
        "no WAL is replayed in the paused state"

    # Request to resume recovery and wait until it's actually resumed.
    node_standby2.safe_sql("SELECT pg_wal_replay_resume()")
    assert node_standby2.poll_query_until(
        "SELECT pg_get_wal_replay_pause_state() = 'not paused' AND "
        f"pg_last_wal_replay_lsn() > '{replay_lsn}'::pg_lsn"
    ), "Timed out while waiting for recovery to be resumed"

    # Check that the paused state ends and promotion continues if a promotion
    # is triggered while recovery is paused.
    node_standby2.safe_sql("SELECT pg_wal_replay_pause()")
    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(41,50))")
    assert node_standby2.poll_query_until(
        "SELECT pg_get_wal_replay_pause_state() = 'paused'"
    ), "Timed out while waiting for recovery to be paused"

    node_standby2.promote()
    assert node_standby2.poll_query_until(
        "SELECT NOT pg_is_in_recovery()"
    ), "Timed out while waiting for promotion to finish"
