# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests related to WAL archiving and recovery."""

import os
import re

from libpq.errors import QueryError
from pypg.util import slurp_file


def test_020_archive_status(create_pg):
    primary = create_pg(
        "primary", start=False, has_archiving=True, allows_streaming=True)
    primary.append_conf("autovacuum = off")
    primary.start()
    primary_data = primary.data_dir

    # Temporarily use an archive_command value to make the archiver fail,
    # knowing that archiving is enabled.  Note that we cannot use a command
    # that does not exist as in this case the archiver process would just exit
    # without reporting the failure to pg_stat_archiver.  This also cannot
    # use a plain "false" as that's unportable on Windows.  So, instead, as
    # a portable solution, use an archive command based on a command known to
    # work but will fail: copy with an incorrect original path.
    incorrect_command = 'cp "%p_does_not_exist" "%f_does_not_exist"'
    primary.safe_sql(
        f"ALTER SYSTEM SET archive_command TO '{incorrect_command}'")
    primary.safe_sql("SELECT pg_reload_conf()")

    # Save the WAL segment currently in use and switch to a new segment.
    # This will be used to track the activity of the archiver.
    segment_name_1 = primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())")
    segment_path_1 = f"pg_wal/archive_status/{segment_name_1}"
    segment_path_1_ready = f"{segment_path_1}.ready"
    segment_path_1_done = f"{segment_path_1}.done"
    primary.safe_sql("""
        CREATE TABLE mine AS SELECT generate_series(1,10) AS x;
        SELECT pg_switch_wal();
        CHECKPOINT;
    """)

    # Wait for an archive failure.
    assert primary.poll_query_until(
        "SELECT failed_count > 0 FROM pg_stat_archiver"), \
        "Timed out while waiting for archiving to fail"
    assert os.path.isfile(os.path.join(primary_data, segment_path_1_ready)), \
        (f".ready file exists for WAL segment {segment_name_1} "
         "waiting to be archived")
    assert not os.path.isfile(os.path.join(primary_data, segment_path_1_done)), \
        (f".done file does not exist for WAL segment {segment_name_1} "
         "waiting to be archived")

    assert primary.safe_sql("""
            SELECT archived_count, last_failed_wal
            FROM pg_stat_archiver
        """) == f"0|{segment_name_1}", \
        f"pg_stat_archiver failed to archive {segment_name_1}"

    # Crash the cluster for the next test in charge of checking that
    # non-archived WAL segments are not removed.
    primary.stop("immediate")

    # Recovery tests for the archiving with a standby partially check
    # the recovery behavior when restoring a backup taken using a
    # snapshot with no pg_backup_start/stop.  In this situation,
    # the recovered standby should enter first crash recovery then
    # switch to regular archive recovery.  Note that the base backup
    # is taken here so as archive_command will fail.  This is necessary
    # for the assumptions of the tests done with the standbys below.
    primary.backup_fs_cold("backup")

    primary.start()
    assert os.path.isfile(os.path.join(primary_data, segment_path_1_ready)), \
        (f".ready file for WAL segment {segment_name_1} still exists "
         "after crash recovery on primary")

    # Allow WAL archiving again and wait for a success.
    primary.safe_sql("ALTER SYSTEM RESET archive_command")
    primary.safe_sql("SELECT pg_reload_conf()")

    assert primary.poll_query_until(
        "SELECT archived_count FROM pg_stat_archiver", "1"), \
        "Timed out while waiting for archiving to finish"

    assert not os.path.isfile(os.path.join(primary_data, segment_path_1_ready)), \
        f".ready file for archived WAL segment {segment_name_1} removed"

    assert os.path.isfile(os.path.join(primary_data, segment_path_1_done)), \
        f".done file for archived WAL segment {segment_name_1} exists"

    assert primary.safe_sql(
        "SELECT last_archived_wal FROM pg_stat_archiver") == segment_name_1, \
        ("archive success reported in pg_stat_archiver for WAL segment "
         f"{segment_name_1}")

    # Create some WAL activity and a new checkpoint so as the next standby can
    # create a restartpoint.  As this standby starts in crash recovery because
    # of the cold backup taken previously, it needs a clean restartpoint to
    # deal with existing status files.
    segment_name_2 = primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())")
    segment_path_2 = f"pg_wal/archive_status/{segment_name_2}"
    segment_path_2_ready = f"{segment_path_2}.ready"
    segment_path_2_done = f"{segment_path_2}.done"
    primary.safe_sql("""
        INSERT INTO mine SELECT generate_series(10,20) AS x;
        CHECKPOINT;
    """)

    # Switch to a new segment and use the returned LSN to make sure that
    # standbys have caught up to this point.
    primary_lsn = primary.safe_sql("""
        SELECT pg_switch_wal();
    """)

    assert primary.poll_query_until(
        "SELECT last_archived_wal FROM pg_stat_archiver", segment_name_2), \
        "Timed out while waiting for archiving to finish"

    # Test standby with archive_mode = on.
    standby1 = create_pg("standby", start=False)
    standby1.init_from_backup(primary, "backup", has_restoring=True)
    standby1.append_conf("archive_mode = on")
    standby1_data = standby1.data_dir
    standby1.start()

    # Wait for the replay of the segment switch done previously, ensuring
    # that all segments needed are restored from the archives.
    assert standby1.poll_query_until(
        f"SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '{primary_lsn}') >= 0"
    ), "Timed out while waiting for xlog replay on standby1"

    standby1.safe_sql("CHECKPOINT")

    # Recovery with archive_mode=on does not keep .ready signal files inherited
    # from backup.  Note that this WAL segment existed in the backup.
    assert not os.path.isfile(os.path.join(standby1_data, segment_path_1_ready)), \
        (f".ready file for WAL segment {segment_name_1} present in backup got "
         "removed with archive_mode=on on standby")

    # Recovery with archive_mode=on should not create .ready files.
    # Note that this segment did not exist in the backup.
    assert not os.path.isfile(os.path.join(standby1_data, segment_path_2_ready)), \
        (f".ready file for WAL segment {segment_name_2} not created on standby "
         "when archive_mode=on on standby")

    # Recovery with archive_mode = on creates .done files.
    assert os.path.isfile(os.path.join(standby1_data, segment_path_2_done)), \
        (f".done file for WAL segment {segment_name_2} created when "
         "archive_mode=on on standby")

    # Test recovery with archive_mode = always, which should always keep
    # .ready files if archiving is enabled, though here we want the archive
    # command to fail to persist the .ready files.  Note that this node
    # has inherited the archive command of the previous cold backup that
    # will cause archiving failures.
    standby2 = create_pg("standby2", start=False)
    standby2.init_from_backup(primary, "backup", has_restoring=True)
    standby2.append_conf("archive_mode = always")
    standby2_data = standby2.data_dir
    standby2.start()

    # Wait for the replay of the segment switch done previously, ensuring
    # that all segments needed are restored from the archives.
    assert standby2.poll_query_until(
        f"SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '{primary_lsn}') >= 0"
    ), "Timed out while waiting for xlog replay on standby2"

    standby2.safe_sql("CHECKPOINT")

    assert os.path.isfile(os.path.join(standby2_data, segment_path_1_ready)), \
        (f".ready file for WAL segment {segment_name_1} existing in backup is "
         "kept with archive_mode=always on standby")

    assert os.path.isfile(os.path.join(standby2_data, segment_path_2_ready)), \
        (f".ready file for WAL segment {segment_name_2} created with "
         "archive_mode=always on standby")

    # Reset statistics of the archiver for the next checks.
    standby2.safe_sql("SELECT pg_stat_reset_shared('archiver')")

    # Now crash the cluster to check that recovery step does not
    # remove non-archived WAL segments on a standby where archiving
    # is enabled.
    standby2.stop("immediate")
    standby2.start()

    assert os.path.isfile(os.path.join(standby2_data, segment_path_1_ready)), \
        ("WAL segment still ready to archive after crash recovery on standby "
         "with archive_mode=always")

    # Allow WAL archiving again, and wait for the segments to be archived.
    standby2.safe_sql("ALTER SYSTEM RESET archive_command")
    standby2.safe_sql("SELECT pg_reload_conf()")
    assert standby2.poll_query_until(
        "SELECT last_archived_wal FROM pg_stat_archiver", segment_name_2), \
        "Timed out while waiting for archiving to finish"

    assert standby2.safe_sql(
        "SELECT archived_count FROM pg_stat_archiver") == "2", \
        "correct number of WAL segments archived from standby"

    assert (not os.path.isfile(os.path.join(standby2_data, segment_path_1_ready))
            and not os.path.isfile(os.path.join(standby2_data, segment_path_2_ready))), \
        ".ready files removed after archive success with archive_mode=always on standby"

    assert (os.path.isfile(os.path.join(standby2_data, segment_path_1_done))
            and os.path.isfile(os.path.join(standby2_data, segment_path_2_done))), \
        ".done files created after archive success with archive_mode=always on standby"

    # Check that the archiver process calls the shell archive module's shutdown
    # callback.
    standby2.append_conf("log_min_messages = debug1")
    standby2.reload()

    # Run a query to make sure that the reload has taken effect.
    standby2.safe_sql("SELECT 1")
    log_location = standby2.log_position()

    standby2.stop()
    logfile = slurp_file(standby2.logfile, log_location)
    assert re.search(r"archiver process shutting down", logfile), \
        "check shutdown callback of shell archive module"

    # Test that we can enter and leave backup mode without crashes.
    #
    # The third statement, with an oversized backup label, must fail
    # gracefully.  Here the equivalent is a
    # single in-process query whose final statement errors with "backup label
    # too long".
    try:
        primary.safe_sql(
            "SELECT pg_backup_start('onebackup'); "
            "SELECT pg_backup_stop(); "
            "SELECT pg_backup_start(repeat('x', 1026))")
        raise AssertionError("psql fails correctly")
    except QueryError as exc:
        assert re.search(r"backup label too long", str(exc)), \
            "pg_backup_start fails gracefully"

    primary.safe_sql(
        "SELECT pg_backup_start('onebackup'); SELECT pg_backup_stop();")
    primary.safe_sql("SELECT pg_backup_start('twobackup')")
