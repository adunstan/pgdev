# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Testing streaming replication where standby is promoted and a new cascading
standby (without WAL) is connected to the promoted standby.  Both archiving
and streaming are enabled, but only the history file is available from the
archive, so the WAL files all have to be streamed.  Test that the cascading
standby can follow the new primary (promoted standby).
"""

import os
import sys
import tempfile


def test_025_stuck_on_old_timeline(create_pg):
    # Initialize primary node.
    #
    # Set up an archive command that will copy the history file but not the WAL
    # files. No real archive command should behave this way; the point is to
    # simulate a race condition where the new cascading standby starts up after
    # the timeline history file reaches the archive but before any of the WAL
    # files get there.
    node_primary = create_pg(
        "primary", start=False, allows_streaming=True, has_archiving=True)

    # Write a small script for cp_history_files: it copies the source to the
    # target only when the source path contains "history" (i.e. timeline
    # history files), dropping everything else.  Use a Python script run by the
    # interpreter, so the archive_command works on every platform -- a
    # "#!/bin/sh" script would not run on Windows.
    archivedir_primary = node_primary.archive_dir.replace("\\", "/")
    fd, cp_history_files = tempfile.mkstemp(
        prefix="cp_history_files", suffix=".py")
    os.write(fd, b"""\
import shutil
import sys

src, dst = sys.argv[1], sys.argv[2]
if "history" in src:
    shutil.copyfile(src, dst)
""")
    os.close(fd)

    # Override the default archive_command with our history-only copy script.
    # Forward slashes keep the command valid in postgresql.conf on Windows.
    python = sys.executable.replace("\\", "/")
    script = cp_history_files.replace("\\", "/")
    node_primary.append_conf(f"""
archive_command = '"{python}" "{script}" "%p" "{archivedir_primary}/%f"'
wal_keep_size=128MB
""")
    node_primary.start()

    # Take backup from primary
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Create streaming standby linking to primary
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(
        node_primary, backup_name, has_streaming=True)
    node_standby.start()

    # Take backup of standby, use -Xnone so that pg_wal is empty.
    node_standby.backup(backup_name, backup_options=["-Xnone"])

    # Create cascading standby but don't start it yet.
    # Must set up both streaming and archiving.
    node_cascade = create_pg("cascade", start=False)
    node_cascade.init_from_backup(node_standby, backup_name, has_streaming=True)
    node_cascade.enable_restoring(node_primary)
    node_cascade.append_conf("""
recovery_target_timeline='latest'
""")

    # Promote the standby.
    node_standby.promote()

    # Wait for promotion to complete
    assert node_standby.poll_query_until("SELECT NOT pg_is_in_recovery();"), \
        "Timed out while waiting for promotion"

    # Find next WAL segment to be archived
    walfile_to_be_archived = node_standby.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn());")

    # Make WAL segment eligible for archival
    node_standby.safe_sql("SELECT pg_switch_wal()")

    # Wait until the WAL segment has been archived.
    # Since the history file gets created on promotion and is archived before any
    # WAL segment, this is enough to guarantee that the history file was
    # archived.
    archive_wait_query = (
        f"SELECT '{walfile_to_be_archived}' <= last_archived_wal "
        "FROM pg_stat_archiver")
    assert node_standby.poll_query_until(archive_wait_query), \
        "Timed out while waiting for WAL segment to be archived"

    # Start cascade node
    node_cascade.start()

    # Create some content on promoted standby and check its presence on the
    # cascading standby.
    node_standby.safe_sql("CREATE TABLE tab_int AS SELECT 1 AS a")

    # Wait for the replication to catch up
    node_standby.wait_for_catchup(node_cascade)

    # Check that cascading standby has the new content
    result = node_cascade.safe_sql("SELECT count(*) FROM tab_int")
    print(f"cascade: {result}")
    assert result == "1", "check streamed content on cascade standby"
