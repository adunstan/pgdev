# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""This test aims to validate two things: (1) that the calculated truncation
block never exceeds the segment size and (2) that the correct limit block
length is calculated for the VM fork.

The test exercises incremental-backup handling of relation truncation: a
relation shrinks (via DELETE + VACUUM TRUNCATE) between the full and the
incremental backup.  The full and incremental backups are then combined and
the result verified on a started node.

FRAMEWORK NOTES:

  * The framework's init_from_backup does not support incremental combine, so
    run pg_combinebackup directly into the verification node's data_dir, then
    build a verification node on top of it (as test_002 / test_006 do).
"""

import os
import shutil


def test_011_ib_truncation(create_pg, tmp_path):
    # Initialize primary node
    primary = create_pg("primary", start=False, has_archiving=True,
                        allows_streaming=True)
    primary.append_conf("summarize_wal = on")
    primary.start()

    # Backup locations
    backup_path = primary.backup_dir
    full_backup = os.path.join(backup_path, "full")

    # To avoid using up lots of disk space in the CI/buildfarm environment,
    # this test will only find the issue when run with a small RELSEG_SIZE. As
    # of this writing, one of the CI runs is configured using
    # --with-segsize-blocks=6, and we aim to have this test check for the issue
    # only in that configuration.
    target_blocks = 6
    block_size = int(primary.safe_sql(
        "SELECT current_setting('block_size')::int;"))

    # We'll have two blocks more than the target number of blocks (one will
    # survive the subsequent truncation).
    target_rows = int(target_blocks + 2)
    rows_after_truncation = int(target_rows - 1)

    # Create a test table. STORAGE PLAIN prevents compression and TOASTing of
    # repetitive data, ensuring predictable row sizes.
    primary.safe_sql("""
    CREATE TABLE t (
        id int,
        data text STORAGE PLAIN
    ) WITH (autovacuum_enabled = false);
""")

    # The tuple size should be enough to prevent two tuples from being on the
    # same page. Since the template string has a length of 32 bytes, it's
    # enough to repeat it (block_size / (2*32)) times.
    primary.safe_sql(
        "INSERT INTO t\n"
        "        SELECT i,\n"
        "            repeat('0123456789ABCDEF0123456789ABCDEF', "
        f"({block_size} / (2*32)))\n"
        f"    FROM generate_series(1, {target_rows}) i;")

    # Make sure hint bits are set.
    primary.safe_sql("VACUUM t;")

    # Verify that the relation is as large as was desired.
    t_blocks = int(primary.safe_sql(
        "SELECT pg_relation_size('t') / current_setting('block_size')::int;"))
    assert t_blocks > target_blocks, "target block size exceeded"

    # Take a full base backup
    primary.backup("full")

    # Delete rows at the logical end of the table, creating removable pages.
    primary.safe_sql(
        f"DELETE FROM t WHERE id > ({rows_after_truncation});")

    # VACUUM the table. TRUNCATE is enabled by default, and is just mentioned
    # here for emphasis.
    primary.safe_sql("VACUUM (TRUNCATE) t;")

    # Verify expected length after truncation.
    t_blocks = int(primary.safe_sql(
        "SELECT pg_relation_size('t') / current_setting('block_size')::int;"))
    assert t_blocks == rows_after_truncation, \
        "post-truncation row count as expected"
    assert t_blocks > target_blocks, \
        "post-truncation block count as expected"

    # Take an incremental backup based on the full backup manifest
    primary.backup("incr", backup_options=[
        "--incremental", os.path.join(full_backup, "backup_manifest")])

    # We used to have a bug where the wrong limit block was calculated for the
    # VM fork, so verify that the WAL summary records the correct VM fork
    # truncation limit. We can't just check whether the restored VM fork is
    # the right size on disk, because it's so small that the incremental
    # backup code will send the entire file.
    relfilenode = primary.safe_sql("SELECT pg_relation_filenode('t');")
    vm_limits = primary.safe_sql(
        "SELECT string_agg(relblocknumber::text, ',')\n"
        "   FROM pg_available_wal_summaries() s,\n"
        "        pg_wal_summary_contents(s.tli, s.start_lsn, s.end_lsn) c\n"
        f"  WHERE c.relfilenode = {relfilenode}\n"
        "    AND c.relforknumber = 2\n"
        "    AND c.is_limit_block;")
    assert vm_limits == "1", \
        "WAL summary has correct VM fork truncation limit"

    # Combine full and incremental backups.  Before the fix, this failed
    # because the INCREMENTAL file header contained an incorrect
    # truncation_block_length value.
    #
    # Run pg_combinebackup directly into the verification node's data_dir.
    restored = create_pg("node2", start=False)
    combined = restored.data_dir
    if os.path.isdir(combined):
        shutil.rmtree(combined)
    incr_backup = os.path.join(backup_path, "incr")
    restored.command_ok(
        ["pg_combinebackup", full_backup, incr_backup, "--output", combined],
        "combine full and incremental backup")

    # init() already wrote our connection settings (port, socket dir) to the
    # original data dir's postgresql.conf, which pg_combinebackup overwrote.
    # Append them again to the combined data dir before starting.
    restored.append_conf("\n".join([
        "",
        f"port = {restored.port}",
        "listen_addresses = ''",
        f"unix_socket_directories = '{restored.host}'",
        "",
    ]))
    restored.start()

    # Check that the restored table contains the correct number of rows
    restored_count = restored.safe_sql("SELECT count(*) FROM t;")
    assert int(restored_count) == rows_after_truncation, \
        "Restored backup has correct row count"

    primary.stop()
    restored.stop()
