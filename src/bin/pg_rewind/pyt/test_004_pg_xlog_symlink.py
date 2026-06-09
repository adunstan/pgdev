# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_rewind when the target's pg_wal directory is a symlink."""

import os
import shutil

import pytest

from pypg.util import dir_symlink


def run_test(rewind, test_mode):
    rewind.setup_cluster(test_mode)

    test_primary_datadir = rewind.node_primary.data_dir

    # External directory that pg_wal will be symlinked to.  It lives under the
    # primary node's basedir.
    primary_xlogdir = os.path.join(rewind.node_primary.basedir, "xlog_primary")

    if os.path.exists(primary_xlogdir):
        shutil.rmtree(primary_xlogdir)

    # Turn pg_wal into a symlink.
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
    run_test(rewind, mode)
