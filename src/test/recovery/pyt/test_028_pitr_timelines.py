# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Test PITR recovery where the target point is physically in a WAL segment
with a higher TLI than the target point's TLI.
"""

# Test recovering to a point-in-time using WAL archive, such that the
# target point is physically in a WAL segment with a higher TLI than
# the target point's TLI.  For example, imagine that the following WAL
# segments exist in the WAL archive:
#
#      000000010000000000000001
#      000000010000000000000002
#      000000020000000000000003
#
# The timeline switch happened in the middle of WAL segment 3, but it
# was never archived on timeline 1.  The first half of
# 000000020000000000000003 contains the WAL from timeline 1 up to the
# point where the timeline switch happened.  If you now perform
# archive recovery with recovery target point in that first half of
# segment 3, archive recovery will find the WAL up to that point in
# segment 000000020000000000000003, but it will not follow the
# timeline switch to timeline 2, and creates a timeline switching
# end-of-recovery record with TLI 1 -> 3.  That's what this test case
# tests.
#
# The comments below contain lists of WAL segments at different points
# in the tests, to make it easier to follow along.  They are correct
# as of this writing, but the exact WAL segment numbers could change
# if the backend logic for when it switches to a new segment changes.
# The actual checks are not sensitive to that.


def test_028_pitr_timelines(create_pg):
    # Initialize and start primary node with WAL archiving
    node_primary = create_pg(
        "primary", start=False, has_archiving=True, allows_streaming=True)
    node_primary.start()

    # Take a backup.
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Workload with some transactions, and the target restore point.
    #
    # Each statement must auto-commit separately, but our in-process
    # safe_sql runs a multi-statement string as a
    # single implicit transaction, which would leave the whole workload
    # uncommitted at restore point 'rp' (so 'foo' would not yet exist on PITR).
    # Issue each statement separately to reproduce psql's per-statement
    # autocommit, so 'foo' with row 1 is committed before 'rp'.
    node_primary.safe_sql("CREATE TABLE foo(i int);")
    node_primary.safe_sql("INSERT INTO foo VALUES(1);")
    node_primary.safe_sql("SELECT pg_create_restore_point('rp');")
    node_primary.safe_sql("INSERT INTO foo VALUES(2);")

    # Contents of the WAL archive at this point:
    #
    # 000000010000000000000001
    # 000000010000000000000002
    # 000000010000000000000002.00000028.backup
    #
    # The operations on the test table and the restore point went into WAL
    # segment 3, but it hasn't been archived yet.

    # Start a standby node, and wait for it to catch up.
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(
        node_primary, backup_name,
        standby=True,
        has_streaming=True,
        has_restoring=False)
    # Archiving is a no-op in init_from_backup, which only acts on
    # has_streaming/has_restoring.  The standby inherits
    # the primary's archive_command -- which targets the primary's archive_dir
    # -- from the postgresql.conf copied by pg_basebackup.  We only override
    # archive_mode to 'always' so the standby keeps archiving into the
    # primary's archive_dir, where the PITR nodes below restore from.
    node_standby.append_conf("archive_mode = always")
    node_standby.start()
    node_primary.wait_for_catchup(node_standby)

    # Check that it's really caught up.
    result = node_standby.safe_sql("SELECT max(i) FROM foo;")
    assert result == "2", "check table contents after archive recovery"

    # Kill the old primary, before it archives the most recent WAL segment that
    # contains all the INSERTs.
    node_primary.stop("immediate")

    # Promote the standby, and switch WAL so that it archives a WAL segment
    # that contains all the INSERTs, on a new timeline.
    node_standby.promote()

    # Find next WAL segment to be archived.
    node_standby.safe_sql("SELECT pg_walfile_name(pg_current_wal_lsn());")

    # Make WAL segment eligible for archival
    node_standby.safe_sql("SELECT pg_switch_wal()")

    # We don't need the standby anymore, request shutdown. The server will
    # finish archiving all the WAL on timeline 2 before it exits.
    node_standby.stop()

    # Contents of the WAL archive at this point:
    #
    # 000000010000000000000001
    # 000000010000000000000002
    # 000000010000000000000002.00000028.backup
    # 000000010000000000000003.partial
    # 000000020000000000000003
    # 00000002.history
    #
    # The operations on the test table and the restore point are in
    # segment 3.  They are part of timeline 1, but were not archived by
    # the primary yet.  However, they were copied into the beginning of
    # segment 000000020000000000000003, before the timeline switching
    # record.  (They are also present in the
    # 000000010000000000000003.partial file, but .partial files are not
    # used automatically.)

    # Now test PITR to the recovery target.  It should find the WAL in
    # segment 000000020000000000000003, but not follow the timeline switch
    # to timeline 2.
    node_pitr = create_pg("node_pitr", start=False)
    node_pitr.init_from_backup(
        node_primary, backup_name,
        standby=False,
        has_restoring=True)
    node_pitr.append_conf("""
recovery_target_name = 'rp'
recovery_target_action = 'promote'
""")

    node_pitr.start()

    # Wait until recovery finishes.
    assert node_pitr.poll_query_until("SELECT pg_is_in_recovery() = 'f';"), \
        "Timed out while waiting for PITR promotion"

    # Check that we see the data we expect.
    result = node_pitr.safe_sql("SELECT max(i) FROM foo;")
    assert result == "1", "check table contents after point-in-time recovery"

    # Insert a row so that we can check later that we successfully recover
    # back to this timeline.
    node_pitr.safe_sql("INSERT INTO foo VALUES(3);")

    # Wait for the archiver to be running.  The startup process might have yet
    # to exit, in which case the postmaster has not started the archiver.  If
    # we stop() without an archiver, the archive will be incomplete.
    assert node_pitr.poll_query_until(
        "SELECT true FROM pg_stat_activity WHERE backend_type = 'archiver';"), \
        "Timed out while waiting for archiver to start"

    # Stop the node.  This archives the last segment.
    node_pitr.stop()

    # Test archive recovery on the timeline created by the PITR.  This
    # replays the end-of-recovery record that switches from timeline 1 to
    # 3.
    node_pitr2 = create_pg("node_pitr2", start=False)
    node_pitr2.init_from_backup(
        node_primary, backup_name,
        standby=False,
        has_restoring=True)
    node_pitr2.append_conf("""
recovery_target_action = 'promote'
""")

    node_pitr2.start()

    # Wait until recovery finishes.
    assert node_pitr2.poll_query_until("SELECT pg_is_in_recovery() = 'f';"), \
        "Timed out while waiting for PITR promotion"

    # Verify that we can see the row inserted after the PITR.
    result = node_pitr2.safe_sql("SELECT max(i) FROM foo;")
    assert result == "3", "check table contents after point-in-time recovery"
