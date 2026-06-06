# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test situation where a target data directory contains WAL files that were
already recycled by the new primary.
"""

import re
import sys

from pypg.command import PgBin


def test_010_keep_recycled_wals(rewind, bindir):
    rewind.setup_cluster()
    rewind.node_primary.enable_archiving()
    rewind.start_primary()

    rewind.create_standby()
    rewind.node_standby.enable_restoring(rewind.node_primary, standby=False)
    rewind.node_standby.reload()

    rewind.primary_psql("CHECKPOINT")  # last common checkpoint

    # We use the running interpreter with "exit(1)" as an alternative to
    # "false", because the latter might not be available on Windows.
    false = f'{sys.executable} -c "import sys; sys.exit(1)"'
    rewind.node_primary.append_conf(
        "\n"
        f"archive_command = '{false}'\n"
    )
    rewind.node_primary.reload()

    # advance WAL on primary; this WAL segment will never make it to the
    # archive
    rewind.primary_psql("CREATE TABLE t(a int)")
    rewind.primary_psql("INSERT INTO t VALUES(0)")
    rewind.primary_psql("SELECT pg_switch_wal()")

    rewind.promote_standby()

    # new primary loses diverging WAL segment
    rewind.standby_psql("INSERT INTO t values(0)")
    rewind.standby_psql("SELECT pg_switch_wal()")

    rewind.node_standby.stop()
    rewind.node_primary.stop()

    pg_bin = PgBin(bindir)
    res = pg_bin.result(
        [
            "pg_rewind", "--debug",
            "--source-pgdata", rewind.node_standby.data_dir,
            "--target-pgdata", rewind.node_primary.data_dir,
            "--no-sync",
        ]
    )

    assert re.search(
        r"Not removing file .* because it is required for recovery",
        res.stderr,
    ), "some WAL files were skipped\n" + res.stderr
