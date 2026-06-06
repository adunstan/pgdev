# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test that pg_rewind copies WAL segments generated before the divergence
point from the source, replacing corrupted copies on the target.
"""

import os
import re

from pypg.command import PgBin


def test_011_wal_copy(rewind, bindir):
    rewind.setup_cluster()
    rewind.start_primary()
    rewind.create_standby()

    node_primary = rewind.node_primary
    node_standby = rewind.node_standby

    # Advance WAL on primary
    rewind.primary_psql("CREATE TABLE t(a int)")
    rewind.primary_psql("INSERT INTO t VALUES(0)")

    # Segment that is not copied from the source to the target, being
    # generated before the servers have diverged.
    wal_seg_skipped = node_primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())"
    )

    rewind.primary_psql("SELECT pg_switch_wal()")

    # Follow-up segment, that will include corrupted contents, and will be
    # copied from the source to the target even if generated before the point
    # of divergence.
    rewind.primary_psql("INSERT INTO t VALUES(0)")
    corrupt_wal_seg = node_primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())"
    )
    rewind.primary_psql("SELECT pg_switch_wal()")

    rewind.primary_psql("CHECKPOINT")
    rewind.promote_standby()

    # New segment on a new timeline, expected to be copied.
    new_timeline_wal_seg = node_standby.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn())"
    )

    # Corrupt a WAL segment on target that has been generated before the
    # divergence point.  We will check that it is copied from the source.
    corrupt_wal_seg_in_target_path = os.path.join(
        node_primary.data_dir, "pg_wal", corrupt_wal_seg
    )
    with open(corrupt_wal_seg_in_target_path, "ab") as fh:
        fh.write(b"a")

    assert os.path.exists(
        corrupt_wal_seg_in_target_path
    ), f"segment {corrupt_wal_seg} exists in target before rewind"
    corrupt_wal_seg_size_before_rewind = os.stat(corrupt_wal_seg_in_target_path).st_size

    # Verify that the WAL segment on the new timeline does not exist in target
    # before the rewind.
    new_timeline_wal_seg_path = os.path.join(
        node_primary.data_dir, "pg_wal", new_timeline_wal_seg
    )
    assert not os.path.exists(
        new_timeline_wal_seg_path
    ), f"segment {new_timeline_wal_seg} does not exist in target before rewind"

    node_standby.stop()
    node_primary.stop()

    # Cross-check how WAL segments are handled:
    # - The "corrupted" segment generated before the point of divergence is
    #   copied.
    # - The "clean" segment generated before the point of divergence is skipped.
    # - The segment of the new timeline is copied.
    pg_bin = PgBin(bindir)
    pg_bin.command_checks_all(
        [
            "pg_rewind",
            "--debug",
            "--source-pgdata",
            node_standby.data_dir,
            "--target-pgdata",
            node_primary.data_dir,
            "--no-sync",
        ],
        0,
        [re.compile("")],
        [
            re.compile(r"pg_wal/" + re.escape(wal_seg_skipped) + r" \(NONE\)"),
            re.compile(r"pg_wal/" + re.escape(corrupt_wal_seg) + r" \(COPY\)"),
            re.compile(r"pg_wal/" + re.escape(new_timeline_wal_seg) + r" \(COPY\)"),
        ],
        "run pg_rewind",
    )

    # Verify that the first WAL segment of the new timeline now exists in
    # target.
    assert os.path.exists(
        new_timeline_wal_seg_path
    ), f"new timeline segment {new_timeline_wal_seg} exists in target after rewind"

    # Validate that the WAL segment with the same file name as the
    # corrupted WAL segment in target has been copied from source
    # where it was still intact.
    corrupt_wal_seg_in_source_path = os.path.join(
        node_standby.data_dir, "pg_wal", corrupt_wal_seg
    )
    assert os.path.exists(
        corrupt_wal_seg_in_source_path
    ), f"corrupted {corrupt_wal_seg} exists in source after rewind"
    corrupt_wal_seg_source_size = os.stat(corrupt_wal_seg_in_source_path).st_size

    assert os.path.exists(
        corrupt_wal_seg_in_target_path
    ), f"corrupted {corrupt_wal_seg} exists in target after rewind"
    corrupt_wal_seg_size_after_rewind = os.stat(corrupt_wal_seg_in_target_path).st_size

    assert corrupt_wal_seg_size_before_rewind != corrupt_wal_seg_source_size, (
        f"different size of corrupted {corrupt_wal_seg} in source vs target "
        "before rewind"
    )
    assert corrupt_wal_seg_size_after_rewind == corrupt_wal_seg_source_size, (
        f"same size of corrupted {corrupt_wal_seg} in source and target " "after rewind"
    )
