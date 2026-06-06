# Copyright (c) 2026, PostgreSQL Global Development Group

"""Verify crash recovery behavior when the WAL segment containing the
checkpoint record referenced by pg_controldata is missing.  This
checks the code path where there is no backup_label file, where the
startup process should fail with FATAL and log a message about the
missing checkpoint record.
"""

import os
import re

from pypg import slurp_file


def test_052_checkpoint_segment_missing(create_pg):
    node = create_pg("testnode", start=False)
    node.append_conf("log_checkpoints = on")
    node.start()

    # Force a checkpoint so as pg_controldata points to a checkpoint record we
    # can target.
    node.safe_sql("CHECKPOINT;")

    # Retrieve the checkpoint LSN and derive the WAL segment name.
    checkpoint_walfile = node.safe_sql(
        "SELECT pg_walfile_name(checkpoint_lsn) FROM pg_control_checkpoint()"
    )

    assert (
        checkpoint_walfile != ""
    ), f"derived checkpoint WAL file name: {checkpoint_walfile}"

    # Stop the node.
    node.stop("immediate")

    # Remove the WAL segment containing the checkpoint record.
    walpath = os.path.join(node.data_dir, "pg_wal", checkpoint_walfile)
    assert os.path.isfile(
        walpath
    ), f"checkpoint WAL file exists before deletion: {walpath}"

    os.unlink(walpath)

    assert not os.path.exists(walpath), f"checkpoint WAL file removed: {walpath}"

    # Use pg_ctl directly instead of node.start because this test expects
    # that the server ends with an error during recovery.
    node.pg_bin.result(
        [
            "pg_ctl",
            "--pgdata",
            node.data_dir,
            "--log",
            node.logfile,
            "start",
        ]
    )

    # Confirm that recovery has failed as expected.
    logfile = slurp_file(node.logfile)
    assert re.search(
        r"FATAL: .* could not locate a valid checkpoint record at .*", logfile
    ), "FATAL logged for missing checkpoint record (no backup_label path)"
