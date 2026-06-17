# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test for replication slots invalidation due to idle_timeout."""

import pytest

from libpq.errors import QueryError


def wait_for_slot_invalidation(node, slot_name, offset):
    """Wait for slot to first become idle and then get invalidated."""
    # The slot's invalidation should be logged
    node.wait_for_log(rf'invalidating obsolete replication slot "{slot_name}"', offset)

    # Check that the invalidation reason is 'idle_timeout'
    if not node.poll_query_until(
        f"""
        SELECT COUNT(slot_name) = 1 FROM pg_replication_slots
            WHERE slot_name = '{slot_name}' AND
            invalidation_reason = 'idle_timeout';
    """
    ):
        raise TimeoutError(
            "Timed out while waiting for invalidation reason of slot "
            f"{slot_name} to be set on node {node.name}"
        )


def test_044_invalidate_inactive_slots(create_pg):
    # ====================================================================
    # Testcase start
    #
    # Test invalidation of physical replication slot and logical replication
    # slot due to idle timeout.

    # Initialize the node
    node = create_pg("node", allows_streaming="logical", start=False)

    # Avoid unpredictability
    node.append_conf(
        """
checkpoint_timeout = 1h
idle_replication_slot_timeout = 1min
"""
    )
    node.start()

    # This test depends on injection point that forces slot invalidation
    # due to idle_timeout.  Check if the 'injection_points' extension is
    # available, as it may be possible that this script is run with
    # installcheck, where the module would not be installed by default.
    if (
        node.safe_sql(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        == "0"
    ):
        pytest.skip("Extension injection_points not installed")

    # Create both physical and logical replication slots
    node.safe_sql(
        """
        SELECT pg_create_physical_replication_slot(slot_name := 'physical_slot', immediately_reserve := true);
        SELECT pg_create_logical_replication_slot('logical_slot', 'test_decoding');
"""
    )

    log_offset = node.log_position()

    # Register an injection point on the node to forcibly cause a slot
    # invalidation due to idle_timeout
    node.safe_sql("CREATE EXTENSION injection_points;")

    node.safe_sql("SELECT injection_points_attach('slot-timeout-inval', 'error');")

    # Slot invalidation occurs during a checkpoint, so perform a checkpoint to
    # invalidate the slots.
    node.safe_sql("CHECKPOINT")

    # Wait for slots to become inactive. Since nobody has acquired the slot
    # yet, it can only be due to the idle timeout mechanism.
    wait_for_slot_invalidation(node, "physical_slot", log_offset)
    wait_for_slot_invalidation(node, "logical_slot", log_offset)

    # Check that the invalidated slot cannot be acquired
    sess = node.connect()
    try:
        with pytest.raises(QueryError) as excinfo:
            sess.query_safe(
                "SELECT pg_replication_slot_advance('logical_slot', '0/1');"
            )
        assert 'can no longer access replication slot "logical_slot"' in str(
            excinfo.value
        ), "detected error upon trying to acquire invalidated slot on node"
    finally:
        sess.close()

    # Testcase end
    # =====================================================================
