# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic pg_rewind test.

Run in each of the three source modes: a local data
directory ('local'), a live source server ('remote'), and a WAL archive
('archive').
"""

import os
import stat

import pytest

from pypg.command import PgBin


def check_mode_recursive(path, dir_mode, file_mode):
    """Assert every dir/file under *path* has the expected permission bits.

    Returns True when all entries match; raises AssertionError (with details)
    otherwise.
    """
    ok = True
    for root, dirs, files in os.walk(path):
        for name in dirs:
            full = os.path.join(root, name)
            if os.path.islink(full):
                continue
            actual = stat.S_IMODE(os.lstat(full).st_mode)
            if actual != dir_mode:
                print(f"mode of directory {full} is {actual:#o}, "
                      f"expected {dir_mode:#o}")
                ok = False
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                continue
            actual = stat.S_IMODE(os.lstat(full).st_mode)
            if actual != file_mode:
                print(f"mode of file {full} is {actual:#o}, "
                      f"expected {file_mode:#o}")
                ok = False
    return ok


def run_test(rewind, bindir, test_mode):
    rewind.setup_cluster(test_mode)
    rewind.start_primary()

    # Create an in-place tablespace with some data on it.
    rewind.primary_psql("CREATE TABLESPACE space_test LOCATION ''")
    rewind.primary_psql(
        "CREATE TABLE space_tbl (d text) TABLESPACE space_test")
    rewind.primary_psql(
        "INSERT INTO space_tbl VALUES ('in primary, before promotion')")

    # Create a test table and insert a row in primary.
    rewind.primary_psql("CREATE TABLE tbl1 (d text)")
    rewind.primary_psql("INSERT INTO tbl1 VALUES ('in primary')")

    # This test table will be used to test truncation, i.e. the table
    # is extended in the old primary after promotion.
    rewind.primary_psql("CREATE TABLE trunc_tbl (d text)")
    rewind.primary_psql("INSERT INTO trunc_tbl VALUES ('in primary')")

    # This test table will be used to test the "copy-tail" case, i.e. the
    # table is truncated in the old primary after promotion.
    rewind.primary_psql("CREATE TABLE tail_tbl (id integer, d text)")
    rewind.primary_psql("INSERT INTO tail_tbl VALUES (0, 'in primary')")

    # This test table is dropped in the old primary after promotion.
    rewind.primary_psql("CREATE TABLE drop_tbl (d text)")
    rewind.primary_psql("INSERT INTO drop_tbl VALUES ('in primary')")

    rewind.primary_psql("CHECKPOINT")

    rewind.create_standby(test_mode)

    # Insert additional data on primary that will be replicated to standby.
    rewind.primary_psql(
        "INSERT INTO tbl1 values ('in primary, before promotion')")
    rewind.primary_psql(
        "INSERT INTO trunc_tbl values ('in primary, before promotion')")
    rewind.primary_psql(
        "INSERT INTO tail_tbl SELECT g, 'in primary, before promotion: ' || g "
        "FROM generate_series(1, 10000) g")

    rewind.primary_psql("CHECKPOINT")

    rewind.promote_standby()

    # Insert a row in the old primary. This causes the primary and standby to
    # have "diverged", it's no longer possible to just apply the standby's
    # logs over primary directory - you need to rewind.
    rewind.primary_psql("INSERT INTO tbl1 VALUES ('in primary, after promotion')")

    # Also insert a new row in the standby, which won't be present in the old
    # primary.
    rewind.standby_psql("INSERT INTO tbl1 VALUES ('in standby, after promotion')")

    # Insert enough rows to trunc_tbl to extend the file. pg_rewind should
    # truncate it back to the old size.
    rewind.primary_psql(
        "INSERT INTO trunc_tbl SELECT 'in primary, after promotion: ' || g "
        "FROM generate_series(1, 10000) g")

    # Truncate tail_tbl. pg_rewind should copy back the truncated part.
    # (We cannot use an actual TRUNCATE command here, as that creates a whole
    # new relfilenode.)
    rewind.primary_psql("DELETE FROM tail_tbl WHERE id > 10")
    rewind.primary_psql("VACUUM tail_tbl")

    # Drop drop_tbl. pg_rewind should copy it back.
    rewind.primary_psql(
        "insert into drop_tbl values ('in primary, after promotion')")
    rewind.primary_psql("DROP TABLE drop_tbl")

    # Insert some data in the in-place tablespace for the old primary and the
    # standby.
    rewind.primary_psql(
        "INSERT INTO space_tbl VALUES ('in primary, after promotion')")
    rewind.standby_psql(
        "INSERT INTO space_tbl VALUES ('in standby, after promotion')")

    # Before running pg_rewind, do a couple of extra tests with several option
    # combinations.  As the code paths taken by those tests do not change for
    # the "local" and "remote" modes, just run them in "local" mode for
    # simplicity's sake.
    if test_mode == "local":
        pg_bin = PgBin(bindir)
        primary_pgdata = rewind.node_primary.data_dir
        standby_pgdata = rewind.node_standby.data_dir

        # First check that pg_rewind fails if the target cluster is not
        # stopped as it fails to start up for the forced recovery step.
        pg_bin.command_fails(
            [
                "pg_rewind", "--debug",
                "--source-pgdata", standby_pgdata,
                "--target-pgdata", primary_pgdata,
                "--no-sync",
            ],
            "pg_rewind with running target",
        )

        # Again with --no-ensure-shutdown, which should equally fail.  This
        # time pg_rewind complains without attempting to perform recovery once.
        pg_bin.command_fails(
            [
                "pg_rewind", "--debug",
                "--source-pgdata", standby_pgdata,
                "--target-pgdata", primary_pgdata,
                "--no-sync", "--no-ensure-shutdown",
            ],
            "pg_rewind --no-ensure-shutdown with running target",
        )

        # Stop the target, and attempt to run with a local source still
        # running.  This fails as pg_rewind requires the source cleanly
        # stopped.
        rewind.node_primary.stop()
        pg_bin.command_fails(
            [
                "pg_rewind", "--debug",
                "--source-pgdata", standby_pgdata,
                "--target-pgdata", primary_pgdata,
                "--no-sync", "--no-ensure-shutdown",
            ],
            "pg_rewind with unexpected running source",
        )

        # Stop the target cluster cleanly, and run pg_rewind again in
        # --dry-run mode.  If anything gets generated in the data folder, the
        # follow-up run of pg_rewind will most likely fail, so keep this test
        # as the last one of this subset.
        rewind.node_standby.stop()
        pg_bin.command_ok(
            [
                "pg_rewind", "--debug",
                "--source-pgdata", standby_pgdata,
                "--target-pgdata", primary_pgdata,
                "--no-sync", "--dry-run",
            ],
            "pg_rewind --dry-run",
        )

        # Both clusters need to be alive moving forward.
        rewind.node_standby.start()
        rewind.node_primary.start()

    rewind.run_pg_rewind(test_mode)

    rewind.check_query(
        "SELECT * FROM space_tbl ORDER BY d",
        "in primary, before promotion\n"
        "in standby, after promotion\n",
        "table content",
    )

    rewind.check_query(
        "SELECT * FROM tbl1",
        "in primary\n"
        "in primary, before promotion\n"
        "in standby, after promotion\n",
        "table content",
    )

    rewind.check_query(
        "SELECT * FROM trunc_tbl",
        "in primary\n"
        "in primary, before promotion\n",
        "truncation",
    )

    rewind.check_query(
        "SELECT count(*) FROM tail_tbl",
        "10001\n",
        "tail-copy",
    )

    rewind.check_query(
        "SELECT * FROM drop_tbl",
        "in primary\n",
        "drop",
    )

    # Permissions on PGDATA should be default.
    assert check_mode_recursive(rewind.node_primary.data_dir, 0o700, 0o600), \
        "check PGDATA permissions"

    rewind.clean_rewind_test()


@pytest.mark.parametrize("mode", ["local", "remote", "archive"])
def test_001_basic(rewind, bindir, mode):
    run_test(rewind, bindir, mode)
