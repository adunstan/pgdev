# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Verify that an incremental backup correctly copies whole database files when
needed.  When a database is dropped and recreated (with the same OID) between
the full and incremental backups, pg_combinebackup must take the whole new
database file rather than try to apply an incremental delta, so the restored
cluster reflects the recreated (empty) database.
"""

import os
import shutil

from libpq import ExecStatusType


def test_006_db_file_copy(create_pg, tmp_path):
    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_COMBINEBACKUP_MODE") or "--copy"
    print(f"# testing using mode {mode}")

    # Set up a new database instance.
    primary = create_pg("primary", start=False,
                        has_archiving=True, allows_streaming=True)
    primary.append_conf("summarize_wal = on")
    primary.start()

    # Initial setup.
    primary.safe_sql(
        "CREATE DATABASE lakh OID = 100000 STRATEGY = FILE_COPY")
    primary.safe_sql("CREATE TABLE t1 (a int)", dbname="lakh")

    # Take a full backup.
    backup1path = os.path.join(primary.backup_dir, "backup1")
    primary.command_ok(
        ["pg_basebackup",
         "--pgdata", backup1path,
         "--no-sync",
         "--checkpoint", "fast"],
        "full backup")

    # Now make some database changes.  DROP/CREATE DATABASE cannot run inside
    # a transaction block, so issue them as separate top-level statements (the
    # in-process Session wraps a multi-statement string in one transaction).
    #
    # The CREATE TABLE above opened (and the framework cached) a session
    # connected to "lakh"; close it so it does not block DROP DATABASE.
    lakh_sess = primary._sessions.pop("lakh", None)
    if lakh_sess is not None:
        lakh_sess.close()
    primary.safe_sql("DROP DATABASE lakh;")
    primary.safe_sql(
        "CREATE DATABASE lakh OID = 100000 STRATEGY = FILE_COPY")

    # Take an incremental backup.
    backup2path = os.path.join(primary.backup_dir, "backup2")
    primary.command_ok(
        ["pg_basebackup",
         "--pgdata", backup2path,
         "--no-sync",
         "--checkpoint", "fast",
         "--incremental", os.path.join(backup1path, "backup_manifest")],
        "incremental backup")

    # Recover the incremental backup.
    #
    # The framework's init_from_backup does not support incremental combine, so
    # run pg_combinebackup directly to produce the combined data directory,
    # then build a verification node on top of it (as test_010 does for its
    # restore-and-query checks).
    restore = create_pg("restore", start=False)
    combined = restore.data_dir
    shutil.rmtree(combined, ignore_errors=True)
    restore.command_ok(
        ["pg_combinebackup", backup1path, backup2path,
         "--output", combined, mode],
        "combine backups")

    # init() already wrote our connection settings (port, socket dir) to the
    # original data dir's postgresql.conf, which pg_combinebackup overwrote.
    # Append them again to the combined data dir before starting.
    restore.append_conf("\n".join([
        "",
        f"port = {restore.port}",
        "listen_addresses = ''",
        f"unix_socket_directories = '{restore.host}'",
        "",
    ]))
    restore.start()

    # Query the DB.  The table created before the full backup must be gone,
    # because the database was dropped and recreated between backups.
    res = restore.sql("SELECT * FROM t1", dbname="lakh")
    assert res.status == ExecStatusType.PGRES_FATAL_ERROR, \
        "SELECT * FROM t1: query should fail"
    assert res.psqlout == "", "SELECT * FROM t1: no stdout"
    assert 'relation "t1" does not exist' in (res.error_message or ""), \
        "SELECT * FROM t1: stderr missing table"
