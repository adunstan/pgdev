# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Evaluates PostgreSQL's recovery behavior when a WAL segment containing the
redo record is missing, with a checkpoint record located in a different
segment.
"""

import os
import re

import pytest

from pypg import slurp_file


def test_050_redo_segment_missing(create_pg):
    node = create_pg("testnode", start=False)
    node.append_conf("log_checkpoints = on")
    node.start()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    if (
        node.safe_sql(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        == "0"
    ):
        pytest.skip("Extension injection_points not installed")

    node.safe_sql("CREATE EXTENSION injection_points")

    # Note that this uses two injection points based on waits, not one.  This
    # may look strange, but this works as a workaround to enforce all memory
    # allocations to happen outside the critical section of the checkpoint
    # required for this test.
    # First, "create-checkpoint-initial" is run outside the critical section
    # section, and is used as a way to initialize the shared memory required
    # for the wait machinery with its DSM registry.
    # Then, "create-checkpoint-run" is loaded outside the critical section of
    # a checkpoint to allocate any memory required by the library load, and
    # its callback is run inside the critical section.
    node.safe_sql("select injection_points_attach('create-checkpoint-initial', 'wait')")
    node.safe_sql("select injection_points_attach('create-checkpoint-run', 'wait')")

    # Start a session to run the checkpoint in the background and make
    # the test wait on the injection point so the checkpoint stops just after
    # it starts.
    checkpoint = node.connect("postgres")
    checkpoint.do_async("CHECKPOINT;")

    # Wait for the initial point to finish, the checkpointer is still
    # outside its critical section.  Then release to reach the second
    # point.
    node.wait_for_event("checkpointer", "create-checkpoint-initial")
    node.safe_sql("select injection_points_wakeup('create-checkpoint-initial')")

    # Wait until the checkpoint has reached the second injection point.
    # We are now in the middle of a checkpoint running, after the redo
    # record has been logged.
    node.wait_for_event("checkpointer", "create-checkpoint-run")

    # Switch the WAL segment, ensuring that the redo record will be included
    # in a different segment than the checkpoint record.
    node.safe_sql("SELECT pg_switch_wal()")

    # Continue the checkpoint and wait for its completion.
    log_offset = node.log_position()
    node.safe_sql("select injection_points_wakeup('create-checkpoint-run')")
    node.wait_for_log(r"checkpoint complete", log_offset)

    checkpoint.wait_for_completion()
    checkpoint.close()

    # Retrieve the WAL file names for the redo record and checkpoint record.
    redo_lsn = node.safe_sql("SELECT redo_lsn FROM pg_control_checkpoint()")
    redo_walfile_name = node.safe_sql(f"SELECT pg_walfile_name('{redo_lsn}')")
    checkpoint_lsn = node.safe_sql("SELECT checkpoint_lsn FROM pg_control_checkpoint()")
    checkpoint_walfile_name = node.safe_sql(
        f"SELECT pg_walfile_name('{checkpoint_lsn}')"
    )

    # Redo record and checkpoint record should be on different segments.
    assert (
        redo_walfile_name != checkpoint_walfile_name
    ), "redo and checkpoint records on different segments"

    # Remove the WAL segment containing the redo record.
    os.unlink(os.path.join(node.data_dir, "pg_wal", redo_walfile_name))

    node.stop("immediate")

    # Use pg_bin.result instead of node.start because this test expects that
    # the server ends with an error during recovery.
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

    # Confirm that recovery has failed, as expected.
    logfile = slurp_file(node.logfile)
    assert re.search(
        r"FATAL: .* could not find redo location .* "
        r"referenced by checkpoint record at .*",
        logfile,
    ), "ends with FATAL because it could not find redo location"
