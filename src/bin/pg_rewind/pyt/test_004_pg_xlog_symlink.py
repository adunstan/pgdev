# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_rewind when the target's pg_wal directory is a symlink."""

import os
import shutil

import pytest

from pypg.util import dir_symlink, short_tempdir


def _run_test(rewind, test_mode, xlog_parent):
    rewind.setup_cluster(test_mode)

    test_primary_datadir = rewind.node_primary.data_dir

    # External directory that pg_wal is moved to and linked back from.  It must
    # have a SHORT path: on Windows a directory junction stores its target path
    # twice, and the server reads the reparse data into a fixed MAX_PATH-sized
    # buffer (pgreadlink), so a long target -- such as one under the deep
    # per-test data directory -- overflows it and the server fails to start
    # with "could not get junction".  short_tempdir keeps it well within range.
    primary_xlogdir = os.path.join(xlog_parent, "xlog_primary")

    # Turn pg_wal into a symlink (a junction on Windows).
    pg_wal = os.path.join(test_primary_datadir, "pg_wal")
    print(f"moving {pg_wal} to {primary_xlogdir}")
    shutil.move(pg_wal, primary_xlogdir)
    dir_symlink(primary_xlogdir, pg_wal)

    rewind.start_primary()

    # Create a test table and insert a row in primary.
    rewind.primary_psql("CREATE TABLE tbl1 (d text)")
    rewind.primary_psql("INSERT INTO tbl1 VALUES ('in primary')")

    rewind.primary_psql("CHECKPOINT")

    rewind.create_standby(test_mode)

    # Insert additional data on primary that will be replicated to standby.
    rewind.primary_psql("INSERT INTO tbl1 values ('in primary, before promotion')")

    rewind.primary_psql("CHECKPOINT")

    rewind.promote_standby()

    # Insert a row in the old primary.  This causes the primary and standby to
    # have "diverged", it's no longer possible to just apply the standby's logs
    # over primary directory - you need to rewind.
    rewind.primary_psql("INSERT INTO tbl1 VALUES ('in primary, after promotion')")

    # Also insert a new row in the standby, which won't be present in the old
    # primary.
    rewind.standby_psql("INSERT INTO tbl1 VALUES ('in standby, after promotion')")

    rewind.run_pg_rewind(test_mode)

    rewind.check_query(
        "SELECT * FROM tbl1",
        "in primary\n"
        "in primary, before promotion\n"
        "in standby, after promotion\n",
        "table content",
    )

    rewind.clean_rewind_test()


# Run the test in both modes.
@pytest.mark.parametrize("mode", ["local", "remote"])
def test_004_pg_xlog_symlink(rewind, mode):
    xlog_parent = short_tempdir()
    try:
        _run_test(rewind, mode, xlog_parent)
    finally:
        shutil.rmtree(xlog_parent, ignore_errors=True)
