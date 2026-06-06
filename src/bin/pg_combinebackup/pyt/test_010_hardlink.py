# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Validate that hard links are created as expected in the output directory when
running pg_combinebackup in --link mode.  Take a full backup and an
incremental backup, combine them with pg_combinebackup --link, and check that
data files which did not change between the two backups are hard-linked
(st_nlink == 2), while the last segment of a table that did change has a single
link.

FRAMEWORK NOTE:

  * The framework's init_from_backup does not support incremental combine, so
    (as test_006 does) run pg_combinebackup directly into the restore node's
    data directory.
"""

import os
import shutil


def _get_hard_link_count(path):
    """Return the number of hard links of the file at *path*."""
    return os.stat(path).st_nlink


def _check_data_file(data_file, last_segment_nlinks):
    """Check the hard link counts of all segments of a data file.

    Given the path to the first segment of a data file, inspect its parent
    directory to find all the segments of that data file.  All segments should
    contain 2 hard links, except the last one, which should match
    *last_segment_nlinks*.
    """
    data_file_segments = [data_file]

    # Start checking for additional segments.
    segment_number = 1
    while True:
        next_segment = f"{data_file}.{segment_number}"
        if os.path.isfile(next_segment):
            data_file_segments.append(next_segment)
            segment_number += 1
        else:
            break

    # All segments of the given data file should contain 2 hard links, except
    # for the last one, which should match the given number of links.
    last_segment = data_file_segments.pop()

    for segment in data_file_segments:
        nlink_count = _get_hard_link_count(segment)
        assert nlink_count == 2, f"File '{segment}' has 2 hard links"

    nlink_count = _get_hard_link_count(last_segment)
    assert nlink_count == last_segment_nlinks, \
        f"File '{last_segment}' has {last_segment_nlinks} hard link(s)"


def test_010_hardlink(create_pg):
    # Set up a new database instance.
    primary = create_pg("primary", start=False, has_archiving=True,
                         allows_streaming=True)
    primary.append_conf("summarize_wal = on")
    # We disable autovacuum to prevent "something else" to modify our test
    # tables.
    primary.append_conf("autovacuum = off")
    primary.start()

    # Create a couple of tables (~264KB each).
    # Note: Cirrus CI runs some tests with a very small segment size, so, in
    # that environment, a single table of 264KB would have both a segment with
    # a link count of 1 and also one with a link count of 2.  But in a normal
    # installation, segment size is 1GB.  Therefore, we use 2 different tables
    # here: for test_1, all segments (or the only one) will have two hard
    # links; for test_2, the last segment (or the only one) will have 1 hard
    # link, and any others will have 2.
    query = """
CREATE TABLE test_{0} AS
    SELECT x.id::bigint,
           repeat('a', 1600) AS value
    FROM generate_series(1, 100) AS x(id);
"""

    primary.safe_sql(query.format("1"))
    primary.safe_sql(query.format("2"))

    # Fetch information about the data files.
    path_query = """
SELECT pg_relation_filepath(oid)
FROM pg_class
WHERE relname = 'test_{0}';
"""

    test_1_path = primary.safe_sql(path_query.format("1"))
    print(f"# test_1 path is {test_1_path}")

    test_2_path = primary.safe_sql(path_query.format("2"))
    print(f"# test_2 path is {test_2_path}")

    # Take a full backup.
    backup1path = os.path.join(primary.backup_dir, "backup1")
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup1path,
            "--no-sync",
            "--checkpoint", "fast",
            "--wal-method", "none",
        ],
        "full backup")

    # Perform an insert that touches a page of the last segment of the data
    # file of table test_2.
    primary.safe_sql(
        "INSERT INTO test_2 (id, value) VALUES (101, repeat('a', 1600));")

    # Take an incremental backup.
    backup2path = os.path.join(primary.backup_dir, "backup2")
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup2path,
            "--no-sync",
            "--checkpoint", "fast",
            "--wal-method", "none",
            "--incremental", os.path.join(backup1path, "backup_manifest"),
        ],
        "incremental backup")

    # Restore the incremental backup and use it to create a new node.
    #
    # Run pg_combinebackup --link directly into the restore node's data
    # directory.
    restore = create_pg("restore", start=False)
    combined = restore.data_dir
    if os.path.isdir(combined):
        shutil.rmtree(combined)
    restore.command_ok(
        [
            "pg_combinebackup", backup1path, backup2path,
            "--output", combined,
            "--link",
        ],
        "combine backups with --link")

    # Ensure files have the expected count of hard links.  We expect all data
    # files from test_1 to contain 2 hard links, because they were not touched
    # between the full and incremental backups, and the last data file of table
    # test_2 to contain a single hard link because of changes in its last page.
    test_1_full_path = os.path.join(restore.data_dir, test_1_path)
    _check_data_file(test_1_full_path, 2)

    test_2_full_path = os.path.join(restore.data_dir, test_2_path)
    _check_data_file(test_2_full_path, 1)
