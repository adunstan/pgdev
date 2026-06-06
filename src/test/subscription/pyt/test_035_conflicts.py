# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test conflicts in logical replication."""

import re


def psql_stderr(node, sql):
    """Run *sql* on *node* and return the client-side stderr.

    ERROR/WARNING/NOTICE messages emitted by the server are captured here
    (notices via the notice processor, the ERROR via the session's last
    error).
    """
    sess = node.connect()
    try:
        sess.query(sql)
        return sess.get_stderr()
    finally:
        sess.close()


def wait_apply_worker_stopped(node):
    """Wait for the logical replication apply worker to stop on *node*."""
    node.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity "
        "WHERE backend_type = 'logical replication apply worker'"
    )


def test_035_conflicts(create_pg):
    ###############################
    # Setup
    ###############################

    # Create a publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical")

    # Create a subscriber node
    node_subscriber = create_pg("subscriber", allows_streaming="logical")

    # Create a table on publisher
    node_publisher.safe_sql(
        "CREATE TABLE conf_tab (a int PRIMARY KEY, b int UNIQUE, c int UNIQUE);"
    )

    node_publisher.safe_sql(
        "CREATE TABLE conf_tab_2 (a int PRIMARY KEY, b int UNIQUE, c int UNIQUE);"
    )

    # Create same table on subscriber
    node_subscriber.safe_sql(
        "CREATE TABLE conf_tab (a int PRIMARY key, b int UNIQUE, c int UNIQUE);"
    )

    node_subscriber.safe_sql(
        """
         CREATE TABLE conf_tab_2 (a int PRIMARY KEY, b int, c int, unique(a,b)) PARTITION BY RANGE (a);
         CREATE TABLE conf_tab_2_p1 PARTITION OF conf_tab_2 FOR VALUES FROM (MINVALUE) TO (100);
    """
    )

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql(
        "CREATE PUBLICATION pub_tab FOR TABLE conf_tab, conf_tab_2"
    )

    # Create the subscription
    appname = "sub_tab"
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub_tab "
        f"CONNECTION '{publisher_connstr} application_name={appname}' "
        "PUBLICATION pub_tab;"
    )

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, appname)

    ##################################################
    # INSERT data on Pub and Sub
    ##################################################

    # Insert data in the publisher table
    node_publisher.safe_sql("INSERT INTO conf_tab VALUES (1,1,1);")

    # Insert data in the subscriber table
    node_subscriber.safe_sql(
        "INSERT INTO conf_tab VALUES (2,2,2), (3,3,3), (4,4,4);"
    )

    ##################################################
    # Test multiple_unique_conflicts due to INSERT
    ##################################################
    log_offset = node_subscriber.log_position()

    node_publisher.safe_sql("INSERT INTO conf_tab VALUES (2,3,4);")

    # Confirm that this causes an error on the subscriber
    node_subscriber.wait_for_log(
        r'conflict detected on relation "public.conf_tab": conflict=multiple_unique_conflicts.*\n'
        r".*Could not apply remote change: remote row \(2, 3, 4\).*\n"
        r'.*Key already exists in unique index "conf_tab_pkey", modified in transaction .*: key \(a\)=\(2\), local row \(2, 2, 2\).*\n'
        r'.*Key already exists in unique index "conf_tab_b_key", modified in transaction .*: key \(b\)=\(3\), local row \(3, 3, 3\).*\n'
        r'.*Key already exists in unique index "conf_tab_c_key", modified in transaction .*: key \(c\)=\(4\), local row \(4, 4, 4\).',
        log_offset,
    )

    # pass('multiple_unique_conflicts detected during insert')

    # Truncate table to get rid of the error
    node_subscriber.safe_sql("TRUNCATE conf_tab;")

    ##################################################
    # Test multiple_unique_conflicts due to UPDATE
    ##################################################
    log_offset = node_subscriber.log_position()

    # Insert data in the publisher table
    node_publisher.safe_sql("INSERT INTO conf_tab VALUES (5,5,5);")

    # Insert data in the subscriber table
    node_subscriber.safe_sql(
        "INSERT INTO conf_tab VALUES (6,6,6), (7,7,7), (8,8,8);"
    )

    node_publisher.safe_sql("UPDATE conf_tab set a=6, b=7, c=8 where a=5;")

    # Confirm that this causes an error on the subscriber
    node_subscriber.wait_for_log(
        r'conflict detected on relation "public.conf_tab": conflict=multiple_unique_conflicts.*\n'
        r".*Could not apply remote change: remote row \(6, 7, 8\), replica identity \(a\)=\(5\).*\n"
        r'.*Key already exists in unique index "conf_tab_pkey", modified in transaction .*: key \(a\)=\(6\), local row \(6, 6, 6\).*\n'
        r'.*Key already exists in unique index "conf_tab_b_key", modified in transaction .*: key \(b\)=\(7\), local row \(7, 7, 7\).*\n'
        r'.*Key already exists in unique index "conf_tab_c_key", modified in transaction .*: key \(c\)=\(8\), local row \(8, 8, 8\).',
        log_offset,
    )

    # pass('multiple_unique_conflicts detected during update')

    # Truncate table to get rid of the error
    node_subscriber.safe_sql("TRUNCATE conf_tab;")

    ##################################################
    # Test multiple_unique_conflicts due to INSERT on a leaf partition
    ##################################################

    # Insert data in the subscriber table
    node_subscriber.safe_sql("INSERT INTO conf_tab_2 VALUES (55,2,3);")

    # Insert data in the publisher table
    node_publisher.safe_sql("INSERT INTO conf_tab_2 VALUES (55,2,3);")

    node_subscriber.wait_for_log(
        r'conflict detected on relation "public.conf_tab_2_p1": conflict=multiple_unique_conflicts.*\n'
        r".*Could not apply remote change: remote row \(55, 2, 3\).*\n"
        r'.*Key already exists in unique index "conf_tab_2_p1_pkey", modified in transaction .*: key \(a\)=\(55\), local row \(55, 2, 3\).*\n'
        r'.*Key already exists in unique index "conf_tab_2_p1_a_b_key", modified in transaction .*: key \(a, b\)=\(55, 2\), local row \(55, 2, 3\).',
        log_offset,
    )

    # pass('multiple_unique_conflicts detected on a leaf partition during insert')

    ###########################################################################
    # Setup a bidirectional logical replication between node_A & node_B
    ###########################################################################

    # Initialize nodes. Enable the track_commit_timestamp on both nodes to
    # detect the conflict when attempting to update a row that was previously
    # modified by a different origin.

    # node_A. Increase the log_min_messages setting to DEBUG2 to debug test
    # failures. Disable autovacuum to avoid generating xid that could affect
    # the replication slot's xmin value.
    node_A = node_publisher
    node_A.append_conf(
        """track_commit_timestamp = on
        autovacuum = off
        log_min_messages = 'debug2'"""
    )
    node_A.restart()

    # node_B
    node_B = node_subscriber
    node_B.append_conf("track_commit_timestamp = on")
    node_B.restart()

    # Create table on node_A
    node_A.safe_sql("CREATE TABLE tab (a int PRIMARY KEY, b int)")

    # Create the same table on node_B
    node_B.safe_sql("CREATE TABLE tab (a int PRIMARY KEY, b int)")

    subname_AB = "tap_sub_a_b"
    subname_BA = "tap_sub_b_a"

    # Setup logical replication
    # node_A (pub) -> node_B (sub)
    node_A_connstr = f"host={node_A.host} port={node_A.port} dbname=postgres"
    node_A.safe_sql("CREATE PUBLICATION tap_pub_A FOR TABLE tab")
    node_B.safe_sql(
        f"CREATE SUBSCRIPTION {subname_BA} "
        f"CONNECTION '{node_A_connstr} application_name={subname_BA}' "
        "PUBLICATION tap_pub_A "
        "WITH (origin = none, retain_dead_tuples = true)"
    )

    # node_B (pub) -> node_A (sub)
    node_B_connstr = f"host={node_B.host} port={node_B.port} dbname=postgres"
    node_B.safe_sql("CREATE PUBLICATION tap_pub_B FOR TABLE tab")
    node_A.safe_sql(
        f"CREATE SUBSCRIPTION {subname_AB} "
        f"CONNECTION '{node_B_connstr} application_name={subname_AB}' "
        "PUBLICATION tap_pub_B "
        "WITH (origin = none, copy_data = off)"
    )

    # Wait for initial table sync to finish
    node_A.wait_for_subscription_sync(node_B, subname_AB)
    node_B.wait_for_subscription_sync(node_A, subname_BA)

    # is(1, 1, 'Bidirectional replication setup is complete')

    # Confirm that the conflict detection slot is created on Node B and the
    # xmin value is valid.
    assert node_B.poll_query_until(
        "SELECT xmin IS NOT NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is valid on Node B"

    ##################################################
    # Check that the retain_dead_tuples option can be enabled only for disabled
    # subscriptions. Validate the NOTICE message during the subscription DDL,
    # and ensure the conflict detection slot is created upon enabling the
    # retain_dead_tuples option.
    ##################################################

    # Alter retain_dead_tuples for enabled subscription
    stderr = psql_stderr(
        node_A,
        f"ALTER SUBSCRIPTION {subname_AB} SET (retain_dead_tuples = true)",
    )
    assert re.search(
        r'ERROR:  cannot set option "retain_dead_tuples" for enabled subscription',
        stderr,
    ), "altering retain_dead_tuples is not allowed for enabled subscription"

    # Disable the subscription
    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} DISABLE;")

    # Wait for the apply worker to stop
    wait_apply_worker_stopped(node_A)

    # Enable retain_dead_tuples for disabled subscription
    stderr = psql_stderr(
        node_A,
        f"ALTER SUBSCRIPTION {subname_AB} SET (retain_dead_tuples = true);",
    )
    assert re.search(
        r"NOTICE:  deleted rows to detect conflicts would not be removed until the subscription is enabled",
        stderr,
    ), "altering retain_dead_tuples is allowed for disabled subscription"

    # Re-enable the subscription
    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} ENABLE;")

    # Confirm that the conflict detection slot is created on Node A and the
    # xmin value is valid.
    assert node_A.poll_query_until(
        "SELECT xmin IS NOT NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is valid on Node A"

    ##################################################
    # Check the WARNING when changing the origin to ANY, if retain_dead_tuples
    # is enabled. This warns of the possibility of receiving changes from
    # origins other than the publisher.
    ##################################################

    stderr = psql_stderr(
        node_A, f"ALTER SUBSCRIPTION {subname_AB} SET (origin = any);"
    )
    assert re.search(
        r'WARNING:  subscription "tap_sub_a_b" enabled retain_dead_tuples but might not reliably detect conflicts for changes from different origins',
        stderr,
    ), "warn of the possibility of receiving changes from origins other than the publisher"

    # Reset the origin to none
    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} SET (origin = none);")

    ###########################################################################
    # Check that dead tuples on node A cannot be cleaned by VACUUM until the
    # concurrent transactions on Node B have been applied and flushed on Node A.
    # Also, check that an update_deleted conflict is detected when updating a
    # row that was deleted by a different origin.
    ###########################################################################

    # Insert a record
    node_A.safe_sql("INSERT INTO tab VALUES (1, 1), (2, 2);")
    node_A.wait_for_catchup(subname_BA)

    result = node_B.safe_sql("SELECT * FROM tab;")
    assert result == "1|1\n2|2", "check replicated insert on node B"

    # Disable the logical replication from node B to node A
    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} DISABLE")

    # Wait for the apply worker to stop
    wait_apply_worker_stopped(node_A)

    log_location = node_B.log_position()

    node_B.safe_sql("UPDATE tab SET b = 3 WHERE a = 1;")
    node_A.safe_sql("DELETE FROM tab WHERE a = 1;")

    stderr = psql_stderr(node_A, "VACUUM (verbose) public.tab;")
    assert re.search(
        r"1 are dead but not yet removable", stderr
    ), "the deleted column is non-removable"

    # Ensure the DELETE is replayed on Node B
    node_A.wait_for_catchup(subname_BA)

    # Check the conflict detected on Node B
    logfile = node_B.log_content()[log_location:]
    assert re.search(
        r'conflict detected on relation "public.tab": conflict=delete_origin_differs.*\n'
        r".*DETAIL:.* Deleting the row that was modified locally in transaction [0-9]+ at .*: local row \(1, 3\), replica identity \(a\)=\(1\).",
        logfile,
    ), "delete target row was modified in tab"

    log_location = node_A.log_position()

    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} ENABLE;")
    node_B.wait_for_catchup(subname_AB)

    logfile = node_A.log_content()[log_location:]
    assert re.search(
        r'conflict detected on relation "public.tab": conflict=update_deleted.*\n'
        r".*DETAIL:.* Could not find the row to be updated: remote row \(1, 3\), replica identity \(a\)=\(1\).\n"
        r".*The row to be updated was deleted locally in transaction [0-9]+ at .*",
        logfile,
    ), "update target row was deleted in tab"

    # Remember the next transaction ID to be assigned
    next_xid = node_A.safe_sql("SELECT txid_current() + 1;")

    # Confirm that the xmin value is advanced to the latest nextXid. If no
    # transactions are running, the apply worker selects nextXid as the
    # candidate for the non-removable xid. See GetOldestActiveTransactionId().
    assert node_A.poll_query_until(
        f"SELECT xmin = {next_xid} from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is updated on Node A"

    ###########################################################################
    # Ensure that the deleted tuple needed to detect an update_deleted conflict
    # is accessible via a sequential table scan.
    ###########################################################################

    # Drop the primary key from tab on node A and set REPLICA IDENTITY to FULL
    # to enforce sequential scanning of the table.
    node_A.safe_sql("ALTER TABLE tab REPLICA IDENTITY FULL")
    node_B.safe_sql("ALTER TABLE tab REPLICA IDENTITY FULL")
    node_A.safe_sql("ALTER TABLE tab DROP CONSTRAINT tab_pkey;")

    # Disable the logical replication from node B to node A
    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} DISABLE")

    # Wait for the apply worker to stop
    wait_apply_worker_stopped(node_A)

    node_B.safe_sql("UPDATE tab SET b = 4 WHERE a = 2;")
    node_A.safe_sql("DELETE FROM tab WHERE a = 2;")

    log_location = node_A.log_position()

    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} ENABLE;")
    node_B.wait_for_catchup(subname_AB)

    logfile = node_A.log_content()[log_location:]
    assert re.search(
        r'conflict detected on relation "public.tab": conflict=update_deleted.*\n'
        r".*DETAIL:.* Could not find the row to be updated: remote row \(2, 4\), replica identity full \(2, 2\).*\n"
        r".*The row to be updated was deleted locally in transaction [0-9]+ at .*",
        logfile,
    ), "update target row was deleted in tab"

    ###########################################################################
    # Check that the xmin value of the conflict detection slot can be advanced
    # when the subscription has no tables.
    ###########################################################################

    # Remove the table from the publication
    node_B.safe_sql("ALTER PUBLICATION tap_pub_B DROP TABLE tab")

    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} REFRESH PUBLICATION")

    # Remember the next transaction ID to be assigned
    next_xid = node_A.safe_sql("SELECT txid_current() + 1;")

    # Confirm that the xmin value is advanced to the latest nextXid. If no
    # transactions are running, the apply worker selects nextXid as the
    # candidate for the non-removable xid. See GetOldestActiveTransactionId().
    assert node_A.poll_query_until(
        f"SELECT xmin = {next_xid} from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is updated on Node A"

    # Re-add the table to the publication for further tests
    node_B.safe_sql("ALTER PUBLICATION tap_pub_B ADD TABLE tab")

    node_A.safe_sql(
        f"ALTER SUBSCRIPTION {subname_AB} REFRESH PUBLICATION WITH (copy_data = false)"
    )

    ###########################################################################
    # Test that publisher's transactions marked with DELAY_CHKPT_IN_COMMIT
    # prevent concurrently deleted tuples on the subscriber from being removed.
    # This test also acts as a safeguard to prevent developers from moving the
    # commit timestamp acquisition before marking DELAY_CHKPT_IN_COMMIT in
    # RecordTransactionCommitPrepared.
    ###########################################################################

    injection_points_supported = (
        node_B.safe_sql(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        != "0"
    )

    # This test depends on an injection point to block the prepared transaction
    # commit after marking DELAY_CHKPT_IN_COMMIT flag.
    if injection_points_supported:
        node_B.append_conf(
            """shared_preload_libraries = 'injection_points'
            max_prepared_transactions = 1"""
        )
        node_B.restart()

        # Disable the subscription on Node B for testing only one-way
        # replication.
        node_B.safe_sql(f"ALTER SUBSCRIPTION {subname_BA} DISABLE;")

        # Wait for the apply worker to stop
        wait_apply_worker_stopped(node_B)

        # Truncate the table to cleanup existing dead rows in the table. Then
        # insert a new row.
        node_B.safe_sql(
            """
            TRUNCATE tab;
            INSERT INTO tab VALUES(1, 1);
        """
        )

        node_B.wait_for_catchup(subname_AB)

        # Create the injection_points extension on the publisher node and
        # attach to the commit-after-delay-checkpoint injection point.
        node_B.safe_sql(
            "CREATE EXTENSION injection_points;"
            "SELECT injection_points_attach('commit-after-delay-checkpoint', 'wait');"
        )

        # Start a background session on the publisher node to perform an update
        # and pause at the injection point.
        pub_session = node_B.connect()
        pub_session.do(
            "BEGIN",
            "UPDATE tab SET b = 2 WHERE a = 1",
            "PREPARE TRANSACTION 'txn_with_later_commit_ts'",
        )
        # COMMIT PREPARED will block on the injection point
        pub_session.do_async("COMMIT PREPARED 'txn_with_later_commit_ts'")

        # Wait until the backend enters the injection point
        node_B.wait_for_event("client backend", "commit-after-delay-checkpoint")

        # Confirm the update is suspended
        result = node_B.safe_sql("SELECT * FROM tab WHERE a = 1")
        assert result == "1|1", "publisher sees the old row"

        # Delete the row on the subscriber. The deleted row should be retained
        # due to a transaction on the publisher, which is currently marked with
        # the DELAY_CHKPT_IN_COMMIT flag.
        node_A.safe_sql("DELETE FROM tab WHERE a = 1;")

        # Get the commit timestamp for the delete
        sub_ts = node_A.safe_sql(
            "SELECT timestamp FROM pg_last_committed_xact();"
        )

        log_location = node_A.log_position()

        # Confirm that the apply worker keeps requesting publisher status, while
        # awaiting the prepared transaction to commit. Thus, the request log
        # should appear more than once.
        node_A.wait_for_log(
            r"sending publisher status request message", log_location
        )

        log_location = node_A.log_position()

        node_A.wait_for_log(
            r"sending publisher status request message", log_location
        )

        # Confirm that the dead tuple cannot be removed
        stderr = psql_stderr(node_A, "VACUUM (verbose) public.tab;")
        assert re.search(
            r"1 are dead but not yet removable", stderr
        ), "the deleted column is non-removable"

        log_location = node_A.log_position()

        # Wakeup and detach the injection point on the publisher node. The
        # prepared transaction should now commit.
        node_B.safe_sql(
            "SELECT injection_points_wakeup('commit-after-delay-checkpoint');"
            "SELECT injection_points_detach('commit-after-delay-checkpoint');"
        )

        # Wait for the async query to complete and close the background session
        pub_session.wait_for_completion()
        pub_session.close()

        # Confirm that the transaction committed
        result = node_B.safe_sql("SELECT * FROM tab WHERE a = 1")
        assert result == "1|2", "publisher sees the new row"

        # Ensure the UPDATE is replayed on subscriber
        node_B.wait_for_catchup(subname_AB)

        logfile = node_A.log_content()[log_location:]
        assert re.search(
            r'conflict detected on relation "public.tab": conflict=update_deleted.*\n'
            r".*DETAIL:.* Could not find the row to be updated: remote row \(1, 2\), replica identity full \(1, 1\).*\n"
            r".*The row to be updated was deleted locally in transaction [0-9]+ at .*",
            logfile,
        ), "update target row was deleted in tab"

        # Remember the next transaction ID to be assigned
        next_xid = node_A.safe_sql("SELECT txid_current() + 1;")

        # Confirm that the xmin value is advanced to the latest nextXid after
        # the prepared transaction on the publisher has been committed.
        assert node_A.poll_query_until(
            f"SELECT xmin = {next_xid} from pg_replication_slots "
            "WHERE slot_name = 'pg_conflict_detection'"
        ), "the xmin value of slot 'pg_conflict_detection' is updated on subscriber"

        # Get the commit timestamp for the publisher's update
        pub_ts = node_B.safe_sql(
            "SELECT pg_xact_commit_timestamp(xmin) from tab where a=1;"
        )

        # Check that the commit timestamp for the update on the publisher is
        # later than or equal to the timestamp of the local deletion, as the
        # commit timestamp should be assigned after marking the
        # DELAY_CHKPT_IN_COMMIT flag.
        result = node_B.safe_sql(
            f"SELECT '{pub_ts}'::timestamp >= '{sub_ts}'::timestamp"
        )
        assert result == "t", (
            "pub UPDATE's timestamp is later than that of sub's DELETE"
        )

        # Re-enable the subscription for further tests
        node_B.safe_sql(f"ALTER SUBSCRIPTION {subname_BA} ENABLE;")

    ###########################################################################
    # Check that dead tuple retention stops due to the wait time surpassing
    # max_retention_duration.
    ###########################################################################

    # Create a physical slot
    node_B.safe_sql(
        "SELECT * FROM pg_create_physical_replication_slot('blocker');"
    )

    # Add the inactive physical slot to synchronized_standby_slots
    node_B.append_conf("synchronized_standby_slots = 'blocker'")
    node_B.reload()

    # Enable failover to activate the synchronized_standby_slots setting
    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} DISABLE;")
    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} SET (failover = true);")
    node_A.safe_sql(f"ALTER SUBSCRIPTION {subname_AB} ENABLE;")

    # Insert a record
    node_B.safe_sql("INSERT INTO tab VALUES (5, 5);")

    # Advance the xid on Node A to trigger the next cycle of
    # oldest_nonremovable_xid advancement.
    node_A.safe_sql("SELECT txid_current() + 1;")

    log_offset = node_A.log_position()

    # Set max_retention_duration to a minimal value to initiate retention stop.
    node_A.safe_sql(
        f"ALTER SUBSCRIPTION {subname_AB} SET (max_retention_duration = 1);"
    )

    # Confirm that the retention is stopped
    node_A.wait_for_log(
        r'logical replication worker for subscription "tap_sub_a_b" has stopped retaining the information for detecting conflicts',
        log_offset,
    )

    assert node_A.poll_query_until(
        "SELECT xmin IS NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is invalid on Node A"

    result = node_A.safe_sql(
        f"SELECT subretentionactive FROM pg_subscription WHERE subname='{subname_AB}';"
    )
    assert result == "f", "retention is inactive"

    ###########################################################################
    # Check that dead tuple retention resumes when the max_retention_duration
    # is set 0.
    ###########################################################################

    log_offset = node_A.log_position()

    # Set max_retention_duration to 0
    node_A.safe_sql(
        f"ALTER SUBSCRIPTION {subname_AB} SET (max_retention_duration = 0);"
    )

    # Drop the physical slot and reset the synchronized_standby_slots setting.
    # We change this after setting max_retention_duration to 0, ensuring
    # consistent results in the test as the resumption becomes possible
    # immediately after resetting synchronized_standby_slots, due to the
    # smaller max_retention_duration value of 1ms.
    node_B.safe_sql("SELECT * FROM pg_drop_replication_slot('blocker');")
    # adjust_conf: reset synchronized_standby_slots to ''. Appending a later
    # assignment overrides the earlier one (last value in postgresql.conf
    # wins).
    node_B.append_conf("synchronized_standby_slots = ''")
    node_B.reload()

    # Confirm that the retention resumes
    node_A.wait_for_log(
        r'logical replication worker for subscription "tap_sub_a_b" will resume retaining the information for detecting conflicts\n'
        r".*DETAIL:.* Retention is re-enabled because max_retention_duration has been set to unlimited.*",
        log_offset,
    )

    assert node_A.poll_query_until(
        "SELECT xmin IS NOT NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is valid on Node A"

    result = node_A.safe_sql(
        f"SELECT subretentionactive FROM pg_subscription WHERE subname='{subname_AB}';"
    )
    assert result == "t", "retention is active"

    ###########################################################################
    # Check that the replication slot pg_conflict_detection is dropped after
    # removing all the subscriptions.
    ###########################################################################

    node_B.safe_sql(f"DROP SUBSCRIPTION {subname_BA}")

    assert node_B.poll_query_until(
        "SELECT count(*) = 0 FROM pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the slot 'pg_conflict_detection' has been dropped on Node B"

    node_A.safe_sql(f"DROP SUBSCRIPTION {subname_AB}")

    assert node_A.poll_query_until(
        "SELECT count(*) = 0 FROM pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the slot 'pg_conflict_detection' has been dropped on Node A"
