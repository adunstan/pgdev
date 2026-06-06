# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test low-level backup method by using pg_backup_start() and pg_backup_stop()
to create backups.
"""

import os
import shutil

from pypg.util import append_to_file, copy_live_tree


def test_042_low_level_backup(create_pg):
    # Start primary node with archiving.
    node_primary = create_pg(
        "primary", start=False, has_archiving=True, allows_streaming=True
    )
    node_primary.start()

    # Start backup.
    backup_name = "backup1"
    # The backup state is per-connection, so pg_backup_start() and
    # pg_backup_stop() must run on the same persistent libpq session, which is
    # kept open between the two calls.
    psql = node_primary.connect()

    psql.do("SET client_min_messages TO WARNING")
    psql.query("select pg_backup_start('test label')")

    # Copy files.
    backup_dir = os.path.join(node_primary.backup_dir, backup_name)

    # Copying a running primary's data dir races with the server (e.g. WAL
    # archive_status flags come and go), so use a copy that tolerates files
    # that disappear mid-copy.
    copy_live_tree(node_primary.data_dir, backup_dir)

    # Cleanup some files/paths that should not be in the backup.  There is no
    # attempt to handle all the exclusions done by pg_basebackup here, in part
    # because these are not required, but also to keep the test simple.
    #
    # Also remove pg_control because it needs to be copied later.
    os.unlink(os.path.join(backup_dir, "postmaster.pid"))
    os.unlink(os.path.join(backup_dir, "postmaster.opts"))
    os.unlink(os.path.join(backup_dir, "global", "pg_control"))

    shutil.rmtree(os.path.join(backup_dir, "pg_wal"))
    os.mkdir(os.path.join(backup_dir, "pg_wal"))

    # Create a table that will be used to verify that recovery started at the
    # correct location, rather than a location recorded in the control file.
    node_primary.safe_sql("create table canary (id int)")

    # Advance the checkpoint location in pg_control past the location where the
    # backup started.  Switch WAL to make it really clear that the location is
    # different and to put the checkpoint in a new WAL segment.
    segment_name = node_primary.safe_sql("select pg_walfile_name(pg_switch_wal())")

    # Ensure that the segment just switched from is archived.  The follow-up
    # tests depend on its presence to begin recovery.
    assert node_primary.poll_query_until(
        "SELECT last_archived_wal FROM pg_stat_archiver", segment_name
    ), "Timed out while waiting for archiving of switched segment to finish"

    node_primary.safe_sql("checkpoint")

    # Copy pg_control last so it contains the new checkpoint.
    shutil.copy(
        os.path.join(node_primary.data_dir, "global", "pg_control"),
        os.path.join(backup_dir, "global", "pg_control"),
    )

    # Save the name segment that will be archived by pg_backup_stop().
    # This is copied to the pg_wal directory of the node whose recovery
    # is done without a backup_label.
    stop_segment_name = node_primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())"
    )

    # Stop backup and get backup_label, the last segment is archived.
    backup_label = psql.query_oneval("select labelfile from pg_backup_stop()")

    psql.close()

    # Rather than writing out backup_label, try to recover the backup without
    # backup_label to demonstrate that recovery will not work correctly without
    # it, i.e. the canary table will be missing and the cluster will be
    # corrupted.  Provide only the WAL segment that recovery will think it
    # needs.
    #
    # The point of this test is to explicitly demonstrate that backup_label is
    # being used in a later test to get the correct recovery info.
    node_replica = create_pg("replica_fail", start=False)
    node_replica.init_from_backup(node_primary, backup_name)
    node_replica.append_conf("archive_mode = off")

    canary_query = "select count(*) from pg_class where relname = 'canary'"

    shutil.copy(
        os.path.join(node_primary.archive_dir, stop_segment_name),
        os.path.join(node_replica.data_dir, "pg_wal", stop_segment_name),
    )

    node_replica.start()

    assert node_replica.safe_sql(canary_query) == "0", "canary is missing"

    # Check log to ensure that crash recovery was used as there is no
    # backup_label.
    assert node_replica.log_contains(
        "database system was not properly shut down; automatic recovery in progress"
    ), "verify backup recovery performed with crash recovery"

    node_replica.teardown()

    # Save backup_label into the backup directory and recover using the
    # primary's archive.  This time recovery will succeed and the canary table
    # will be present.
    append_to_file(os.path.join(backup_dir, "backup_label"), backup_label)

    node_replica = create_pg("replica_success", start=False)
    node_replica.init_from_backup(node_primary, backup_name, has_restoring=True)
    node_replica.start()

    assert node_replica.safe_sql(canary_query) == "1", "canary is present"

    # Check log to ensure that backup_label was used for recovery.
    assert node_replica.log_contains(
        "starting backup recovery with redo LSN"
    ), "verify backup recovery performed with backup_label"
