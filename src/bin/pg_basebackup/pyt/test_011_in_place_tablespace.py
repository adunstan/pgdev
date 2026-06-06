# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that pg_basebackup correctly handles in-place tablespaces."""

import glob
import os

# For nearly all pg_basebackup invocations some options should be specified,
# to keep test times reasonable.
PG_BASEBACKUP_DEFS = ["pg_basebackup", "--no-sync", "--checkpoint", "fast"]


def test_011_in_place_tablespace(create_pg, tmp_path):
    tempdir = str(tmp_path)

    # Set up an instance.
    node = create_pg("main", allows_streaming=True)

    # Create an in-place tablespace.  These run as separate statements so that
    # CREATE TABLESPACE is not wrapped in an implicit transaction block (the
    # cached session persists the SET across calls).
    node.safe_sql("SET allow_in_place_tablespaces = on")
    node.safe_sql("CREATE TABLESPACE inplace LOCATION ''")

    # Back it up.
    backupdir = os.path.join(tempdir, "backup")
    node.command_ok(
        PG_BASEBACKUP_DEFS
        + [
            "--pgdata", backupdir,
            "--format", "tar",
            "--wal-method", "none",
        ],
        "pg_basebackup runs",
    )

    # Make sure we got base.tar and one tablespace.
    assert os.path.isfile(os.path.join(backupdir, "base.tar")), \
        "backup tar was created"
    tblspc_tars = glob.glob(os.path.join(backupdir, "[0-9]*.tar"))
    assert len(tblspc_tars) == 1, "one tablespace tar was created"
