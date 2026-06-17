# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Failover slot synchronization.  A primary owns logical slots whose failover
property is enabled (driven by subscriptions created with ``failover = true``).
A physical standby copies those slots either via pg_sync_replication_slots() or
via the slot sync worker (``sync_replication_slots = on``).  The test then:

- verifies failover flips the slot's failover flag on the publisher;
- syncs slots (different output plugins) to the standby and checks the
  ``synced`` / ``inactive_since`` / ``invalidation_reason`` bookkeeping;
- checks that synced slots cannot be decoded, altered or dropped, and that
  sync requires a standby with dbname in primary_conninfo and is rejected on
  cascading standbys;
- checks that failover logical slots wait for the physical slots named in
  ``synchronized_standby_slots`` before handing out changes;
- checks two_phase synchronization;
- promotes the standby and verifies the subscriber resumes from the synced
  slots and that a prepared transaction / a buffered logical message survive;
- checks the slot-sync skip/retry path and its statistics.

Conventions: queries run in-process via libpq Session; each CREATE/ALTER/DROP
SUBSCRIPTION, slot function, CREATE DATABASE, etc. is its own safe_sql call.
"""

import re

from libpq import Session


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def validate_slot_inactive_since(node, slot_name, reference_time):
    """Return the slot's inactive_since.

    Assert it is later than the epoch and than *reference_time*.
    """
    inactive_since = node.safe_sql(
        "SELECT inactive_since FROM pg_replication_slots "
        f"WHERE slot_name = '{slot_name}' AND inactive_since IS NOT NULL"
    )
    assert (
        node.safe_sql(
            f"SELECT '{inactive_since}'::timestamptz > to_timestamp(0) AND "
            f"'{inactive_since}'::timestamptz > '{reference_time}'::timestamptz"
        )
        == "t"
    ), f"last inactive time for slot {slot_name} is valid on node {node.name}"
    return inactive_since


def expect_error(node, sql, pattern, msg, dbname="postgres", replication=None):
    """Run *sql* expecting it to fail; assert *pattern* matches the error.

    Uses an in-process libpq Session.  *replication* may be 'database' (or
    True) to open a replication connection.
    """
    connstr = node.connstr(dbname)
    if replication == "database":
        connstr += " replication=database"
    elif replication:
        connstr += " replication=true"
    with Session(connstr=connstr, libdir=node.libdir) as sess:
        res = sess.query(sql)
        assert res.error_message is not None and re.search(
            pattern, res.error_message
        ), f"{msg}: expected /{pattern}/, got: {res.error_message!r}"


def test_040_standby_failover_slots_sync(create_pg):
    ##################################################
    # Test that when a subscription with failover enabled is created, it will
    # alter the failover property of the corresponding slot on the publisher.
    ##################################################

    # Create publisher
    publisher = create_pg("publisher", allows_streaming="logical", start=False)
    # Disable autovacuum to avoid generating xid during stats update as
    # otherwise the new XID could then be replicated to standby at some random
    # point making slots at primary lag behind standby during slot sync.
    publisher.append_conf("autovacuum = off\nmax_prepared_transactions = 1\n")
    publisher.start()

    publisher.safe_sql("CREATE PUBLICATION regress_mypub FOR ALL TABLES;")

    publisher_connstr = f"host={publisher.host} port={publisher.port} dbname=postgres"

    # Create a subscriber node, wait for sync to complete
    subscriber1 = create_pg("subscriber1", start=False)
    subscriber1.append_conf("max_prepared_transactions = 1\n")
    subscriber1.start()

    # Capture the time before the logical failover slot is created on the
    # primary.  We later call this publisher as primary anyway.
    slot_creation_time_on_primary = publisher.safe_sql("SELECT current_timestamp;")

    # Create a subscription that enables failover.
    subscriber1.safe_sql(
        f"CREATE SUBSCRIPTION regress_mysub1 CONNECTION '{publisher_connstr}' "
        "PUBLICATION regress_mypub WITH (slot_name = lsub1_slot, "
        "copy_data = false, failover = true, enabled = false);"
    )

    # Confirm that the failover flag on the slot is turned on
    assert (
        publisher.safe_sql(
            "SELECT failover from pg_replication_slots WHERE slot_name = 'lsub1_slot';"
        )
        == "t"
    ), "logical slot has failover true on the publisher"

    ##################################################
    # Test that changing the failover property of a subscription updates the
    # corresponding failover property of the slot.
    ##################################################

    # Disable failover
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 SET (failover = false)")

    # Confirm that the failover flag on the slot has now been turned off
    assert (
        publisher.safe_sql(
            "SELECT failover from pg_replication_slots WHERE slot_name = 'lsub1_slot';"
        )
        == "f"
    ), "logical slot has failover false on the publisher"

    # Enable failover
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 SET (failover = true)")

    # Confirm that the failover flag on the slot has now been turned on
    assert (
        publisher.safe_sql(
            "SELECT failover from pg_replication_slots WHERE slot_name = 'lsub1_slot';"
        )
        == "t"
    ), "logical slot has failover true on the publisher"

    ##################################################
    # Test that the failover option cannot be changed for enabled subscriptions.
    ##################################################

    # Enable subscription
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 ENABLE")

    # Disable failover for enabled subscription
    expect_error(
        subscriber1,
        "ALTER SUBSCRIPTION regress_mysub1 SET (failover = false)",
        r'ERROR:  cannot set option "failover" for enabled subscription',
        "altering failover is not allowed for enabled subscription",
    )

    ##################################################
    # Test that pg_sync_replication_slots() cannot be executed on a non-standby
    # server.
    ##################################################

    expect_error(
        publisher,
        "SELECT pg_sync_replication_slots();",
        r"ERROR:  replication slots can only be synchronized to a standby server",
        "cannot sync slots on a non-standby server",
    )

    ##################################################
    # Test logical failover slots corresponding to different plugins can be
    # synced to the standby.
    #
    #   failover slot lsub1_slot  | output_plugin: pgoutput
    #   failover slot lsub2_slot  | output_plugin: test_decoding
    #   physical slot sb1_slot  ----> standby1 (lsub1_slot, lsub2_slot synced)
    ##################################################

    primary = publisher
    primary.backup("backup")

    # Create a standby
    standby1 = create_pg("standby1", start=False)
    standby1.init_from_backup(primary, "backup", has_streaming=1, has_restoring=1)

    # Increase the log_min_messages setting to DEBUG2 on both the standby and
    # primary to debug test failures, if any.
    #
    # Build an unquoted conninfo; PostgresServer.connstr() quotes values and
    # adds dbname, which would break embedding inside primary_conninfo = '...'.
    connstr_1 = f"port={primary.port} host={primary.host}"

    # A primary_conninfo with no application_name shows up as 'walreceiver' in
    # pg_stat_replication.  wait_for_catchup polls the upstream's
    # pg_stat_replication matching application_name IN (name, 'walreceiver'),
    # which would match two physical standbys at once when one is unnamed.  Pin
    # an explicit application_name on standby1 to keep each catchup wait
    # matching exactly one row.
    standby1_appname = " application_name=standby1"
    standby1.append_conf(
        "hot_standby_feedback = on\n"
        "primary_slot_name = 'sb1_slot'\n"
        f"primary_conninfo = '{connstr_1} dbname=postgres{standby1_appname}'\n"
        "log_min_messages = 'debug2'\n"
    )

    primary.append_conf("log_min_messages = 'debug2'\n")
    primary.reload()

    # Drop the subscription to prevent further advancement of the restart_lsn
    # for the lsub1_slot.
    subscriber1.safe_sql("DROP SUBSCRIPTION regress_mysub1;")

    # To ensure that restart_lsn has moved to a recent WAL position, we
    # re-create the lsub1_slot.
    primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('lsub1_slot', 'pgoutput', false, false, true);"
    )
    primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('lsub2_slot', 'test_decoding', false, false, true);"
    )
    primary.safe_sql("SELECT pg_create_physical_replication_slot('sb1_slot');")

    # Start the standby so that slot syncing can begin
    standby1.start()

    # Capture the inactive_since of the slot from the primary.  Note that the
    # slot will be inactive since the corresponding subscription was dropped.
    inactive_since_on_primary = validate_slot_inactive_since(
        primary, "lsub1_slot", slot_creation_time_on_primary
    )

    # Wait for the standby to catch up so that the standby is not lagging behind
    # the failover slots.
    primary.wait_for_replay_catchup(standby1)

    # Synchronize the primary server slots to the standby.
    standby1.safe_sql("SELECT pg_sync_replication_slots();")

    # Confirm that the logical failover slots are created on the standby and are
    # flagged as 'synced'
    assert (
        standby1.safe_sql(
            "SELECT count(*) = 2 FROM pg_replication_slots WHERE slot_name IN "
            "('lsub1_slot', 'lsub2_slot') AND synced AND NOT temporary;"
        )
        == "t"
    ), "logical slots have synced as true on standby"

    # Capture the inactive_since of the synced slot on the standby
    inactive_since_on_standby = validate_slot_inactive_since(
        standby1, "lsub1_slot", slot_creation_time_on_primary
    )

    # Synced slot on the standby must get its own inactive_since
    assert (
        standby1.safe_sql(
            f"SELECT '{inactive_since_on_primary}'::timestamptz < "
            f"'{inactive_since_on_standby}'::timestamptz;"
        )
        == "t"
    ), "synchronized slot has got its own inactive_since"

    ##################################################
    # Test that the synchronized slot will be dropped if the corresponding
    # remote slot on the primary server has been dropped.
    ##################################################

    primary.safe_sql("SELECT pg_drop_replication_slot('lsub2_slot');")

    standby1.safe_sql("SELECT pg_sync_replication_slots();")

    assert (
        standby1.safe_sql(
            "SELECT count(*) = 0 FROM pg_replication_slots WHERE slot_name = 'lsub2_slot';"
        )
        == "t"
    ), "synchronized slot has been dropped"

    ##################################################
    # Test that if the synchronized slot is invalidated while the remote slot is
    # still valid, the slot will be dropped and re-created on the standby by
    # executing pg_sync_replication_slots() again.
    ##################################################

    # Configure the max_slot_wal_keep_size so that the synced slot can be
    # invalidated due to wal removal.
    standby1.append_conf("max_slot_wal_keep_size = 64kB\n")
    standby1.reload()

    # Generate some activity and switch WAL file on the primary
    primary.advance_wal(1)
    primary.safe_sql("CHECKPOINT")
    primary.wait_for_replay_catchup(standby1)

    # Request a checkpoint on the standby to trigger the WAL file(s) removal
    standby1.safe_sql("CHECKPOINT")

    # Check if the synced slot is invalidated
    assert (
        standby1.safe_sql(
            "SELECT invalidation_reason = 'wal_removed' FROM pg_replication_slots "
            "WHERE slot_name = 'lsub1_slot';"
        )
        == "t"
    ), "synchronized slot has been invalidated"

    # Reset max_slot_wal_keep_size to avoid further wal removal
    standby1.append_conf("max_slot_wal_keep_size = -1\n")
    standby1.reload()

    # Capture the time before the logical failover slot is created on the
    # primary.
    slot_creation_time_on_primary = publisher.safe_sql("SELECT current_timestamp;")

    # To ensure that restart_lsn has moved to a recent WAL position, we
    # re-create the lsub1_slot.
    primary.safe_sql("SELECT pg_drop_replication_slot('lsub1_slot');")
    primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('lsub1_slot', 'pgoutput', false, false, true);"
    )

    # Capture the inactive_since of the slot from the primary.  Note that the
    # slot will be inactive since the corresponding subscription was dropped.
    inactive_since_on_primary = validate_slot_inactive_since(
        primary, "lsub1_slot", slot_creation_time_on_primary
    )

    # Wait for the standby to catch up so that the standby is not lagging behind
    # the failover slots.
    primary.wait_for_replay_catchup(standby1)

    log_offset = standby1.log_position()

    # Synchronize the primary server slots to the standby.
    standby1.safe_sql("SELECT pg_sync_replication_slots();")

    # Confirm that the invalidated slot has been dropped.
    standby1.wait_for_log(
        r'dropped replication slot "lsub1_slot" of database with OID [0-9]+',
        log_offset,
    )

    # Confirm that the logical slot has been re-created on the standby and is
    # flagged as 'synced'
    assert (
        standby1.safe_sql(
            "SELECT invalidation_reason IS NULL AND synced AND NOT temporary FROM "
            "pg_replication_slots WHERE slot_name = 'lsub1_slot';"
        )
        == "t"
    ), "logical slot is re-synced"

    # Reset the log_min_messages to the default value.
    primary.append_conf("log_min_messages = 'warning'\n")
    primary.reload()

    standby1.append_conf("log_min_messages = 'warning'\n")
    standby1.reload()

    ##################################################
    # Test that a synchronized slot can not be decoded, altered or dropped by
    # the user
    ##################################################

    # Attempting to perform logical decoding on a synced slot should error
    expect_error(
        standby1,
        "select * from pg_logical_slot_get_changes('lsub1_slot', NULL, NULL);",
        r'ERROR:  cannot use replication slot "lsub1_slot" for logical decoding',
        "logical decoding is not allowed on synced slot",
    )

    # Attempting to alter a synced slot should result in an error
    expect_error(
        standby1,
        "ALTER_REPLICATION_SLOT lsub1_slot (failover);",
        r'ERROR:  cannot alter replication slot "lsub1_slot"',
        "synced slot on standby cannot be altered",
        replication="database",
    )

    # Attempting to drop a synced slot should result in an error
    expect_error(
        standby1,
        "SELECT pg_drop_replication_slot('lsub1_slot');",
        r'ERROR:  cannot drop replication slot "lsub1_slot"',
        "synced slot on standby cannot be dropped",
    )

    ##################################################
    # Test that we cannot synchronize slots if dbname is not specified in the
    # primary_conninfo.
    ##################################################

    standby1.append_conf(f"primary_conninfo = '{connstr_1}{standby1_appname}'\n")

    # Capture the log position before reload to check for walreceiver
    # termination.
    log_offset = standby1.log_position()

    standby1.reload()

    # Wait for the walreceiver to be stopped and restarted after a
    # configuration reload.  When primary_conninfo changes, the walreceiver
    # should be terminated and a new one spawned.
    standby1.wait_for_log(
        r"FATAL: .* terminating walreceiver process due to administrator command",
        log_offset,
    )

    expect_error(
        standby1,
        "SELECT pg_sync_replication_slots();",
        r'ERROR:  replication slot synchronization requires "dbname" to be specified in "primary_conninfo"',
        "cannot sync slots if dbname is not specified in primary_conninfo",
    )

    # Add the dbname back to the primary_conninfo for further tests
    standby1.append_conf(
        f"primary_conninfo = '{connstr_1} dbname=postgres{standby1_appname}'\n"
    )
    standby1.reload()

    ##################################################
    # Test that we cannot synchronize slots to a cascading standby server.
    ##################################################

    # Create a cascading standby
    standby1.backup("backup2")

    cascading_standby = create_pg("cascading_standby", start=False)
    cascading_standby.init_from_backup(
        standby1, "backup2", has_streaming=1, has_restoring=1
    )

    cascading_connstr = f"port={standby1.port} host={standby1.host}"
    cascading_standby.append_conf(
        "hot_standby_feedback = on\n"
        "primary_slot_name = 'cascading_sb_slot'\n"
        f"primary_conninfo = '{cascading_connstr} dbname=postgres'\n"
    )

    standby1.safe_sql(
        "SELECT pg_create_physical_replication_slot('cascading_sb_slot');"
    )

    cascading_standby.start()

    expect_error(
        cascading_standby,
        "SELECT pg_sync_replication_slots();",
        r"ERROR:  cannot synchronize replication slots from a standby server",
        "cannot sync slots to a cascading standby server",
    )

    cascading_standby.stop()

    ##################################################
    # Create a failover slot and advance the restart_lsn to a position where a
    # running transaction exists.  This setup is for testing that the synced
    # slots can achieve the consistent snapshot state starting from the
    # restart_lsn after promotion without losing any data that otherwise would
    # have been received from the primary.
    ##################################################

    primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('snap_test_slot', 'test_decoding', false, false, true);"
    )

    # Wait for the standby to catch up so that the standby is not lagging behind
    # the failover slots.
    primary.wait_for_replay_catchup(standby1)

    standby1.safe_sql("SELECT pg_sync_replication_slots();")

    # Two xl_running_xacts logs are generated here.  When decoding the first
    # log, it only serializes the snapshot, without advancing the restart_lsn
    # to the latest position.  This is because if a transaction is running, the
    # restart_lsn can only move to a position before that transaction.  Hence,
    # the second xl_running_xacts log is needed, the decoding for which allows
    # the restart_lsn to advance to the last serialized snapshot's position
    # (the first log).
    primary.safe_sql(
        """
        BEGIN;
        SELECT txid_current();
        SELECT pg_log_standby_snapshot();
        COMMIT;
        BEGIN;
        SELECT txid_current();
        SELECT pg_log_standby_snapshot();
        COMMIT;
        """
    )

    # Advance the restart_lsn to the position of the first xl_running_xacts log
    # generated above.  Note that there might be concurrent xl_running_xacts
    # logs written by the bgwriter, which could cause the position to be
    # advanced to an unexpected point, but that would be a rare scenario and
    # doesn't affect the test results.
    primary.safe_sql(
        "SELECT pg_replication_slot_advance('snap_test_slot', pg_current_wal_lsn());"
    )

    # Wait for the standby to catch up so that the standby is not lagging behind
    # the failover slots.
    primary.wait_for_replay_catchup(standby1)

    # Log a message that will be consumed on the standby after promotion using
    # the synced slot.  See the test where we promote standby (Promote the
    # standby1 to primary.)
    primary.safe_sql("SELECT pg_logical_emit_message(false, 'test', 'test');")

    # Get the confirmed_flush_lsn for the logical slot snap_test_slot on primary
    confirmed_flush_lsn = primary.safe_sql(
        "SELECT confirmed_flush_lsn from pg_replication_slots WHERE slot_name = 'snap_test_slot';"
    )

    standby1.safe_sql("SELECT pg_sync_replication_slots();")

    # Verify that confirmed_flush_lsn of snap_test_slot is synced to the standby
    assert standby1.poll_query_until(
        f"SELECT '{confirmed_flush_lsn}' = confirmed_flush_lsn from "
        "pg_replication_slots WHERE slot_name = 'snap_test_slot' AND synced AND "
        "NOT temporary;"
    ), "confirmed_flush_lsn of slot snap_test_slot synced to standby"

    ##################################################
    # Test to confirm that the slot synchronization is protected from malicious
    # users.
    ##################################################

    primary.safe_sql("CREATE DATABASE slotsync_test_db")
    primary.wait_for_replay_catchup(standby1)

    standby1.stop()

    # On the primary server, create '=' operator in another schema mapped to
    # inequality function and redirect the queries to use new operator by
    # setting search_path.  The new '=' operator is created with leftarg as
    # 'bigint' and right arg as 'int' to redirect 'count(*) = 1' in slot sync's
    # query to use new '=' operator.
    # Use a one-shot connection (closed immediately) so the primary has no
    # lingering session on slotsync_test_db that would block the later DROP
    # DATABASE.
    setup_sess = primary.connect(dbname="slotsync_test_db")
    try:
        setup_sess.query_safe(
            """

CREATE ROLE repl_role REPLICATION LOGIN;
CREATE SCHEMA myschema;

CREATE FUNCTION myschema.myintne(bigint, int) RETURNS bool as $$
        BEGIN
          RETURN $1 <> $2;
        END;
      $$ LANGUAGE plpgsql immutable;

CREATE OPERATOR myschema.= (
      leftarg    = bigint,
      rightarg   = int,
      procedure  = myschema.myintne);

ALTER DATABASE slotsync_test_db SET SEARCH_PATH TO myschema,pg_catalog;
GRANT USAGE on SCHEMA myschema TO repl_role;
"""
        )
    finally:
        setup_sess.close()

    # Start the standby with changed primary_conninfo.
    standby1.append_conf(
        f"primary_conninfo = '{connstr_1} dbname=slotsync_test_db user=repl_role{standby1_appname}'\n"
    )
    standby1.start()

    # Run the synchronization function.  If the sync flow was not prepared to
    # handle such attacks, it would have failed during the validation of the
    # primary_slot_name itself resulting in
    # ERROR:  slot synchronization requires valid primary_slot_name
    # Use a one-shot connection so standby1 keeps no cached session on the
    # database we are about to drop.
    sync_sess = standby1.connect(dbname="slotsync_test_db")
    try:
        sync_sess.query_safe("SELECT pg_sync_replication_slots();")
    finally:
        sync_sess.close()

    # Reset the dbname and user in primary_conninfo to the earlier values.
    standby1.append_conf(
        f"primary_conninfo = '{connstr_1} dbname=postgres{standby1_appname}'\n"
    )
    standby1.reload()

    # Drop the newly created database.  Wait for the standby's walreceiver to
    # reconnect to the postgres database (after the reload above) so it no
    # longer holds a connection to slotsync_test_db.
    assert primary.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity WHERE datname = 'slotsync_test_db'"
    )
    primary.safe_sql("DROP DATABASE slotsync_test_db;")

    ##################################################
    # Test to confirm that the slot sync worker exits on invalid GUC(s) and
    # get started again on valid GUC(s).
    ##################################################

    log_offset = standby1.log_position()

    # Enable slot sync worker.
    standby1.append_conf("sync_replication_slots = on\n")
    standby1.reload()

    # Confirm that the slot sync worker is able to start.
    standby1.wait_for_log(r"slot sync worker started", log_offset)

    log_offset = standby1.log_position()

    # Disable another GUC required for slot sync.
    standby1.append_conf("hot_standby_feedback = off\n")
    standby1.reload()

    # Confirm that slot sync worker acknowledge the GUC change and logs the msg
    # about wrong configuration.
    standby1.wait_for_log(
        r"slot synchronization worker will restart because of a parameter change",
        log_offset,
    )
    standby1.wait_for_log(
        r'slot synchronization requires "hot_standby_feedback" to be enabled',
        log_offset,
    )

    log_offset = standby1.log_position()

    # Re-enable the required GUC
    standby1.append_conf("hot_standby_feedback = on\n")
    standby1.reload()

    # Confirm that the slot sync worker is able to start now.
    standby1.wait_for_log(r"slot sync worker started", log_offset)

    ##################################################
    # Test to confirm that confirmed_flush_lsn of the logical slot on the
    # primary is synced to the standby via the slot sync worker.
    ##################################################

    # Insert data on the primary
    primary.safe_sql(
        "CREATE TABLE tab_int (a int PRIMARY KEY);\n"
        "INSERT INTO tab_int SELECT generate_series(1, 10);"
    )

    # Subscribe to the new table data and wait for it to arrive
    subscriber1.safe_sql("CREATE TABLE tab_int (a int PRIMARY KEY);")
    subscriber1.safe_sql(
        f"CREATE SUBSCRIPTION regress_mysub1 CONNECTION '{publisher_connstr}' "
        "PUBLICATION regress_mypub WITH (slot_name = lsub1_slot, failover = true, "
        "create_slot = false);"
    )

    subscriber1.wait_for_subscription_sync()

    # Do not allow any further advancement of the confirmed_flush_lsn for the
    # lsub1_slot.
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 DISABLE")

    # Wait for the replication slot to become inactive on the publisher
    assert primary.poll_query_until(
        "SELECT COUNT(*) FROM pg_catalog.pg_replication_slots WHERE "
        "slot_name = 'lsub1_slot' AND active='f'",
        "1",
    )

    # Get the confirmed_flush_lsn for the logical slot lsub1_slot on the primary
    primary_flush_lsn = primary.safe_sql(
        "SELECT confirmed_flush_lsn from pg_replication_slots WHERE slot_name = 'lsub1_slot';"
    )

    # Confirm that confirmed_flush_lsn of lsub1_slot is synced to the standby
    assert standby1.poll_query_until(
        f"SELECT '{primary_flush_lsn}' = confirmed_flush_lsn from "
        "pg_replication_slots WHERE slot_name = 'lsub1_slot' AND synced AND NOT "
        "temporary;"
    ), "confirmed_flush_lsn of slot lsub1_slot synced to standby"

    ##################################################
    # Test that logical failover replication slots wait for the specified
    # physical replication slots to receive the changes first.
    #
    #   primary --(physical)--> standby1 (primary_slot_name = sb1_slot)
    #           --(physical)--> standby2 (primary_slot_name = sb2_slot)
    #           --(logical) --> subscriber1 (failover = true, lsub1_slot)
    #           --(logical) --> subscriber2 (failover = false, lsub2_slot)
    #
    # synchronized_standby_slots = 'sb1_slot'
    ##################################################

    primary.safe_sql("SELECT pg_create_physical_replication_slot('sb2_slot');")

    primary.backup("backup3")

    # Create another standby
    standby2 = create_pg("standby2", start=False)
    standby2.init_from_backup(primary, "backup3", has_streaming=1, has_restoring=1)
    standby2.append_conf("primary_slot_name = 'sb2_slot'\n")
    standby2.start()
    primary.wait_for_replay_catchup(standby2)

    # Configure primary to disallow any logical slots that have enabled failover
    # from getting ahead of the specified physical replication slot (sb1_slot).
    primary.append_conf("synchronized_standby_slots = 'sb1_slot'\n")
    primary.reload()

    # Create another subscriber node without enabling failover, wait for sync
    subscriber2 = create_pg("subscriber2")
    subscriber2.safe_sql("CREATE TABLE tab_int (a int PRIMARY KEY);")
    subscriber2.safe_sql(
        f"CREATE SUBSCRIPTION regress_mysub2 CONNECTION '{publisher_connstr}' "
        "PUBLICATION regress_mypub WITH (slot_name = lsub2_slot);"
    )

    subscriber2.wait_for_subscription_sync()

    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 ENABLE")

    offset = primary.log_position()

    # Stop the standby associated with the specified physical replication slot
    # (sb1_slot) so that the logical replication slot (lsub1_slot) won't receive
    # changes until the standby comes up.
    standby1.stop()

    # Create some data on the primary
    primary_row_count = 20
    primary.safe_sql(
        f"INSERT INTO tab_int SELECT generate_series(11, {primary_row_count});"
    )

    # Wait until the standby2 that's still running gets the data from the primary
    primary.wait_for_replay_catchup(standby2)
    assert (
        standby2.safe_sql(f"SELECT count(*) = {primary_row_count} FROM tab_int;") == "t"
    ), "standby2 gets data from primary"

    # Wait for regress_mysub2 to get the data from the primary.  This
    # subscription was not enabled for failover so it gets the data without
    # waiting for any standbys.
    primary.wait_for_catchup("regress_mysub2")
    assert (
        subscriber2.safe_sql(f"SELECT count(*) = {primary_row_count} FROM tab_int;")
        == "t"
    ), "subscriber2 gets data from primary"

    # Wait until the primary server logs a warning indicating that it is waiting
    # for the sb1_slot to catch up.
    primary.wait_for_log(
        r'replication slot "sb1_slot" specified in parameter "synchronized_standby_slots" does not have active_pid',
        offset,
    )

    # The regress_mysub1 was enabled for failover so it doesn't get the data
    # from primary and keeps waiting for the standby specified in
    # synchronized_standby_slots (sb1_slot aka standby1).
    assert (
        subscriber1.safe_sql(f"SELECT count(*) <> {primary_row_count} FROM tab_int;")
        == "t"
    ), "subscriber1 doesn't get data from primary until standby1 acknowledges changes"

    # Start the standby specified in synchronized_standby_slots (sb1_slot aka
    # standby1) and wait for it to catch up with the primary.
    standby1.start()
    primary.wait_for_replay_catchup(standby1)
    assert (
        standby1.safe_sql(f"SELECT count(*) = {primary_row_count} FROM tab_int;") == "t"
    ), "standby1 gets data from primary"

    # Now that the standby specified in synchronized_standby_slots is up and
    # running, the primary can send the decoded changes to the subscription
    # enabled for failover (i.e. regress_mysub1).  While the standby was down,
    # regress_mysub1 didn't receive any data from the primary.  i.e. the primary
    # didn't allow it to go ahead of standby.
    primary.wait_for_catchup("regress_mysub1")
    assert (
        subscriber1.safe_sql(f"SELECT count(*) = {primary_row_count} FROM tab_int;")
        == "t"
    ), "subscriber1 gets data from primary after standby1 acknowledges changes"

    ##################################################
    # Verify that when using pg_logical_slot_get_changes to consume changes from
    # a logical failover slot, it will also wait for the slots specified in
    # synchronized_standby_slots to catch up.
    ##################################################

    # Stop the standby associated with the specified physical replication slot
    # so that the logical replication slot won't receive changes until the
    # standby slot's restart_lsn is advanced or the slot is removed from the
    # synchronized_standby_slots list.
    primary.safe_sql("TRUNCATE tab_int;")
    primary.wait_for_catchup("regress_mysub1")
    standby1.stop()

    # Disable the regress_mysub1 to prevent the logical walsender from
    # generating more warnings.
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 DISABLE")

    # Wait for the replication slot to become inactive on the publisher
    assert primary.poll_query_until(
        "SELECT COUNT(*) FROM pg_catalog.pg_replication_slots WHERE "
        "slot_name = 'lsub1_slot' AND active = 'f'",
        "1",
    )

    # Create a logical 'test_decoding' replication slot with failover enabled
    primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('test_slot', 'test_decoding', false, false, true);"
    )

    back_q = Session(connstr=primary.connstr(), libdir=primary.libdir)

    # pg_logical_slot_get_changes will be blocked until the standby catches up,
    # hence it needs to be executed in a background session.
    offset = primary.log_position()
    assert back_q.do_async(
        "SELECT pg_logical_slot_get_changes('test_slot', NULL, NULL);"
    )

    # Wait until the primary server logs a warning indicating that it is waiting
    # for the sb1_slot to catch up.
    primary.wait_for_log(
        r'replication slot "sb1_slot" specified in parameter "synchronized_standby_slots" does not have active_pid',
        offset,
    )

    # Remove the standby from the synchronized_standby_slots list and reload the
    # configuration.
    primary.append_conf("synchronized_standby_slots = ''\n")
    primary.reload()

    # Since there are no slots in synchronized_standby_slots, the function
    # pg_logical_slot_get_changes should now return, and the session can be
    # stopped.
    back_q.wait_for_completion()
    back_q.close()

    primary.safe_sql("SELECT pg_drop_replication_slot('test_slot');")

    # Add the physical slot (sb1_slot) back to the synchronized_standby_slots
    # for further tests.
    primary.append_conf("synchronized_standby_slots = 'sb1_slot'\n")
    primary.reload()

    # Enable the regress_mysub1 for further tests
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 ENABLE")

    ##################################################
    # Test that logical replication will wait for the user-created inactive
    # physical slot to catch up until we remove the slot from
    # synchronized_standby_slots.
    ##################################################

    offset = primary.log_position()

    # Create some data on the primary
    primary_row_count = 10
    primary.safe_sql(
        f"INSERT INTO tab_int SELECT generate_series(1, {primary_row_count});"
    )

    # Wait until the primary server logs a warning indicating that it is waiting
    # for the sb1_slot to catch up.
    primary.wait_for_log(
        r'replication slot "sb1_slot" specified in parameter "synchronized_standby_slots" does not have active_pid',
        offset,
    )

    # The regress_mysub1 doesn't get the data from primary because the specified
    # standby slot (sb1_slot) in synchronized_standby_slots is inactive.
    assert (
        subscriber1.safe_sql("SELECT count(*) = 0 FROM tab_int;") == "t"
    ), "subscriber1 doesn't get data as the sb1_slot doesn't catch up"

    # Remove the standby from the synchronized_standby_slots list and reload the
    # configuration.
    primary.append_conf("synchronized_standby_slots = ''\n")
    primary.reload()

    # Since there are no slots in synchronized_standby_slots, the primary server
    # should now send the decoded changes to the subscription.
    primary.wait_for_catchup("regress_mysub1")
    assert (
        subscriber1.safe_sql(f"SELECT count(*) = {primary_row_count} FROM tab_int;")
        == "t"
    ), "subscriber1 gets data from primary after standby1 is removed from the synchronized_standby_slots list"

    # Add the physical slot (sb1_slot) back to the synchronized_standby_slots
    # for further tests.
    primary.append_conf("synchronized_standby_slots = 'sb1_slot'\n")
    primary.reload()

    ##################################################
    # Test the synchronization of the two_phase setting for a subscription with
    # the standby.  Additionally, prepare a transaction before enabling the
    # two_phase option; subsequent tests will verify if it can be correctly
    # replicated to the subscriber after committing it on the promoted standby.
    ##################################################

    standby1.start()

    # Prepare a transaction
    primary.safe_sql(
        """
        BEGIN;
        INSERT INTO tab_int values(0);
        PREPARE TRANSACTION 'test_twophase_slotsync';
        """
    )

    primary.wait_for_replay_catchup(standby1)
    primary.wait_for_catchup("regress_mysub1")

    # Disable the subscription to allow changing the two_phase option.
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 DISABLE")

    # Wait for the replication slot to become inactive on the publisher
    assert primary.poll_query_until(
        "SELECT COUNT(*) FROM pg_catalog.pg_replication_slots WHERE "
        "slot_name = 'lsub1_slot' AND active='f'",
        "1",
    )

    # Set two_phase to true and enable the subscription
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 SET (two_phase = true);")
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 ENABLE;")

    primary.wait_for_catchup("regress_mysub1")

    two_phase_at = primary.safe_sql(
        "SELECT two_phase_at from pg_replication_slots WHERE slot_name = 'lsub1_slot';"
    )

    # Confirm that two_phase setting of lsub1_slot is synced to the standby
    assert standby1.poll_query_until(
        f"SELECT two_phase AND '{two_phase_at}' = two_phase_at from "
        "pg_replication_slots WHERE slot_name = 'lsub1_slot' AND synced AND NOT "
        "temporary;"
    ), "two_phase setting of slot lsub1_slot synced to standby"

    # Confirm that the prepared transaction is not yet replicated to the
    # subscriber.
    assert (
        subscriber1.safe_sql("SELECT count(*) = 0 FROM pg_prepared_xacts;") == "t"
    ), "the prepared transaction is not replicated to the subscriber"

    ##################################################
    # Promote the standby1 to primary.  Confirm that:
    # a) the slot 'lsub1_slot' and 'snap_test_slot' are retained on the new
    #    primary
    # b) logical replication for regress_mysub1 is resumed after failover
    # c) changes from the transaction prepared 'test_twophase_slotsync' can be
    #    consumed from the synced slot once committed on the new primary
    # d) changes can be consumed from the synced slot 'snap_test_slot'
    ##################################################
    primary.wait_for_replay_catchup(standby1)

    # Capture the time before the standby is promoted
    promotion_time_on_primary = standby1.safe_sql("SELECT current_timestamp;")

    standby1.promote()

    # Capture the inactive_since of the synced slot after the promotion.  The
    # expectation here is that the slot gets its inactive_since as part of the
    # promotion.  We do this check before the slot is enabled on the new primary
    # below, otherwise, the slot gets active setting inactive_since to NULL.
    inactive_since_on_new_primary = validate_slot_inactive_since(
        standby1, "lsub1_slot", promotion_time_on_primary
    )

    assert (
        standby1.safe_sql(
            f"SELECT '{inactive_since_on_new_primary}'::timestamptz > "
            f"'{inactive_since_on_primary}'::timestamptz"
        )
        == "t"
    ), "synchronized slot has got its own inactive_since on the new primary after promotion"

    # Update subscription with the new primary's connection info
    standby1_conninfo = f"host={standby1.host} port={standby1.port} dbname=postgres"
    subscriber1.safe_sql(
        f"ALTER SUBSCRIPTION regress_mysub1 CONNECTION '{standby1_conninfo}';"
    )

    # Confirm the synced slot 'lsub1_slot' is retained on the new primary
    assert (
        standby1.safe_sql(
            "SELECT count(*) = 2 FROM pg_replication_slots WHERE slot_name IN "
            "('lsub1_slot', 'snap_test_slot') AND synced AND NOT temporary;"
        )
        == "t"
    ), "synced slot retained on the new primary"

    # Commit the prepared transaction
    standby1.safe_sql("COMMIT PREPARED 'test_twophase_slotsync';")
    standby1.wait_for_catchup("regress_mysub1")

    # Confirm that the prepared transaction is replicated to the subscriber
    assert (
        subscriber1.safe_sql("SELECT count(*) FROM tab_int;") == "11"
    ), "prepared data replicated from the new primary"

    # Insert data on the new primary
    standby1.safe_sql("INSERT INTO tab_int SELECT generate_series(11, 20);")
    standby1.wait_for_catchup("regress_mysub1")

    # Confirm that data in tab_int replicated on the subscriber
    assert (
        subscriber1.safe_sql("SELECT count(*) FROM tab_int;") == "21"
    ), "data replicated from the new primary"

    # Consume the data from the snap_test_slot.  The synced slot should reach a
    # consistent point by restoring the snapshot at the restart_lsn serialized
    # during slot synchronization.
    assert (
        standby1.safe_sql(
            "SELECT count(*) FROM pg_logical_slot_get_changes('snap_test_slot', NULL, NULL) "
            "WHERE data ~ 'message*';"
        )
        == "1"
    ), "data can be consumed using snap_test_slot"

    ##################################################
    # Remove any unnecessary replication slots and clear pending transactions on
    # the primary server to ensure a clean environment.
    ##################################################

    primary.safe_sql("SELECT pg_drop_replication_slot('sb1_slot');")
    primary.safe_sql("SELECT pg_drop_replication_slot('lsub1_slot');")
    primary.safe_sql("SELECT pg_drop_replication_slot('snap_test_slot');")

    subscriber2.safe_sql("DROP SUBSCRIPTION regress_mysub2;")
    subscriber1.safe_sql("DROP SUBSCRIPTION regress_mysub1;")
    subscriber1.safe_sql("TRUNCATE tab_int;")

    # Remove the dropped sb1_slot from the synchronized_standby_slots list and
    # reload the configuration.
    primary.append_conf("synchronized_standby_slots = ''\n")
    primary.reload()

    # Verify that all slots have been removed except the one necessary for
    # standby2, which is needed for further testing.
    assert (
        primary.safe_sql(
            "SELECT count(*) = 0 FROM pg_replication_slots WHERE slot_name != 'sb2_slot';"
        )
        == "t"
    ), "all replication slots have been dropped except the physical slot used by standby2"

    # Commit the pending prepared transaction
    primary.safe_sql("COMMIT PREPARED 'test_twophase_slotsync';")
    primary.wait_for_replay_catchup(standby2)

    ##################################################
    # Test that pg_sync_replication_slots() on the standby skips and retries
    # until the slot becomes sync-ready (when the remote slot catches up with
    # the locally reserved position).
    # Also verify that slotsync skip statistics are correctly updated when the
    # slotsync operation is skipped.
    ##################################################

    # Recreate the slot by creating a subscription on the subscriber, keep it
    # disabled.
    subscriber1.safe_sql("CREATE TABLE push_wal (a int);")
    subscriber1.safe_sql(
        f"CREATE SUBSCRIPTION regress_mysub1 CONNECTION '{publisher_connstr}' "
        "PUBLICATION regress_mypub WITH (slot_name = lsub1_slot, failover = true, "
        "enabled = false);"
    )

    # Create some DDL on the primary so that the slot lags behind the standby
    primary.safe_sql("CREATE TABLE push_wal (a int);")

    # Make sure the DDL changes are synced to the standby
    primary.wait_for_replay_catchup(standby2)

    log_offset = standby2.log_position()

    # Enable standby for slot synchronization
    standby2.append_conf(
        "hot_standby_feedback = on\n"
        f"primary_conninfo = '{connstr_1} dbname=postgres application_name=standby2'\n"
        "log_min_messages = 'debug2'\n"
    )

    standby2.reload()

    # Attempt to synchronize slots using API.  The API will continue retrying
    # synchronization until the remote slot catches up.  The API will not return
    # until this happens, to be able to make further calls, call the API in a
    # background process.
    h = Session(connstr=standby2.connstr(), libdir=standby2.libdir)
    assert h.do_async("SELECT pg_sync_replication_slots();")

    # Confirm that the slot sync is skipped due to the remote slot lagging behind
    standby2.wait_for_log(
        r'could not synchronize replication slot "lsub1_slot"', log_offset
    )

    # Confirm that the slotsync skip reason is updated
    assert (
        standby2.safe_sql(
            "SELECT slotsync_skip_reason FROM pg_replication_slots WHERE slot_name = 'lsub1_slot'"
        )
        == "wal_or_rows_removed"
    ), "check slot sync skip reason"

    # Confirm that the slotsync skip statistics is updated
    assert (
        standby2.safe_sql(
            "SELECT slotsync_skip_count > 0 FROM pg_stat_replication_slots WHERE slot_name = 'lsub1_slot'"
        )
        == "t"
    ), "check slot sync skip count increments"

    # Configure primary to disallow any logical slots that have enabled failover
    # from getting ahead of the specified physical replication slot (sb2_slot).
    primary.append_conf("synchronized_standby_slots = 'sb2_slot'\n")
    primary.reload()

    # Enable the Subscription, so that the remote slot catches up
    subscriber1.safe_sql("ALTER SUBSCRIPTION regress_mysub1 ENABLE")
    subscriber1.wait_for_subscription_sync()

    # Create xl_running_xacts on the primary to speed up restart_lsn advancement.
    primary.safe_sql("SELECT pg_log_standby_snapshot();")

    # Confirm from the log that the slot is sync-ready now.
    standby2.wait_for_log(
        r'newly created replication slot "lsub1_slot" is sync-ready now',
        log_offset,
    )

    h.wait_for_completion()
    h.close()
