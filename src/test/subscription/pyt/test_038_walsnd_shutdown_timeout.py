# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test publisher shutdown with wal_sender_shutdown_timeout set.

Checks that the publisher is able to shut down without waiting for sending of
all pending data to the subscriber.
"""

import os
import re
import signal
import time

from pypg.util import TIMEOUT_DEFAULT, WINDOWS_OS

WALSENDER_TIMEOUT_PATTERN = (
    "WARNING: .* terminating walsender process due to replication shutdown timeout"
)


def test_038_walsnd_shutdown_timeout(create_pg):
    # Initialize publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical", start=False)
    node_publisher.append_conf(
        """wal_sender_timeout = 1h
 wal_sender_shutdown_timeout = 10ms"""
    )
    node_publisher.start()

    # Initialize subscriber node
    node_subscriber = create_pg("subscriber")

    # Create publication for test table
    node_publisher.safe_sql("CREATE TABLE test_tab (id int PRIMARY KEY);")
    node_publisher.safe_sql("CREATE PUBLICATION test_pub FOR TABLE test_tab;")

    # Create matching table and subscription on subscriber.  These are issued
    # as separate statements because the in-process Session would wrap
    # a multi-statement string in one implicit transaction, and CREATE
    # SUBSCRIPTION (create_slot = true) cannot run inside a transaction block,
    # so issue each statement separately.
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} " "dbname=postgres"
    )
    node_subscriber.safe_sql("CREATE TABLE test_tab (id int PRIMARY KEY);")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION test_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION test_pub WITH (failover = true);"
    )

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "test_sub")

    # Start a background session on the subscriber to run a transaction later
    # that will block the logical apply worker on a lock.
    sub_session = node_subscriber.connect()

    # Test that when the logical apply worker is blocked on a lock and
    # replication is stalled, shutting down the publisher causes the logical
    # walsender to exit due to wal_sender_shutdown_timeout, allowing shutdown
    # to complete.

    # Cause the logical apply worker to block on a lock by running conflicting
    # transactions on the publisher and subscriber.
    sub_session.do("BEGIN; INSERT INTO test_tab VALUES (0);")
    node_publisher.safe_sql("INSERT INTO test_tab VALUES (0);")

    log_offset = node_publisher.log_position()

    # Verify that the walsender exits due to wal_sender_shutdown_timeout.
    node_publisher.stop("fast")
    assert node_publisher.log_contains(
        WALSENDER_TIMEOUT_PATTERN, log_offset
    ), "walsender exits due to wal_sender_shutdown_timeout"

    sub_session.do("ABORT;")
    node_publisher.start()
    node_publisher.wait_for_catchup("test_sub")

    # Test that when the logical apply worker is blocked on a lock, replication
    # is stalled, and the logical walsender's output buffer is full, shutting
    # down the publisher causes the walsender to exit due to
    # wal_sender_shutdown_timeout, allowing shutdown to complete.
    #
    # This test differs from the previous one in that the walsender's output
    # buffer is full (because pending data cannot be transferred).

    # Run a transaction on the subscriber that blocks the logical apply worker
    # on a lock.
    sub_session.do("BEGIN; LOCK TABLE test_tab IN EXCLUSIVE MODE;")

    # Generate enough data to fill the logical walsender's output buffer.
    node_publisher.safe_sql("INSERT INTO test_tab VALUES (generate_series(1, 20000));")

    # Wait for the logical walsender's output buffer to fill. If the WAL send
    # positions do not advance between checks, treat the buffer as full.
    last_sent_lsn = node_publisher.safe_sql(
        "SELECT sent_lsn FROM pg_stat_replication "
        "WHERE application_name = 'test_sub';"
    )

    max_attempts = TIMEOUT_DEFAULT * 10
    while max_attempts >= 0:
        max_attempts -= 1
        time.sleep(0.1)

        cur_sent_lsn = node_publisher.safe_sql(
            "SELECT sent_lsn FROM pg_stat_replication "
            "WHERE application_name = 'test_sub';"
        )

        diff = node_publisher.safe_sql(
            f"SELECT pg_wal_lsn_diff('{cur_sent_lsn}', '{last_sent_lsn}');"
        )
        if int(diff) == 0:
            break

        last_sent_lsn = cur_sent_lsn

    log_offset = node_publisher.log_position()

    # Verify that the walsender exits due to wal_sender_shutdown_timeout.
    node_publisher.stop("fast")
    assert node_publisher.log_contains(
        WALSENDER_TIMEOUT_PATTERN, log_offset
    ), "walsender with full output buffer exits due to wal_sender_shutdown_timeout"

    sub_session.do("ABORT;")

    # The remaining scenario stalls physical replication by sending SIGSTOP to
    # the standby's walreceiver, which is not portable to Windows; end the test
    # here on that platform.
    if WINDOWS_OS:
        return

    node_publisher.start()

    # Test that wal_sender_shutdown_timeout works correctly when both physical
    # and logical replication are active, and slot synchronization is running
    # on the standby.
    #
    # In this scenario, the logical apply worker is blocked on a lock and
    # the standby's walreceiver is stopped (via SIGSTOP signal), stalling both
    # replication streams. Verify that shutting down the publisher (primary)
    # causes both physical and logical walsenders to exit due to
    # wal_sender_shutdown_timeout, allowing shutdown to complete.

    # Create the standby with slot synchronization enabled.
    node_publisher.backup(
        "publisher_backup",
        backup_options=[
            "--create-slot",
            "--slot",
            "test_slot",
            "-d",
            "dbname=postgres",
            "--write-recovery-conf",
        ],
    )

    node_publisher.append_conf("synchronized_standby_slots = 'test_slot'")
    node_publisher.reload()

    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_publisher, "publisher_backup")
    node_standby.append_conf(
        """sync_replication_slots = on
hot_standby_feedback = on"""
    )
    node_standby.start()

    # Cause the logical apply worker to block on a lock by running conflicting
    # transactions on the publisher and subscriber, stalling logical
    # replication.
    node_publisher.wait_for_catchup("test_sub")
    sub_session.do("BEGIN; LOCK TABLE test_tab IN EXCLUSIVE MODE;")
    node_publisher.safe_sql("INSERT INTO test_tab VALUES (-1); ")

    # Cause the standby's walreceiver to be blocked with SIGSTOP signal,
    # stalling physical replication.
    assert node_standby.poll_query_until(
        "SELECT EXISTS(SELECT 1 FROM pg_stat_wal_receiver)"
    )
    receiverpid = node_standby.safe_sql("SELECT pid FROM pg_stat_wal_receiver")
    assert re.match(r"^[0-9]+$", receiverpid), f"have walreceiver pid {receiverpid}"
    os.kill(int(receiverpid), signal.SIGSTOP)

    log_offset = node_publisher.log_position()

    # Verify that the walsender exits due to wal_sender_shutdown_timeout
    # even when both physical and logical replication are stalled.
    node_publisher.safe_sql("INSERT INTO test_tab VALUES (-2);")
    node_publisher.stop("fast")
    assert node_publisher.log_contains(WALSENDER_TIMEOUT_PATTERN, log_offset), (
        "walsender exits due to wal_sender_shutdown_timeout even when both "
        "physical and logical replication are stalled"
    )

    os.kill(int(receiverpid), signal.SIGCONT)
    sub_session.close()

    node_subscriber.stop("fast")
    node_standby.stop("fast")
