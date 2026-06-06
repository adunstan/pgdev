# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""This test verifies the case when the physical slot is advanced during
checkpoint. The test checks that the physical slot's restart_lsn still refers
to an existed WAL segment after immediate restart.
"""

import os

import pytest


def test_047_checkpoint_physical_slot(create_pg):
    node = create_pg("mike", start=False)
    node.append_conf("wal_level = 'replica'")
    node.start()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    if node.safe_sql(
        "SELECT count(*) FROM pg_available_extensions "
        "WHERE name = 'injection_points'"
    ) == "0":
        pytest.skip("Extension injection_points not installed")

    node.safe_sql("CREATE EXTENSION injection_points")

    # Create a physical replication slot.
    node.safe_sql(
        "select pg_create_physical_replication_slot('slot_physical', true)")

    # Advance slot to the current position, just to have everything "valid".
    node.safe_sql(
        "select pg_replication_slot_advance('slot_physical', "
        "pg_current_wal_lsn())")

    # Run checkpoint to flush current state to disk and set a baseline.
    node.safe_sql("checkpoint")

    node.advance_wal(20)

    # Advance slot to the current position, just to have everything "valid".
    node.safe_sql(
        "select pg_replication_slot_advance('slot_physical', "
        "pg_current_wal_lsn())")

    # Run another checkpoint to set a new restore LSN.
    node.safe_sql("checkpoint")

    node.advance_wal(20)

    restart_lsn_init = node.safe_sql(
        "select restart_lsn from pg_replication_slots "
        "where slot_name = 'slot_physical'").strip()
    print(f"# restart lsn before checkpoint: {restart_lsn_init}")

    # Run another checkpoint, this time in the background, and make it wait
    # on the injection point so that the checkpoint stops right before
    # removing old WAL segments.
    print("# starting checkpoint")

    checkpoint = node.connect("postgres")
    checkpoint.do(
        "select injection_points_attach("
        "'checkpoint-before-old-wal-removal', 'wait')")
    checkpoint.do_async("checkpoint")

    # Wait until the checkpoint stops right before removing WAL segments.
    print("# waiting for injection_point")
    node.wait_for_event("checkpointer", "checkpoint-before-old-wal-removal")
    print("# injection_point is reached")

    # OK, we're in the right situation: time to advance the physical slot, which
    # recalculates the required LSN and then unblock the checkpoint, which
    # removes the WAL still needed by the physical slot.
    node.safe_sql(
        "select pg_replication_slot_advance('slot_physical', "
        "pg_current_wal_lsn())")

    # Continue the checkpoint and wait for its completion.
    log_offset = node.log_position()
    node.safe_sql(
        "select injection_points_wakeup("
        "'checkpoint-before-old-wal-removal')")
    node.wait_for_log(r"checkpoint complete", log_offset)

    restart_lsn_old = node.safe_sql(
        "select restart_lsn from pg_replication_slots "
        "where slot_name = 'slot_physical'").strip()
    print(f"# restart lsn before stop: {restart_lsn_old}")

    checkpoint.wait_for_completion()
    checkpoint.close()

    # Abruptly stop the server (1 second should be enough for the checkpoint
    # to finish; it would be better).
    node.stop("immediate")

    node.start()

    # Get the restart_lsn of the slot right after restarting.
    restart_lsn = node.safe_sql(
        "select restart_lsn from pg_replication_slots "
        "where slot_name = 'slot_physical'").strip()
    print(f"# restart lsn: {restart_lsn}")

    # Get the WAL segment name for the slot's restart_lsn.
    restart_lsn_segment = node.safe_sql(
        f"SELECT pg_walfile_name('{restart_lsn}'::pg_lsn)").strip()

    # Check if the required wal segment exists.
    print(f"# required by slot segment name: {restart_lsn_segment}")
    datadir = node.data_dir
    assert os.path.isfile(
        os.path.join(datadir, "pg_wal", restart_lsn_segment)), (
        f"WAL segment {restart_lsn_segment} for physical slot's restart_lsn "
        f"{restart_lsn} exists")
