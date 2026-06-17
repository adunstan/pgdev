# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test that effective_wal_level changes upon logical replication slot creation
and deletion.
"""

import os


def check_wal_level(node, expected, msg):
    """Check both wal_level and effective_wal_level on *node* are *expected*."""
    actual = node.safe_sql(
        "select current_setting('wal_level'), "
        "current_setting('effective_wal_level');"
    )
    assert actual == expected, msg


def wait_for_logical_decoding_disabled(node):
    """Wait for the checkpointer to decrease effective_wal_level to 'replica'."""
    assert node.poll_query_until(
        "select current_setting('effective_wal_level') = 'replica';"
    ), "timed out waiting for logical decoding to be disabled"


def test_051_effective_wal_level(create_pg):
    # Initialize the primary server with wal_level = 'replica'.
    primary = create_pg("primary", allows_streaming=True, start=False)
    primary.append_conf("log_min_messages = debug1")
    primary.start()

    # Check both initial wal_level and effective_wal_level values.
    check_wal_level(
        primary,
        "replica|replica",
        "wal_level and effective_wal_level start with the same value 'replica'",
    )

    # Create a physical slot and verify it doesn't affect effective_wal_level.
    primary.safe_sql(
        "select pg_create_physical_replication_slot('test_phy_slot', false, false)"
    )
    check_wal_level(
        primary,
        "replica|replica",
        "effective_wal_level doesn't change with a new physical slot",
    )
    primary.safe_sql("select pg_drop_replication_slot('test_phy_slot')")

    # Create a temporary logical slot but exit without releasing it explicitly.
    # This enables logical decoding but skips disabling it and delegates to the
    # checkpointer.  The temporary slot is tied to the session's lifetime, so we
    # use a dedicated connection and close it to release the slot (safe_sql
    # reuses a persistent in-process session that would otherwise hold the
    # temp slot indefinitely).
    tmp_sess = primary.connect("postgres")
    tmp_sess.query_safe(
        "select pg_create_logical_replication_slot('test_tmp_slot', "
        "'test_decoding', true)"
    )
    tmp_sess.close()
    assert primary.log_contains(
        "logical decoding is enabled upon creating a new logical replication slot"
    ), "logical decoding has been enabled upon creating a temp slot"

    # Wait for the checkpointer to disable logical decoding.
    wait_for_logical_decoding_disabled(primary)

    # Test that logical decoding is disabled after repack
    primary.safe_sql("create table foo(a int primary key)")
    primary.safe_sql("repack (concurrently) foo;")
    assert primary.log_contains(
        "logical decoding is enabled upon creating a new logical replication slot"
    ), "logical decoding enabled by repack"

    # Wait for the checkpointer to disable logical decoding.
    wait_for_logical_decoding_disabled(primary)
    check_wal_level(
        primary, "replica|replica", "logical decoding disabled after repack"
    )

    # Create a new logical slot and check that effective_wal_level must be
    # increased to 'logical'.
    primary.safe_sql(
        "select pg_create_logical_replication_slot('test_slot', 'pgoutput')"
    )
    check_wal_level(
        primary,
        "replica|logical",
        "effective_wal_level increased to 'logical' upon a logical slot creation",
    )

    # Restart the server and check again.
    primary.restart()
    check_wal_level(
        primary,
        "replica|logical",
        "effective_wal_level remains 'logical' even after a server restart",
    )

    # Create and drop another logical slot, then verify that
    # effective_wal_level remains 'logical'.
    primary.safe_sql(
        "select pg_create_logical_replication_slot('test_slot2', 'pgoutput')"
    )
    primary.safe_sql("select pg_drop_replication_slot('test_slot2')")
    check_wal_level(
        primary,
        "replica|logical",
        "effective_wal_level stays 'logical' as one slot remains",
    )

    # Verify that the server cannot start with wal_level='minimal' when there
    # is at least one replication slot.
    primary.append_conf("wal_level = minimal")
    primary.append_conf("max_wal_senders = 0")
    primary.stop()

    log_offset = primary.log_position()
    started = primary.start(fail_ok=True)
    assert not started, (
        "cannot start server with wal_level='minimal' as there is in-use "
        "logical slot"
    )

    assert primary.log_contains(
        'logical replication slot "test_slot" exists, but "wal_level" < "replica"',
        log_offset,
    ), "logical slots requires logical decoding enabled at server startup"

    # Revert the modified settings.
    primary.append_conf("wal_level = replica")
    primary.append_conf("max_wal_senders = 10")

    # Add other settings to test if we disable logical decoding when
    # invalidating the last logical slot.
    primary.append_conf(
        "\n".join(
            [
                "min_wal_size = 32MB",
                "max_wal_size = 32MB",
                "max_slot_wal_keep_size = 16MB",
                "",
            ]
        )
    )
    primary.start()

    # Advance WAL and verify that the slot gets invalidated.
    primary.advance_wal(2)
    primary.safe_sql("CHECKPOINT")
    assert (
        primary.safe_sql(
            "select invalidation_reason = 'wal_removed' from "
            "pg_replication_slots where slot_name = 'test_slot';"
        )
        == "t"
    ), "test_slot gets invalidated due to wal_removed"

    # Verify that logical decoding is disabled after invalidating the last
    # logical slot.
    wait_for_logical_decoding_disabled(primary)
    check_wal_level(
        primary,
        "replica|replica",
        "effective_wal_level got decreased to 'replica' after invalidating "
        "the last logical slot",
    )

    # Revert the modified settings, and restart the server.  (There is no
    # adjust_conf in this framework; appending wins, so re-append defaults.)
    primary.append_conf(
        "\n".join(
            [
                "max_slot_wal_keep_size = -1",
                "min_wal_size = 80MB",
                "max_wal_size = 128MB",
                "",
            ]
        )
    )
    primary.restart()

    # Recreate the logical slot to enable logical decoding again.
    primary.safe_sql("select pg_drop_replication_slot('test_slot')")
    primary.safe_sql(
        "select pg_create_logical_replication_slot('test_slot', 'pgoutput')"
    )

    # Take backup during the effective_wal_level being 'logical'.  But note
    # that replication slots are not included in the backup.
    primary.backup("my_backup")

    # Initialize standby1 node.
    standby1 = create_pg("standby1", start=False)
    standby1.init_from_backup(primary, "my_backup", has_streaming=True)
    standby1.start()

    # Creating a logical slot on standby should succeed as the primary
    # enables it.
    primary.wait_for_replay_catchup(standby1)
    standby1.create_logical_slot_on_standby(primary, "standby1_slot", "postgres")

    # Promote the standby1 node that has one logical slot. So
    # effective_wal_level remains 'logical' even after the promotion.
    standby1.promote()
    check_wal_level(
        standby1,
        "replica|logical",
        "effective_wal_level remains 'logical' even after the promotion",
    )

    # Confirm if we can create a logical slot after the promotion.
    standby1.safe_sql(
        "select pg_create_logical_replication_slot('standby1_slot2', 'pgoutput')"
    )
    standby1.stop()

    # Initialize standby2 node and start it with wal_level = 'logical'.
    standby2 = create_pg("standby2", start=False)
    standby2.init_from_backup(primary, "my_backup", has_streaming=True)
    standby2.append_conf("wal_level = 'logical'")
    standby2.start()
    standby2.backup("my_backup3")

    # Initialize cascade standby and start with wal_level = 'replica'.
    cascade = create_pg("cascade", start=False)
    cascade.init_from_backup(standby2, "my_backup3", has_streaming=True)
    cascade.append_conf("wal_level = replica")
    cascade.start()

    # Regardless of their wal_level values, effective_wal_level values on the
    # standby and the cascaded standby depend on the primary's value,
    # 'logical'.
    check_wal_level(
        standby2,
        "logical|logical",
        "check wal_level and effective_wal_level on standby",
    )
    check_wal_level(
        cascade,
        "replica|logical",
        "check wal_level and effective_wal_level on cascaded standby",
    )

    # Drop the primary's last logical slot, decreasing effective_wal_level to
    # 'replica' on all nodes.
    primary.safe_sql("select pg_drop_replication_slot('test_slot')")
    wait_for_logical_decoding_disabled(primary)

    primary.wait_for_replay_catchup(standby2)
    standby2.wait_for_replay_catchup(cascade, primary)

    check_wal_level(
        primary,
        "replica|replica",
        "effective_wal_level got decreased to 'replica' on primary",
    )
    check_wal_level(
        standby2,
        "logical|replica",
        "effective_wal_level got decreased to 'replica' on standby",
    )
    check_wal_level(
        cascade,
        "replica|replica",
        "effective_wal_level got decreased to 'replica' on cascaded standby",
    )

    # Promote standby2, increasing effective_wal_level to 'logical' as its
    # wal_level is set to 'logical'.
    standby2.promote()

    # Verify that effective_wal_level is increased to 'logical' on the cascaded
    # standby.
    standby2.wait_for_replay_catchup(cascade)
    check_wal_level(
        cascade,
        "replica|logical",
        "effective_wal_level got increased to 'logical' on standby as the new "
        "primary has wal_level='logical'",
    )

    standby2.stop()
    cascade.stop()

    # Initialize standby3 node and start it.
    standby3 = create_pg("standby3", start=False)
    standby3.init_from_backup(primary, "my_backup", has_streaming=True)
    standby3.start()

    # Create logical slots on both nodes.
    primary.safe_sql(
        "select pg_create_logical_replication_slot('test_slot', 'pgoutput')"
    )
    primary.wait_for_replay_catchup(standby3)
    standby3.create_logical_slot_on_standby(primary, "standby3_slot", "postgres")

    # Drop the logical slot from the primary, decreasing effective_wal_level to
    # 'replica' on the primary, which leads to invalidating the logical slot on
    # the standby due to 'wal_level_insufficient'.
    primary.safe_sql("select pg_drop_replication_slot('test_slot')")
    wait_for_logical_decoding_disabled(primary)
    check_wal_level(
        primary,
        "replica|replica",
        "effective_wal_level got decreased to 'replica' on the primary to "
        "invalidate standby's slots",
    )
    assert standby3.poll_query_until(
        "select invalidation_reason = 'wal_level_insufficient' from "
        "pg_replication_slots where slot_name = 'standby3_slot'"
    ), "timed out waiting for standby3_slot to be invalidated"

    # Restart the server to verify that the slot is successfully restored
    # during startup.
    standby3.restart()

    # Check that the logical decoding is not enabled on the standby3. Note that
    # it still has the invalidated logical slot.
    check_wal_level(
        standby3,
        "replica|replica",
        "effective_wal_level got decreased to 'replica' on standby",
    )

    res = standby3.sql(
        "select pg_logical_slot_get_changes('standby3_slot', null, null)"
    )
    assert res.error_message is not None
    assert (
        'logical decoding on standby requires "effective_wal_level" >= '
        '"logical" on the primary' in res.error_message
    ), "cannot use logical decoding on standby as it is disabled on primary"

    # Restart the primary with setting wal_level = 'logical' and create a new
    # logical slot.
    primary.append_conf("wal_level = 'logical'")
    primary.restart()
    primary.safe_sql(
        "select pg_create_logical_replication_slot('test_slot', 'pgoutput')"
    )

    # effective_wal_level should be 'logical' on both nodes.
    primary.wait_for_replay_catchup(standby3)
    check_wal_level(primary, "logical|logical", "check WAL levels on the primary node")
    check_wal_level(
        standby3,
        "replica|logical",
        "effective_wal_level got increased to 'logical' again on standby",
    )

    # Set wal_level to 'replica' and restart the primary. Since one logical
    # slot is still present on the primary, effective_wal_level remains
    # 'logical' even if wal_level got decreased to 'replica'.
    primary.append_conf("wal_level = replica")
    primary.restart()
    primary.wait_for_replay_catchup(standby3)

    # Verify that the effective_wal_level remains 'logical' on both nodes
    check_wal_level(
        primary,
        "replica|logical",
        "effective_wal_level remains 'logical' on primary even after setting "
        "wal_level to 'replica'",
    )
    check_wal_level(
        standby3,
        "replica|logical",
        "effective_wal_level remains 'logical' on standby even after setting "
        "wal_level to 'replica' on primary",
    )

    # Promote the standby3 and verify that effective_wal_level got decreased to
    # 'replica' after the promotion since there is no valid logical slot.
    standby3.promote()
    check_wal_level(
        standby3,
        "replica|replica",
        "effective_wal_level got decreased to 'replica' as there is no valid "
        "logical slot",
    )

    # Cleanup the invalidated slot.
    standby3.safe_sql("select pg_drop_replication_slot('standby3_slot')")

    standby3.stop()

    # Test the race condition at end of the recovery between the startup and
    # logical decoding status change. This test requires injection points
    # enabled.
    injection_available = (
        os.environ.get("enable_injection_points", "no") == "yes"
        and primary.safe_sql(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        != "0"
    )
    if injection_available:
        # Initialize standby4 and start it.
        standby4 = create_pg("standby4", start=False)
        standby4.init_from_backup(primary, "my_backup", has_streaming=True)
        standby4.start()

        # Both servers have one logical slot.
        primary.wait_for_replay_catchup(standby4)
        standby4.create_logical_slot_on_standby(primary, "standby4_slot", "postgres")

        # Enable and attach the injection point on the standby4.
        primary.safe_sql("create extension injection_points")
        primary.wait_for_replay_catchup(standby4)
        standby4.safe_sql(
            "select injection_points_attach("
            "'startup-logical-decoding-status-change-end-of-recovery', "
            "'wait');"
        )

        # Trigger promotion with no wait, and wait for the startup process to
        # reach the injection point.
        standby4.safe_sql("select pg_promote(false)")
        print("# promote the standby and waiting for injection_point")
        standby4.wait_for_event(
            "startup", "startup-logical-decoding-status-change-end-of-recovery"
        )
        print(
            "# injection_point "
            "'startup-logical-decoding-status-change-end-of-recovery' "
            "is reached"
        )

        # Drop the logical slot, requesting to disable logical decoding to the
        # checkpointer.
        standby4.safe_sql("select pg_drop_replication_slot('standby4_slot');")

        # Resume the startup process to complete the recovery.
        standby4.safe_sql(
            "select injection_points_wakeup("
            "'startup-logical-decoding-status-change-end-of-recovery')"
        )

        # Verify that logical decoding got disabled after the recovery.
        wait_for_logical_decoding_disabled(standby4)
        check_wal_level(
            standby4,
            "replica|replica",
            "effective_wal_level properly got decreased to 'replica'",
        )
        standby4.stop()

        # Test the abort process of logical decoding activation. We drop the
        # primary's slot to decrease its effective_wal_level to 'replica'.
        primary.safe_sql("select pg_drop_replication_slot('test_slot')")
        wait_for_logical_decoding_disabled(primary)
        check_wal_level(
            primary,
            "replica|replica",
            "effective_wal_level got decreased to 'replica' on primary",
        )

        # Start a session to test the case where the activation process is
        # interrupted.
        psql_create_slot = primary.connect("postgres")

        # Set up the injection point in this session (using set_local so it
        # only affects this session), then start the slot creation which will
        # block.
        psql_create_slot.do("select injection_points_set_local()")
        psql_create_slot.do(
            "select injection_points_attach('logical-decoding-activation', 'wait')"
        )
        psql_create_slot.do_async(
            "select pg_create_logical_replication_slot('slot_canceled', 'pgoutput')"
        )

        primary.wait_for_event("client backend", "logical-decoding-activation")
        print("# injection_point 'logical-decoding-activation' is reached")

        # Cancel the backend initiated by psql_create_slot, aborting its
        # activation process.
        primary.safe_sql(
            "select pg_cancel_backend(pid) from pg_stat_activity where "
            "query ~ 'slot_canceled' and pid <> pg_backend_pid()"
        )

        # Verify that the backend aborted the activation process.
        primary.wait_for_log("aborting logical decoding activation process")
        check_wal_level(primary, "replica|replica", "the activation process aborted")

        # Clean up the session (the async query was cancelled, so we just
        # close)
        psql_create_slot.close()

    primary.stop()
