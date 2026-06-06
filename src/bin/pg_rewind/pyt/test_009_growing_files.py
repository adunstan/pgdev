# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that pg_rewind reports an error when a source file grows while it is
being copied.
"""

import os
import re
import subprocess

from pypg.command import PgBin


def test_009_growing_files(rewind, bindir):
    rewind.setup_cluster("local")
    rewind.start_primary()

    # Create a test table and insert a row in primary.
    rewind.primary_psql("CREATE TABLE tbl1 (d text)")
    rewind.primary_psql("INSERT INTO tbl1 VALUES ('in primary')")
    rewind.primary_psql("CHECKPOINT")

    rewind.create_standby("local")

    # Insert additional data on primary that will be replicated to standby
    rewind.primary_psql("INSERT INTO tbl1 values ('in primary, before promotion')")
    rewind.primary_psql("CHECKPOINT")

    rewind.promote_standby()

    # Insert a row in the old primary. This causes the primary and standby to
    # have "diverged", it's no longer possible to just apply the standby's logs
    # over primary directory - you need to rewind.  Also insert a new row in
    # the standby, which won't be present in the old primary.
    rewind.primary_psql("INSERT INTO tbl1 VALUES ('in primary, after promotion')")
    rewind.standby_psql("INSERT INTO tbl1 VALUES ('in standby, after promotion')")

    # Stop the nodes before running pg_rewind
    rewind.node_standby.stop()
    rewind.node_primary.stop()

    primary_pgdata = rewind.node_primary.data_dir
    standby_pgdata = rewind.node_standby.data_dir

    # Add an extra file that we can tamper with without interfering with the
    # data directory data files.
    os.mkdir(os.path.join(standby_pgdata, "tst_both_dir"))
    file1 = os.path.join(standby_pgdata, "tst_both_dir", "file1")
    with open(file1, "a", encoding="utf-8") as fh:
        fh.write("a")

    # Run pg_rewind and pipe the output from the run into the extra file we
    # want to copy. This will ensure that the file is continuously growing
    # during the copy operation and the result will be an error.
    pg_bin = PgBin(bindir)
    argv = [
        os.path.join(bindir, "pg_rewind"),
        "--debug",
        "--source-pgdata=" + standby_pgdata,
        "--target-pgdata=" + primary_pgdata,
        "--no-sync",
    ]
    print("# Running: " + " ".join(argv))
    with open(file1, "ab") as errfh:
        proc = subprocess.run(
            argv,
            env=pg_bin.command_env(None),
            stdout=subprocess.DEVNULL,
            stderr=errfh,
            check=False,
        )
    ret = proc.returncode
    assert ret != 0, "Error out on copying growing file"

    # Ensure that the files are of different size, the final error message
    # should only be in one of them making them guaranteed to be different
    primary_size = os.path.getsize(
        os.path.join(primary_pgdata, "tst_both_dir", "file1")
    )
    standby_size = os.path.getsize(file1)
    assert standby_size != primary_size, "File sizes should differ"

    # Extract the last line from the verbose output as that should have the
    # error message for the unexpected file size
    last = None
    with open(file1, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            last = line
    assert last is not None and re.search(
        r"error: size of source file", last
    ), "Check error message"
