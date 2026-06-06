# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Minimal test testing streaming replication."""

import os
import re

from libpq import Session
from libpq.errors import ConnectionError as PqConnectionError


def get_slot_xmins(node, slotname, check_expr):
    """Fetch (xmin, catalog_xmin) of a slot once *check_expr* holds.

    Polls pg_replication_slots until
    *check_expr* is true to reach a quiescent state, then returns the slot's
    xmin and catalog_xmin (empty string for NULL, mirroring psql -At).
    """
    assert node.poll_query_until(
        f"SELECT {check_expr} "
        "FROM pg_catalog.pg_replication_slots "
        f"WHERE slot_name = '{slotname}'"
    ), "Timed out waiting for slot xmins to advance"

    row = node.sql(
        "SELECT xmin, catalog_xmin "
        "FROM pg_catalog.pg_replication_slots "
        f"WHERE slot_name = '{slotname}'"
    ).rows[0]
    xmin = "" if row[0] is None else row[0]
    catalog_xmin = "" if row[1] is None else row[1]
    return xmin, catalog_xmin


def advance_wal(node, num):
    """Advance WAL by *num* segments."""
    for _ in range(num):
        node.safe_sql(
            "SELECT pg_logical_emit_message(false, '', 'foo')")
        node.safe_sql("SELECT pg_switch_wal()")


def check_target_session_attrs(node1, node2, target_node, mode, status):
    """Attempt to connect to node1,node2 with target_session_attrs=mode.

    Expect to connect to *target_node* (None for failure) with the given exit
    *status* (0 success, 2 connection failure).
    """
    connstr = (
        f"host={node1.host},{node2.host} "
        f"port={node1.port},{node2.port} "
        f"dbname=postgres "
        f"target_session_attrs={mode}"
    )

    sess = None
    ret = 0
    stdout = None
    try:
        sess = Session(connstr=connstr, libdir=node1.libdir)
        stdout = sess.query_oneval("SHOW port")
    except PqConnectionError:
        ret = 2
    finally:
        if sess is not None:
            sess.close()

    if status == 0:
        target_port = str(target_node.port)
        assert status == ret and stdout == target_port, (
            f"connect to node {target_node.name} with mode \"{mode}\" and "
            f"{node1.name},{node2.name} listed"
        )
    else:
        assert status == ret and target_node is None, (
            f"fail to connect with mode \"{mode}\" and "
            f"{node1.name},{node2.name} listed"
        )


def test_001_stream_rep(create_pg):
    # Initialize primary node
    #
    # A specific role is created to perform some tests related to replication,
    # and it needs proper authentication configuration.  Under the trust auth
    # this framework uses, the repl_role role just has to exist, which the SQL
    # below ensures.
    node_primary = create_pg("primary", allows_streaming=True)
    backup_name = "my_backup"

    # Take backup
    node_primary.backup(backup_name)

    # Create streaming standby linking to primary
    node_standby_1 = create_pg("standby_1", start=False)
    node_standby_1.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby_1.start()

    # Take backup of standby 1 (not mandatory, but useful to check if
    # pg_basebackup works on a standby).
    node_standby_1.backup(backup_name)

    # Take a second backup of the standby while the primary is offline.
    node_primary.stop()
    node_standby_1.backup("my_backup_2")
    node_primary.start()

    # Create second standby node linking to standby 1
    node_standby_2 = create_pg("standby_2", start=False)
    node_standby_2.init_from_backup(node_standby_1, backup_name, has_streaming=True)
    node_standby_2.start()

    # Reset IO statistics, for the WAL sender check with pg_stat_io.
    node_primary.safe_sql("SELECT pg_stat_reset_shared('io')")

    # Create some content on primary and check its presence in standby nodes
    node_primary.safe_sql(
        "CREATE TABLE tab_int AS SELECT generate_series(1,1002) AS a")

    node_primary.safe_sql(
        """
CREATE TABLE user_logins(id serial, who text);

CREATE FUNCTION on_login_proc() RETURNS EVENT_TRIGGER AS $$
BEGIN
  IF NOT pg_is_in_recovery() THEN
    INSERT INTO user_logins (who) VALUES (session_user);
  END IF;
  IF session_user = 'regress_hacker' THEN
    RAISE EXCEPTION 'You are not welcome!';
  END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE EVENT TRIGGER on_login_trigger ON login EXECUTE FUNCTION on_login_proc();
ALTER EVENT TRIGGER on_login_trigger ENABLE ALWAYS;
""")

    # Wait for standbys to catch up
    node_primary.wait_for_replay_catchup(node_standby_1)
    node_standby_1.wait_for_replay_catchup(node_standby_2, node_primary)

    result = node_standby_1.safe_sql("SELECT count(*) FROM tab_int")
    print(f"standby 1: {result}")
    assert result == "1002", "check streamed content on standby 1"

    result = node_standby_2.safe_sql("SELECT count(*) FROM tab_int")
    print(f"standby 2: {result}")
    assert result == "1002", "check streamed content on standby 2"

    result = node_standby_1.safe_sql(
        "SELECT count(*) FROM pg_stat_recovery "
        "WHERE promote_triggered IS NOT NULL")
    assert result == "1", "check recovery state on standby 1"

    # Likewise, but for a sequence
    node_primary.safe_sql("CREATE SEQUENCE seq1")
    node_primary.safe_sql("SELECT nextval('seq1')")

    # Wait for standbys to catch up
    node_primary.wait_for_replay_catchup(node_standby_1)
    node_standby_1.wait_for_replay_catchup(node_standby_2, node_primary)

    result = node_standby_1.safe_sql("SELECT * FROM seq1")
    print(f"standby 1: {result}")
    assert result == "33|0|t", "check streamed sequence content on standby 1"

    result = node_standby_2.safe_sql("SELECT * FROM seq1")
    print(f"standby 2: {result}")
    assert result == "33|0|t", "check streamed sequence content on standby 2"

    # Check pg_sequence_last_value() returns NULL for unlogged sequence on standby
    node_primary.safe_sql("CREATE UNLOGGED SEQUENCE ulseq")
    node_primary.safe_sql("SELECT nextval('ulseq')")
    node_primary.wait_for_replay_catchup(node_standby_1)
    assert node_standby_1.safe_sql(
        "SELECT pg_sequence_last_value('ulseq'::regclass) IS NULL") == "t", \
        "pg_sequence_last_value() on unlogged sequence on standby 1"

    # Check that only READ-only queries can run on standbys
    res = node_standby_1.sql("INSERT INTO tab_int VALUES (1)")
    assert res.error_message is not None, "read-only queries on standby 1"
    res = node_standby_2.sql("INSERT INTO tab_int VALUES (1)")
    assert res.error_message is not None, "read-only queries on standby 2"

    # Tests for connection parameter target_session_attrs
    print('# testing connection parameter "target_session_attrs"')

    # Connect to primary in "read-write" mode with primary,standby1 list.
    check_target_session_attrs(node_primary, node_standby_1, node_primary,
                              "read-write", 0)

    # Connect to primary in "read-write" mode with standby1,primary list.
    check_target_session_attrs(node_standby_1, node_primary, node_primary,
                              "read-write", 0)

    # Connect to primary in "any" mode with primary,standby1 list.
    check_target_session_attrs(node_primary, node_standby_1, node_primary,
                              "any", 0)

    # Connect to standby1 in "any" mode with standby1,primary list.
    check_target_session_attrs(node_standby_1, node_primary, node_standby_1,
                              "any", 0)

    # Connect to primary in "primary" mode with primary,standby1 list.
    check_target_session_attrs(node_primary, node_standby_1, node_primary,
                              "primary", 0)

    # Connect to primary in "primary" mode with standby1,primary list.
    check_target_session_attrs(node_standby_1, node_primary, node_primary,
                              "primary", 0)

    # Connect to standby1 in "read-only" mode with primary,standby1 list.
    check_target_session_attrs(node_primary, node_standby_1, node_standby_1,
                              "read-only", 0)

    # Connect to standby1 in "read-only" mode with standby1,primary list.
    check_target_session_attrs(node_standby_1, node_primary, node_standby_1,
                              "read-only", 0)

    # Connect to primary in "prefer-standby" mode with primary,primary list.
    check_target_session_attrs(node_primary, node_primary, node_primary,
                              "prefer-standby", 0)

    # Connect to standby1 in "prefer-standby" mode with primary,standby1 list.
    check_target_session_attrs(node_primary, node_standby_1, node_standby_1,
                              "prefer-standby", 0)

    # Connect to standby1 in "prefer-standby" mode with standby1,primary list.
    check_target_session_attrs(node_standby_1, node_primary, node_standby_1,
                              "prefer-standby", 0)

    # Connect to standby1 in "standby" mode with primary,standby1 list.
    check_target_session_attrs(node_primary, node_standby_1, node_standby_1,
                              "standby", 0)

    # Connect to standby1 in "standby" mode with standby1,primary list.
    check_target_session_attrs(node_standby_1, node_primary, node_standby_1,
                              "standby", 0)

    # Fail to connect in "read-write" mode with standby1,standby2 list.
    check_target_session_attrs(node_standby_1, node_standby_2, None,
                              "read-write", 2)

    # Fail to connect in "primary" mode with standby1,standby2 list.
    check_target_session_attrs(node_standby_1, node_standby_2, None,
                              "primary", 2)

    # Fail to connect in "read-only" mode with primary,primary list.
    check_target_session_attrs(node_primary, node_primary, None,
                              "read-only", 2)

    # Fail to connect in "standby" mode with primary,primary list.
    check_target_session_attrs(node_primary, node_primary, None, "standby", 2)

    # Test for SHOW commands using a WAL sender connection with a replication
    # role.
    print("# testing SHOW commands for replication connection")

    node_primary.safe_sql(
        """
CREATE ROLE repl_role REPLICATION LOGIN;
GRANT pg_read_all_settings TO repl_role;""")
    primary_host = node_primary.host
    primary_port = node_primary.port
    connstr_common = f"host={primary_host} port={primary_port} user=repl_role"
    connstr_rep = f"{connstr_common} replication=1"
    connstr_db = f"{connstr_common} replication=database dbname=postgres"

    # Test SHOW ALL
    with Session(connstr=connstr_rep, libdir=node_primary.libdir) as sess:
        res = sess.query("SHOW ALL;")
        assert res.error_message is None, \
            "SHOW ALL with replication role and physical replication"
    with Session(connstr=connstr_db, libdir=node_primary.libdir) as sess:
        res = sess.query("SHOW ALL;")
        assert res.error_message is None, \
            "SHOW ALL with replication role and logical replication"

    # Test SHOW with a user-settable parameter
    with Session(connstr=connstr_rep, libdir=node_primary.libdir) as sess:
        res = sess.query("SHOW work_mem;")
        assert res.error_message is None, (
            "SHOW with user-settable parameter, replication role and "
            "physical replication")
    with Session(connstr=connstr_db, libdir=node_primary.libdir) as sess:
        res = sess.query("SHOW work_mem;")
        assert res.error_message is None, (
            "SHOW with user-settable parameter, replication role and "
            "logical replication")

    # Test SHOW with a superuser-settable parameter
    with Session(connstr=connstr_rep, libdir=node_primary.libdir) as sess:
        res = sess.query("SHOW primary_conninfo;")
        assert res.error_message is None, (
            "SHOW with superuser-settable parameter, replication role and "
            "physical replication")
    with Session(connstr=connstr_db, libdir=node_primary.libdir) as sess:
        res = sess.query("SHOW primary_conninfo;")
        assert res.error_message is None, (
            "SHOW with superuser-settable parameter, replication role and "
            "logical replication")

    print("# testing READ_REPLICATION_SLOT command for replication connection")

    slotname = "test_read_replication_slot_physical"

    with Session(connstr=connstr_rep, libdir=node_primary.libdir) as sess:
        res = sess.query("READ_REPLICATION_SLOT non_existent_slot;")
        assert res.error_message is None, \
            "READ_REPLICATION_SLOT exit code 0 on success"
        assert re.search(r"^\|\|$", res.psqlout), \
            "READ_REPLICATION_SLOT returns NULL values if slot does not exist"

    with Session(connstr=connstr_rep, libdir=node_primary.libdir) as sess:
        sess.query(f"CREATE_REPLICATION_SLOT {slotname} PHYSICAL RESERVE_WAL;")

    with Session(connstr=connstr_rep, libdir=node_primary.libdir) as sess:
        res = sess.query(f"READ_REPLICATION_SLOT {slotname};")
        assert res.error_message is None, \
            "READ_REPLICATION_SLOT success with existing slot"
        assert re.search(r"^physical\|[^|]*\|1$", res.psqlout), \
            "READ_REPLICATION_SLOT returns tuple with slot information"

    with Session(connstr=connstr_rep, libdir=node_primary.libdir) as sess:
        sess.query(f"DROP_REPLICATION_SLOT {slotname};")

    print("# switching to physical replication slot")

    # Wait for the physical WAL sender to update its IO statistics.  This is
    # done before the next restart, which would force a flush of its stats, and
    # far enough from the reset done above to not impact the run time.
    assert node_primary.poll_query_until(
        "SELECT sum(reads) > 0 "
        "FROM pg_catalog.pg_stat_io "
        "WHERE backend_type = 'walsender' "
        "AND object = 'wal'"
    ), "Timed out while waiting for the walsender to update its IO statistics"

    # Switch to using a physical replication slot. We can do this without a new
    # backup since physical slots can go backwards if needed. Do so on both
    # standbys. Since we're going to be testing things that affect the slot
    # state, also increase the standby feedback interval to ensure timely
    # updates.
    slotname_1, slotname_2 = "standby_1", "standby_2"
    node_primary.append_conf("max_replication_slots = 4")
    node_primary.restart()
    res = node_primary.sql(
        f"SELECT pg_create_physical_replication_slot('{slotname_1}')")
    assert res.error_message is None, "physical slot created on primary"
    node_standby_1.append_conf(f"primary_slot_name = {slotname_1}")
    node_standby_1.append_conf("wal_receiver_status_interval = 1")
    node_standby_1.append_conf("max_replication_slots = 4")
    node_standby_1.restart()
    res = node_standby_1.sql(
        f"SELECT pg_create_physical_replication_slot('{slotname_2}')")
    assert res.error_message is None, \
        "physical slot created on intermediate replica"
    node_standby_2.append_conf(f"primary_slot_name = {slotname_2}")
    node_standby_2.append_conf("wal_receiver_status_interval = 1")
    # should be able change primary_slot_name without restart
    # will wait effect in get_slot_xmins above
    node_standby_2.reload()

    # There's no hot standby feedback and there are no logical slots on either
    # peer so xmin and catalog_xmin should be null on both slots.
    xmin, catalog_xmin = get_slot_xmins(
        node_primary, slotname_1, "xmin IS NULL AND catalog_xmin IS NULL")
    assert xmin == "", "xmin of non-cascaded slot null with no hs_feedback"
    assert catalog_xmin == "", \
        "catalog xmin of non-cascaded slot null with no hs_feedback"

    xmin, catalog_xmin = get_slot_xmins(
        node_standby_1, slotname_2, "xmin IS NULL AND catalog_xmin IS NULL")
    assert xmin == "", "xmin of cascaded slot null with no hs_feedback"
    assert catalog_xmin == "", \
        "catalog xmin of cascaded slot null with no hs_feedback"

    # Replication still works?
    node_primary.safe_sql("CREATE TABLE replayed(val integer);")

    def replay_check():
        newval = node_primary.safe_sql(
            "INSERT INTO replayed(val) SELECT coalesce(max(val),0) + 1 "
            "AS newval FROM replayed RETURNING val")
        node_primary.wait_for_replay_catchup(node_standby_1)
        node_standby_1.wait_for_replay_catchup(node_standby_2, node_primary)

        assert node_standby_1.safe_sql(
            f"SELECT 1 FROM replayed WHERE val = {newval}") == "1", \
            f"standby_1 didn't replay primary value {newval}"
        assert node_standby_2.safe_sql(
            f"SELECT 1 FROM replayed WHERE val = {newval}") == "1", \
            f"standby_2 didn't replay standby_1 value {newval}"

    replay_check()

    evttrig = node_standby_1.safe_sql(
        "SELECT evtname FROM pg_event_trigger WHERE evtevent = 'login'")
    assert evttrig == "on_login_trigger", "Name of login trigger"
    evttrig = node_standby_2.safe_sql(
        "SELECT evtname FROM pg_event_trigger WHERE evtevent = 'login'")
    assert evttrig == "on_login_trigger", "Name of login trigger"

    print("# enabling hot_standby_feedback")

    # Enable hs_feedback. The slot should gain an xmin. We set the status
    # interval so we'll see the results promptly.
    node_standby_1.safe_sql("ALTER SYSTEM SET hot_standby_feedback = on;")
    node_standby_1.reload()
    node_standby_2.safe_sql("ALTER SYSTEM SET hot_standby_feedback = on;")
    node_standby_2.reload()
    replay_check()

    xmin, catalog_xmin = get_slot_xmins(
        node_primary, slotname_1,
        "xmin IS NOT NULL AND catalog_xmin IS NULL")
    assert xmin != "", "xmin of non-cascaded slot non-null with hs feedback"
    assert catalog_xmin == "", \
        "catalog xmin of non-cascaded slot still null with hs_feedback"

    xmin1, catalog_xmin1 = get_slot_xmins(
        node_standby_1, slotname_2,
        "xmin IS NOT NULL AND catalog_xmin IS NULL")
    assert xmin1 != "", "xmin of cascaded slot non-null with hs feedback"
    assert catalog_xmin1 == "", \
        "catalog xmin of cascaded slot still null with hs_feedback"

    print("# doing some work to advance xmin")
    node_primary.safe_sql(
        """
do $$
begin
  for i in 10000..11000 loop
    -- use an exception block so that each iteration eats an XID
    begin
      insert into tab_int values (i);
    exception
      when division_by_zero then null;
    end;
  end loop;
end$$;
""")

    node_primary.safe_sql("VACUUM;")
    node_primary.safe_sql("CHECKPOINT;")

    xmin2, catalog_xmin2 = get_slot_xmins(
        node_primary, slotname_1, f"xmin <> '{xmin}'")
    print(f"primary slot's new xmin {xmin2}, old xmin {xmin}")
    assert xmin2 != xmin, \
        "xmin of non-cascaded slot with hs feedback has changed"
    assert catalog_xmin2 == "", (
        "catalog xmin of non-cascaded slot still null with hs_feedback "
        "unchanged")

    xmin2, catalog_xmin2 = get_slot_xmins(
        node_standby_1, slotname_2, f"xmin <> '{xmin1}'")
    print(f"standby_1 slot's new xmin {xmin2}, old xmin {xmin1}")
    assert xmin2 != xmin1, "xmin of cascaded slot with hs feedback has changed"
    assert catalog_xmin2 == "", \
        "catalog xmin of cascaded slot still null with hs_feedback unchanged"

    print("# disabling hot_standby_feedback")

    # Disable hs_feedback. Xmin should be cleared.
    node_standby_1.safe_sql("ALTER SYSTEM SET hot_standby_feedback = off;")
    node_standby_1.reload()
    node_standby_2.safe_sql("ALTER SYSTEM SET hot_standby_feedback = off;")
    node_standby_2.reload()
    replay_check()

    xmin, catalog_xmin = get_slot_xmins(
        node_primary, slotname_1, "xmin IS NULL AND catalog_xmin IS NULL")
    assert xmin == "", "xmin of non-cascaded slot null with hs feedback reset"
    assert catalog_xmin == "", \
        "catalog xmin of non-cascaded slot still null with hs_feedback reset"

    xmin, catalog_xmin = get_slot_xmins(
        node_standby_1, slotname_2, "xmin IS NULL AND catalog_xmin IS NULL")
    assert xmin == "", "xmin of cascaded slot null with hs feedback reset"
    assert catalog_xmin == "", \
        "catalog xmin of cascaded slot still null with hs_feedback reset"

    print("# check change primary_conninfo without restart")
    node_standby_2.append_conf("primary_slot_name = ''")
    node_standby_2.enable_streaming(node_primary)
    node_standby_2.reload()

    # The WAL receiver should have generated some IO statistics.
    stats_reads = node_standby_1.safe_sql(
        "SELECT sum(writes) > 0 FROM pg_stat_io "
        "WHERE backend_type = 'walreceiver' AND object = 'wal'")
    assert stats_reads == "t", \
        "WAL receiver generates statistics for WAL writes"

    # be sure do not streaming from cascade
    node_standby_1.stop()

    newval = node_primary.safe_sql(
        "INSERT INTO replayed(val) SELECT coalesce(max(val),0) + 1 "
        "AS newval FROM replayed RETURNING val")
    node_primary.wait_for_catchup(node_standby_2)
    is_replayed = node_standby_2.safe_sql(
        f"SELECT 1 FROM replayed WHERE val = {newval}")
    assert is_replayed == "1", f"standby_2 didn't replay primary value {newval}"

    # Drop any existing slots on the primary, for the follow-up tests.
    node_primary.safe_sql(
        "SELECT pg_drop_replication_slot(slot_name) "
        "FROM pg_replication_slots;")

    # Test physical slot advancing and its durability.  Create a new slot on
    # the primary, not used by any of the standbys. This reserves WAL at
    # creation.
    phys_slot = "phys_slot"
    node_primary.safe_sql(
        f"SELECT pg_create_physical_replication_slot('{phys_slot}', true);")
    # Generate some WAL, and switch to a new segment, used to check that
    # the previous segment is correctly getting recycled as the slot advancing
    # would recompute the minimum LSN calculated across all slots.
    segment_removed = node_primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())")
    advance_wal(node_primary, 1)
    current_lsn = node_primary.safe_sql("SELECT pg_current_wal_lsn();")
    res = node_primary.sql(
        f"SELECT pg_replication_slot_advance('{phys_slot}', "
        f"'{current_lsn}'::pg_lsn);")
    assert res.error_message is None, "slot advancing with physical slot"
    phys_restart_lsn_pre = node_primary.safe_sql(
        "SELECT restart_lsn from pg_replication_slots "
        f"WHERE slot_name = '{phys_slot}';")
    # Slot advance should persist across clean restarts.
    node_primary.restart()
    phys_restart_lsn_post = node_primary.safe_sql(
        "SELECT restart_lsn from pg_replication_slots "
        f"WHERE slot_name = '{phys_slot}';")
    assert phys_restart_lsn_pre == phys_restart_lsn_post, \
        "physical slot advance persists across restarts"

    # Check if the previous segment gets correctly recycled after the
    # server stopped cleanly, causing a shutdown checkpoint to be generated.
    primary_data = node_primary.data_dir
    assert not os.path.isfile(
        os.path.join(primary_data, "pg_wal", segment_removed)), \
        f"WAL segment {segment_removed} recycled after physical slot advancing"

    # NOTE: the final "pg_backup_start() followed by BASE_BACKUP" and
    # "BASE_BACKUP cancellation" checks are not implemented here.  They drive
    # the replication-protocol BASE_BACKUP command and consume/cancel its
    # COPY_OUT stream, which needs libpq COPY-streaming support
    # (PQgetCopyData) that the in-process Session does not provide yet.  This
    # section should be added once that framework support exists.
