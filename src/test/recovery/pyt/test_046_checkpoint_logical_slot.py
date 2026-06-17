# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""This test verifies the case when the logical slot is advanced during
checkpoint. The test checks that the logical slot's restart_lsn still refers
to an existed WAL segment after immediate restart.
"""

import pytest


def test_046_checkpoint_logical_slot(create_pg):
    node = create_pg("mike", start=False, allows_streaming="logical")
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

    # Create the two slots we'll need.
    node.safe_sql(
        "select pg_create_logical_replication_slot('slot_logical', 'test_decoding')"
    )
    node.safe_sql("select pg_create_physical_replication_slot('slot_physical', true)")

    # Advance both slots to the current position just to have everything
    # "valid".
    node.safe_sql(
        "select count(*) from pg_logical_slot_get_changes('slot_logical', "
        "null, null)"
    )
    node.safe_sql(
        "select pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())"
    )

    # Run checkpoint to flush current state to disk and set a baseline.
    node.safe_sql("checkpoint")

    # Generate some transactions to get RUNNING_XACTS.
    for _ in range(10):
        node.safe_sql("SELECT 1")

    node.advance_wal(20)

    # Run another checkpoint to set a new restore LSN.
    node.safe_sql("checkpoint")

    node.advance_wal(20)

    # Run another checkpoint, this time in the background, and make it wait
    # on the injection point so that the checkpoint stops right before
    # removing old WAL segments.
    print("# starting checkpoint")

    node.safe_sql(
        "select injection_points_attach('checkpoint-before-old-wal-removal', 'wait')"
    )
    checkpoint = node.connect("postgres")
    checkpoint.do_async("CHECKPOINT;")

    # Wait until the checkpoint stops right before removing WAL segments.
    print("# waiting for injection_point")
    node.wait_for_event("checkpointer", "checkpoint-before-old-wal-removal")
    print("# injection_point is reached")

    # Try to advance the logical slot, but make it stop when it moves to the
    # next WAL segment (this has to happen in the background, too).
    # We need to call pg_logical_slot_get_changes repeatedly until the slot
    # advances to the next segment and hits the injection point.
    logical = node.connect("postgres")
    logical.do(
        "select injection_points_attach("
        "'logical-replication-slot-advance-segment', 'wait');"
    )
    logical.do_async(
        "DO $$\n"
        "BEGIN\n"
        "\tLOOP\n"
        "\t\tPERFORM count(*) FROM "
        "pg_logical_slot_get_changes('slot_logical', null, null);\n"
        "\t\tPERFORM pg_sleep(0.1);\n"
        "\tEND LOOP;\n"
        "END $$;"
    )

    # Wait until the slot's restart_lsn points to the next WAL segment.
    print("# waiting for injection_point")
    node.wait_for_event("client backend", "logical-replication-slot-advance-segment")
    print("# injection_point is reached")

    # OK, we're in the right situation: time to advance the physical slot, which
    # recalculates the required LSN, and then unblock the checkpoint, which
    # removes the WAL still needed by the logical slot.
    node.safe_sql(
        "select pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())"
    )

    # Generate a long WAL record, spawning at least two pages for the follow-up
    # post-recovery check.
    node.safe_sql(
        "select pg_logical_emit_message(false, '', repeat('123456789', 1000))"
    )

    # Continue the checkpoint and wait for its completion.
    log_offset = node.log_position()
    node.safe_sql("select injection_points_wakeup('checkpoint-before-old-wal-removal')")
    node.wait_for_log(r"checkpoint complete", log_offset)

    # Abruptly stop the server.
    node.stop("immediate")

    node.start()

    # Logical slot should still be valid after the crash restart: reading from
    # it must not raise (its restart_lsn must refer to an existing WAL segment).
    node.safe_sql(
        "select count(*) from pg_logical_slot_get_changes('slot_logical', "
        "null, null);"
    )

    # Sessions were terminated by the server crash; close them so the framework
    # does not try to reuse the dead connections.
    checkpoint.close()
    logical.close()

    # Verify that the synchronized slots won't be invalidated immediately after
    # synchronization in the presence of a concurrent checkpoint.
    primary = node

    primary.append_conf("autovacuum = off")
    primary.reload()

    backup_name = "backup"

    primary.backup(backup_name)

    # Create a standby
    standby = create_pg("standby", start=False)
    standby.init_from_backup(primary, backup_name, has_streaming=1)

    # PostgresServer.connstr() quotes values and adds dbname, which would break
    # embedding inside primary_conninfo = '...'.  Build an unquoted conninfo.
    connstr_1 = f"port={primary.port} host={primary.host}"
    standby.append_conf(
        "hot_standby_feedback = on\n"
        "primary_slot_name = 'phys_slot'\n"
        f"primary_conninfo = '{connstr_1} dbname=postgres'\n"
    )

    primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('failover_slot', "
        "'test_decoding', false, false, true);"
    )
    primary.safe_sql("SELECT pg_create_physical_replication_slot('phys_slot');")

    standby.start()

    # Generate some activity and switch WAL file on the primary
    primary.advance_wal(1)
    primary.safe_sql("CHECKPOINT")
    primary.wait_for_replay_catchup(standby)

    # checkpoint on the standby and make it wait on the injection point so that
    # the checkpoint stops right before invalidating replication slots.
    print("# starting checkpoint")

    standby.safe_sql(
        "select injection_points_attach("
        "'restartpoint-before-slot-invalidation', 'wait')"
    )
    standby_checkpoint = standby.connect("postgres")
    standby_checkpoint.do_async("CHECKPOINT;")

    # Wait until the checkpoint stops right before invalidating slots
    print("# waiting for injection_point")
    standby.wait_for_event("checkpointer", "restartpoint-before-slot-invalidation")
    print("# injection_point is reached")

    # Enable slot sync worker to synchronize the failover slot to the standby
    standby.append_conf("sync_replication_slots = on")
    standby.reload()

    # Wait for the slot to be synced
    assert standby.poll_query_until(
        "SELECT COUNT(*) > 0 FROM pg_replication_slots "
        "WHERE slot_name = 'failover_slot'"
    )

    # Release the checkpointer
    standby.safe_sql(
        "select injection_points_wakeup('restartpoint-before-slot-invalidation')"
    )
    standby.safe_sql(
        "select injection_points_detach('restartpoint-before-slot-invalidation')"
    )

    standby_checkpoint.wait_for_completion()
    standby_checkpoint.close()

    # Confirm that the slot is not invalidated
    assert (
        standby.safe_sql(
            "SELECT invalidation_reason IS NULL AND synced "
            "FROM pg_replication_slots WHERE slot_name = 'failover_slot';"
        )
        == "t"
    ), "logical slot is not invalidated"
