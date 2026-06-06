# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that pg_rewind correctly reconciles the set of databases present in
the source and target clusters, and that PGDATA permissions are preserved.
"""

import os
import stat

import pytest


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


def run_test(rewind, test_mode):
    rewind.setup_cluster(extra_name=test_mode, extra=["-g"])
    rewind.start_primary()

    # Create a database in primary with a table.
    rewind.primary_psql("CREATE DATABASE inprimary")
    rewind.primary_psql("CREATE TABLE inprimary_tab (a int)", dbname="inprimary")

    rewind.create_standby(test_mode)

    # Create another database with another table, the creation is
    # replicated to the standby.
    rewind.primary_psql("CREATE DATABASE beforepromotion")
    rewind.primary_psql(
        "CREATE TABLE beforepromotion_tab (a int)", dbname="beforepromotion"
    )

    rewind.promote_standby()

    # Create databases in the old primary and the new promoted standby.
    rewind.primary_psql("CREATE DATABASE primary_afterpromotion")
    rewind.primary_psql(
        "CREATE TABLE primary_promotion_tab (a int)",
        dbname="primary_afterpromotion",
    )
    rewind.standby_psql("CREATE DATABASE standby_afterpromotion")
    rewind.standby_psql(
        "CREATE TABLE standby_promotion_tab (a int)",
        dbname="standby_afterpromotion",
    )

    # The clusters are now diverged.

    rewind.run_pg_rewind(test_mode)

    # Check that the correct databases are present after pg_rewind.
    rewind.check_query(
        "SELECT datname FROM pg_database ORDER BY 1",
        "beforepromotion\n"
        "inprimary\n"
        "postgres\n"
        "standby_afterpromotion\n"
        "template0\n"
        "template1\n",
        "database names",
    )

    # Permissions on PGDATA should have group permissions.
    assert check_mode_recursive(
        rewind.node_primary.data_dir, 0o750, 0o640
    ), "check PGDATA permissions"

    rewind.clean_rewind_test()


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_002_databases(rewind, mode):
    run_test(rewind, mode)
