# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Take a full backup and an incremental backup (with a user-defined
tablespace), combine the incremental with the full using pg_combinebackup,
then perform PITR to the same LSN from both the full backup and the combined
backup.  A logical dump (pg_dumpall) of each restored server must match, which
demonstrates that the combined backup reconstructs the same database state as
the original full backup.

FRAMEWORK NOTES:

  * The framework's init_from_backup does not support combining with a prior
    backup or remapping tablespaces.  This test therefore drives
    pg_combinebackup directly and performs the data-directory copy plus
    tablespace symlink relocation itself (see _restore_node).

  * --copy/--clone/--link is chosen via the PG_TEST_PG_COMBINEBACKUP_MODE
    environment variable (default --copy).
"""

import os
import re
import shutil

from pypg.util import dir_symlink, remove_dir_symlink


def _restore_node(node, backup_path, ts_oid, ts_dest):
    """Bring a node's data dir up from a plain-format backup at *backup_path*.

    Copies the backup tree into the node's data directory, relocates the
    user-defined tablespace into *ts_dest* and repoints the pg_tblspc/<oid>
    symlink at it,
    and writes the minimal connection configuration.  Recovery configuration
    (restore_command, recovery_target_*) is appended separately by the caller.
    """
    data_path = node.data_dir
    if os.path.isdir(data_path):
        shutil.rmtree(data_path)
    shutil.copytree(backup_path, data_path, symlinks=True)
    os.chmod(data_path, 0o700)

    # Relocate the tablespace: copy its contents to ts_dest and repoint the
    # pg_tblspc/<oid> symlink.  In a plain-format backup the symlink points at
    # wherever the backup relocated the tablespace; we move it under this
    # node's own area so the two restored nodes don't collide.
    link = os.path.join(data_path, "pg_tblspc", ts_oid)
    src = os.path.realpath(link)
    shutil.copytree(src, ts_dest, symlinks=True)
    remove_dir_symlink(link)
    dir_symlink(ts_dest, link)

    node.append_conf(
        "\n".join(
            [
                "",
                f"port = {node.port}",
                "listen_addresses = ''",
                f"unix_socket_directories = '{node.host}'",
                "",
            ]
        )
    )


def test_002_compare_backups(create_pg, tempdir_short):
    # Use a short tempdir: the tablespace symlinks below are written into a
    # base backup's tar stream, whose target length is limited.
    tempdir = tempdir_short

    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_COMBINEBACKUP_MODE") or "--copy"
    print(f"# testing using mode {mode}")

    # Set up a new database instance.
    primary = create_pg("primary", start=False, has_archiving=True,
                        allows_streaming=True)
    primary.append_conf("summarize_wal = on")
    primary.start()
    tsprimary = os.path.join(tempdir, "ts")
    os.mkdir(tsprimary)

    # Create some test tables, each containing one row of data, plus a whole
    # extra database.  CREATE DATABASE and CREATE TABLESPACE cannot run inside
    # a transaction block, so issue them as separate statements (the
    # in-process libpq session groups a multi-statement string into one
    # implicit transaction, unlike psql).
    primary.safe_sql("""
CREATE TABLE will_change (a int, b text);
INSERT INTO will_change VALUES (1, 'initial test row');
CREATE TABLE will_grow (a int, b text);
INSERT INTO will_grow VALUES (1, 'initial test row');
CREATE TABLE will_shrink (a int, b text);
INSERT INTO will_shrink VALUES (1, 'initial test row');
CREATE TABLE will_get_vacuumed (a int, b text);
INSERT INTO will_get_vacuumed VALUES (1, 'initial test row');
CREATE TABLE will_get_dropped (a int, b text);
INSERT INTO will_get_dropped VALUES (1, 'initial test row');
CREATE TABLE will_get_rewritten (a int, b text);
INSERT INTO will_get_rewritten VALUES (1, 'initial test row');
""")
    primary.safe_sql("CREATE DATABASE db_will_get_dropped;")
    primary.safe_sql(f"CREATE TABLESPACE ts1 LOCATION '{tsprimary}';")
    primary.safe_sql("""
CREATE TABLE will_not_change_in_ts (a int, b text) TABLESPACE ts1;
INSERT INTO will_not_change_in_ts VALUES (1, 'initial test row');
CREATE TABLE will_change_in_ts (a int, b text) TABLESPACE ts1;
INSERT INTO will_change_in_ts VALUES (1, 'initial test row');
CREATE TABLE will_get_dropped_in_ts (a int, b text);
INSERT INTO will_get_dropped_in_ts VALUES (1, 'initial test row');
""")

    # Read list of tablespace OIDs. There should be just one.
    tsoids = [e for e in os.listdir(os.path.join(primary.data_dir, "pg_tblspc"))
              if re.match(r"^\d+", e)]
    assert len(tsoids) == 1, "exactly one user-defined tablespace"
    tsoid = tsoids[0]

    # Take a full backup.
    backup1path = os.path.join(primary.backup_dir, "backup1")
    tsbackup1path = os.path.join(tempdir, "ts1backup")
    os.mkdir(tsbackup1path)
    primary.command_ok(
        [
            "pg_basebackup",
            "--no-sync",
            "--pgdata", backup1path,
            "--checkpoint", "fast",
            "--tablespace-mapping", f"{tsprimary}={tsbackup1path}",
        ],
        "full backup")

    # Now make some database changes.  VACUUM and the DROP/CREATE DATABASE
    # statements cannot run inside a transaction block, so issue the
    # transaction-incompatible statements separately.
    primary.safe_sql("""
UPDATE will_change SET b = 'modified value' WHERE a = 1;
UPDATE will_change_in_ts SET b = 'modified value' WHERE a = 1;
INSERT INTO will_grow
	SELECT g, 'additional row' FROM generate_series(2, 5000) g;
TRUNCATE will_shrink;
DROP TABLE will_get_dropped;
DROP TABLE will_get_dropped_in_ts;
CREATE TABLE newly_created (a int, b text);
INSERT INTO newly_created VALUES (1, 'row for new table');
CREATE TABLE newly_created_in_ts (a int, b text) TABLESPACE ts1;
INSERT INTO newly_created_in_ts VALUES (1, 'row for new table');
""")
    primary.safe_sql("VACUUM will_get_vacuumed;")
    primary.safe_sql("VACUUM FULL will_get_rewritten;")
    primary.safe_sql("DROP DATABASE db_will_get_dropped;")
    primary.safe_sql("CREATE DATABASE db_newly_created;")

    # Take an incremental backup.
    backup2path = os.path.join(primary.backup_dir, "backup2")
    tsbackup2path = os.path.join(tempdir, "tsbackup2")
    os.mkdir(tsbackup2path)
    primary.command_ok(
        [
            "pg_basebackup",
            "--no-sync",
            "--pgdata", backup2path,
            "--checkpoint", "fast",
            "--tablespace-mapping", f"{tsprimary}={tsbackup2path}",
            "--incremental", os.path.join(backup1path, "backup_manifest"),
        ],
        "incremental backup")

    # Find an LSN to which either backup can be recovered.
    lsn = primary.safe_sql("SELECT pg_current_wal_lsn();")

    # Make sure that the WAL segment containing that LSN has been archived.
    # PostgreSQL won't issue two consecutive XLOG_SWITCH records, and the
    # backup just issued one, so call txid_current() to generate some WAL
    # activity before calling pg_switch_wal().
    primary.safe_sql("SELECT txid_current();")
    primary.safe_sql("SELECT pg_switch_wal()")

    # Now wait for the LSN we chose above to be archived.
    archive_wait_query = (
        f"SELECT pg_walfile_name('{lsn}') <= last_archived_wal "
        "FROM pg_stat_archiver;")
    assert primary.poll_query_until(archive_wait_query), \
        "Timed out while waiting for WAL segment to be archived"

    # Perform PITR from the full backup. Disable archive_mode so that the
    # archive doesn't find out about the new timeline; that way, the later PITR
    # below will choose the same timeline.
    tspitr1path = os.path.join(tempdir, "tspitr1")
    pitr1 = create_pg("pitr1", start=False)
    _restore_node(pitr1, backup1path, tsoid, tspitr1path)
    pitr1.enable_restoring(primary, standby=True)
    pitr1.append_conf(f"""
recovery_target_lsn = '{lsn}'
recovery_target_action = 'promote'
archive_mode = 'off'
""")
    pitr1.start()

    # Perform PITR to the same LSN from the incremental backup. Use the same
    # basic configuration as before.  First combine the incremental backup
    # (backup2) with its prior full backup (backup1) using pg_combinebackup,
    # relocating the tablespace.
    tspitr2path = os.path.join(tempdir, "tspitr2")
    combinedpath = os.path.join(primary.backup_dir, "combined")
    tscombinedpath = os.path.join(tempdir, "tscombined")
    pitr2 = create_pg("pitr2", start=False)
    pitr2.command_ok(
        [
            "pg_combinebackup",
            backup1path,
            backup2path,
            "--output", combinedpath,
            "--tablespace-mapping", f"{tsbackup2path}={tscombinedpath}",
            mode,
        ],
        "combine full and incremental backup")
    _restore_node(pitr2, combinedpath, tsoid, tspitr2path)
    pitr2.enable_restoring(primary, standby=True)
    pitr2.append_conf(f"""
recovery_target_lsn = '{lsn}'
recovery_target_action = 'promote'
archive_mode = 'off'
""")
    pitr2.start()

    # Wait until both servers exit recovery.
    assert pitr1.poll_query_until("SELECT NOT pg_is_in_recovery();"), \
        f"Timed out while waiting apply to reach LSN {lsn}"
    assert pitr2.poll_query_until("SELECT NOT pg_is_in_recovery();"), \
        f"Timed out while waiting apply to reach LSN {lsn}"

    # Perform a logical dump of each server, and check that they match.
    # It would be much nicer if we could physically compare the data files, but
    # that doesn't really work. The contents of the page hole aren't guaranteed
    # to be identical, and there can be other discrepancies as well.
    #
    # NB: We're just using the primary's backup directory for scratch space
    # here.  This could equally well be any other directory we wanted to pick.
    backupdir = primary.backup_dir
    dump1 = os.path.join(backupdir, "pitr1.dump")
    dump2 = os.path.join(backupdir, "pitr2.dump")
    pitr1.command_ok(
        [
            "pg_dumpall",
            "--restrict-key", "test",
            "--no-sync",
            "--no-unlogged-table-data",
            "--file", dump1,
            "--dbname", pitr1.connstr("postgres"),
        ],
        "dump from PITR 1")
    pitr2.command_ok(
        [
            "pg_dumpall",
            "--restrict-key", "test",
            "--no-sync",
            "--no-unlogged-table-data",
            "--file", dump2,
            "--dbname", pitr2.connstr("postgres"),
        ],
        "dump from PITR 2")

    # Compare the two dumps, there should be no differences other than
    # the tablespace paths.
    _compare_dumps(dump1, dump2, "contents of dumps match for both PITRs")


def _compare_dumps(dump1, dump2, msg):
    """Compare two pg_dumpall files, normalizing the tablespace location path.

    Lines of the form
    "CREATE TABLESPACE ... LOCATION ...tspitr[12]" have their trailing 1/2
    folded to N before comparison so the per-node tablespace paths don't cause
    a spurious difference.
    """
    def _norm(line):
        return re.sub(r"(create tablespace .* location .*\btspitr)[12]",
                      r"\1N", line, flags=re.IGNORECASE)

    with open(dump1, encoding="utf-8", errors="replace") as fh:
        lines1 = [_norm(line) for line in fh]
    with open(dump2, encoding="utf-8", errors="replace") as fh:
        lines2 = [_norm(line) for line in fh]

    assert lines1 == lines2, msg
