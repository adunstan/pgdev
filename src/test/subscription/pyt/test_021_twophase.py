# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test logical replication of two-phase commit (2PC)."""


def test_021_twophase(create_pg):
    ###############################
    # Setup
    ###############################

    # Initialize publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_publisher.append_conf("max_prepared_transactions = 10")
    node_publisher.restart()

    # Create subscriber node
    node_subscriber = create_pg("subscriber")
    node_subscriber.append_conf("max_prepared_transactions = 0")
    node_subscriber.restart()

    # Create some pre-existing content on publisher
    node_publisher.safe_sql("CREATE TABLE tab_full (a int PRIMARY KEY)")
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full SELECT generate_series(1,10);\n"
        "PREPARE TRANSACTION 'some_initial_data';")
    node_publisher.safe_sql("COMMIT PREPARED 'some_initial_data';")

    # Setup structure on subscriber
    node_subscriber.safe_sql("CREATE TABLE tab_full (a int PRIMARY KEY)")

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres")
    node_publisher.safe_sql(
        "CREATE PUBLICATION tap_pub FOR TABLE tab_full")

    appname = "tap_sub"
    node_subscriber.safe_sql(
        "CREATE SUBSCRIPTION tap_sub "
        f"CONNECTION '{publisher_connstr} application_name={appname}' "
        "PUBLICATION tap_pub "
        "WITH (two_phase = on)")

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, appname)

    # Also wait for two-phase to be enabled
    twophase_query = (
        "SELECT count(1) = 0 FROM pg_subscription "
        "WHERE subtwophasestate NOT IN ('e');")
    assert node_subscriber.poll_query_until(twophase_query), \
        "Timed out while waiting for subscriber to enable twophase"

    ###############################
    # check that 2PC gets replicated to subscriber
    # then COMMIT PREPARED
    ###############################

    # Save the log location, to see the failure of the application
    log_location = node_subscriber.log_position()

    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full VALUES (11);\n"
        "PREPARE TRANSACTION 'test_prepared_tab_full';")

    # Confirm the ERROR is reported because max_prepared_transactions is zero
    node_subscriber.wait_for_log(
        r"ERROR: ( [A-Z0-9]+:)? prepared transactions are disabled",
        log_location)

    # Set max_prepared_transactions to correct value to resume the replication
    node_subscriber.append_conf("max_prepared_transactions = 10")
    node_subscriber.restart()

    node_publisher.wait_for_catchup(appname)

    # check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # check that 2PC gets committed on subscriber
    node_publisher.safe_sql("COMMIT PREPARED 'test_prepared_tab_full';")

    node_publisher.wait_for_catchup(appname)

    # check that transaction is committed on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM tab_full where a = 11;")
    assert result == "1", "Row inserted via 2PC has committed on subscriber"

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "transaction is committed on subscriber"

    ###############################
    # check that 2PC gets replicated to subscriber
    # then ROLLBACK PREPARED
    ###############################

    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full VALUES (12);\n"
        "PREPARE TRANSACTION 'test_prepared_tab_full';")

    node_publisher.wait_for_catchup(appname)

    # check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # check that 2PC gets aborted on subscriber
    node_publisher.safe_sql("ROLLBACK PREPARED 'test_prepared_tab_full';")

    node_publisher.wait_for_catchup(appname)

    # check that transaction is aborted on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM tab_full where a = 12;")
    assert result == "0", "Row inserted via 2PC is not present on subscriber"

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "transaction is aborted on subscriber"

    ###############################
    # Check that ROLLBACK PREPARED is decoded properly on crash restart
    # (publisher and subscriber crash)
    ###############################

    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full VALUES (12);\n"
        "INSERT INTO tab_full VALUES (13);\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_subscriber.stop("immediate")
    node_publisher.stop("immediate")

    node_publisher.start()
    node_subscriber.start()

    # rollback post the restart
    node_publisher.safe_sql("ROLLBACK PREPARED 'test_prepared_tab';")
    node_publisher.wait_for_catchup(appname)

    # check inserts are rolled back
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM tab_full where a IN (12,13);")
    assert result == "0", "Rows rolled back are not on the subscriber"

    ###############################
    # Check that COMMIT PREPARED is decoded properly on crash restart
    # (publisher and subscriber crash)
    ###############################

    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full VALUES (12);\n"
        "INSERT INTO tab_full VALUES (13);\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_subscriber.stop("immediate")
    node_publisher.stop("immediate")

    node_publisher.start()
    node_subscriber.start()

    # commit post the restart
    node_publisher.safe_sql("COMMIT PREPARED 'test_prepared_tab';")
    node_publisher.wait_for_catchup(appname)

    # check inserts are visible
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM tab_full where a IN (12,13);")
    assert result == "2", "Rows inserted via 2PC are visible on the subscriber"

    ###############################
    # Check that COMMIT PREPARED is decoded properly on crash restart
    # (subscriber only crash)
    ###############################

    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full VALUES (14);\n"
        "INSERT INTO tab_full VALUES (15);\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_subscriber.stop("immediate")
    node_subscriber.start()

    # commit post the restart
    node_publisher.safe_sql("COMMIT PREPARED 'test_prepared_tab';")
    node_publisher.wait_for_catchup(appname)

    # check inserts are visible
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM tab_full where a IN (14,15);")
    assert result == "2", "Rows inserted via 2PC are visible on the subscriber"

    ###############################
    # Check that COMMIT PREPARED is decoded properly on crash restart
    # (publisher only crash)
    ###############################

    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full VALUES (16);\n"
        "INSERT INTO tab_full VALUES (17);\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_publisher.stop("immediate")
    node_publisher.start()

    # commit post the restart
    node_publisher.safe_sql("COMMIT PREPARED 'test_prepared_tab';")
    node_publisher.wait_for_catchup(appname)

    # check inserts are visible
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM tab_full where a IN (16,17);")
    assert result == "2", "Rows inserted via 2PC are visible on the subscriber"

    ###############################
    # Test nested transaction with 2PC
    ###############################

    # check that 2PC gets replicated to subscriber
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full VALUES (21);\n"
        "SAVEPOINT sp_inner;\n"
        "INSERT INTO tab_full VALUES (22);\n"
        "ROLLBACK TO SAVEPOINT sp_inner;\n"
        "PREPARE TRANSACTION 'outer';\n")
    node_publisher.wait_for_catchup(appname)

    # check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # COMMIT
    node_publisher.safe_sql("COMMIT PREPARED 'outer';")

    node_publisher.wait_for_catchup(appname)

    # check the transaction state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "transaction is ended on subscriber"

    # check inserts are visible. 22 should be rolled back. 21 should be committed.
    result = node_subscriber.safe_sql(
        "SELECT a FROM tab_full where a IN (21,22);")
    assert result == "21", "Rows committed are on the subscriber"

    ###############################
    # Test using empty GID
    ###############################

    # check that 2PC gets replicated to subscriber
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_full VALUES (51);\n"
        "PREPARE TRANSACTION '';")
    node_publisher.wait_for_catchup(appname)

    # check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # ROLLBACK
    node_publisher.safe_sql("ROLLBACK PREPARED '';")

    # check that 2PC gets aborted on subscriber
    node_publisher.wait_for_catchup(appname)

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "transaction is aborted on subscriber"

    ###############################
    # copy_data=false and two_phase
    ###############################

    # create some test tables for copy tests
    node_publisher.safe_sql("CREATE TABLE tab_copy (a int PRIMARY KEY)")
    node_publisher.safe_sql(
        "INSERT INTO tab_copy SELECT generate_series(1,5);")
    node_subscriber.safe_sql("CREATE TABLE tab_copy (a int PRIMARY KEY)")
    node_subscriber.safe_sql("INSERT INTO tab_copy VALUES (88);")
    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_copy;")
    assert result == "1", "initial data in subscriber table"

    # Setup logical replication
    node_publisher.safe_sql(
        "CREATE PUBLICATION tap_pub_copy FOR TABLE tab_copy;")

    appname_copy = "appname_copy"
    node_subscriber.safe_sql(
        "CREATE SUBSCRIPTION tap_sub_copy "
        f"CONNECTION '{publisher_connstr} application_name={appname_copy}' "
        "PUBLICATION tap_pub_copy "
        "WITH (two_phase=on, copy_data=false);")

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, appname_copy)

    # Also wait for two-phase to be enabled
    assert node_subscriber.poll_query_until(twophase_query), \
        "Timed out while waiting for subscriber to enable twophase"

    # Check that the initial table data was NOT replicated (because we said
    # copy_data=false)
    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_copy;")
    assert result == "1", "initial data in subscriber table"

    # Now do a prepare on publisher and check that it IS replicated
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_copy VALUES (99);\n"
        "PREPARE TRANSACTION 'mygid';")

    # Wait for both subscribers to catchup
    node_publisher.wait_for_catchup(appname_copy)
    node_publisher.wait_for_catchup(appname)

    # Check that the transaction has been prepared on the subscriber, there
    # will be 2 prepared transactions for the 2 subscriptions.
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "2", "transaction is prepared on subscriber"

    # Now commit the insert and verify that it IS replicated
    node_publisher.safe_sql("COMMIT PREPARED 'mygid';")

    result = node_publisher.safe_sql("SELECT count(*) FROM tab_copy;")
    assert result == "6", "publisher inserted data"

    # Wait for both subscribers to catchup
    node_publisher.wait_for_catchup(appname_copy)
    node_publisher.wait_for_catchup(appname)

    # Make sure there are no prepared transactions on the subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "should be no prepared transactions on subscriber"

    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_copy;")
    assert result == "2", "replicated data in subscriber table"

    # Clean up
    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub")

    ###############################
    # Alter the subscription to set two_phase to false.
    # Verify that the altered subscription reflects the new two_phase option.
    ###############################

    # Confirm that the two-phase slot option is enabled before altering
    result = node_publisher.safe_sql(
        "SELECT two_phase FROM pg_replication_slots "
        "WHERE slot_name = 'tap_sub_copy';")
    assert result == "t", "two-phase is enabled"

    # Alter subscription two_phase to false
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub_copy DISABLE;")
    node_subscriber.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity "
        "WHERE backend_type = 'logical replication apply worker'")
    # ALTER SUBSCRIPTION ... SET (two_phase) cannot run inside a transaction
    # block; under libpq's simple query protocol a multi-statement string is
    # one implicit transaction, so issue each statement separately.
    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION tap_sub_copy SET (two_phase = false);")
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub_copy ENABLE;")

    # Wait for subscription startup
    node_subscriber.wait_for_subscription_sync(node_publisher, appname_copy)

    # Make sure that the two-phase is disabled on the subscriber
    result = node_subscriber.safe_sql(
        "SELECT subtwophasestate FROM pg_subscription "
        "WHERE subname = 'tap_sub_copy';")
    assert result == "d", "two-phase subscription option should be disabled"

    # Make sure that the two-phase slot option is also disabled
    result = node_publisher.safe_sql(
        "SELECT two_phase FROM pg_replication_slots "
        "WHERE slot_name = 'tap_sub_copy';")
    assert result == "f", "two-phase slot option should be disabled"

    ###############################
    # Now do a prepare on the publisher and verify that it is not replicated.
    ###############################
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO tab_copy VALUES (100);\n"
        "PREPARE TRANSACTION 'newgid';")

    # Wait for the subscriber to catchup
    node_publisher.wait_for_catchup(appname_copy)

    # Make sure there are no prepared transactions on the subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "should be no prepared transactions on subscriber"

    ###############################
    # Set two_phase to "true" and failover to "true" before the COMMIT PREPARED.
    #
    # This tests the scenario where both two_phase and failover are altered
    # simultaneously.
    ###############################
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub_copy DISABLE;")
    node_subscriber.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity "
        "WHERE backend_type = 'logical replication apply worker'")
    # ALTER SUBSCRIPTION ... SET (two_phase) cannot run inside a transaction
    # block; issue each statement separately (see note above).
    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION tap_sub_copy SET (two_phase = true, failover = true);")
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub_copy ENABLE;")

    ###############################
    # Now commit the insert and verify that it is replicated.
    ###############################
    node_publisher.safe_sql("COMMIT PREPARED 'newgid';")

    # Wait for the subscriber to catchup
    node_publisher.wait_for_catchup(appname_copy)

    # Make sure that the committed transaction is replicated.
    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_copy;")
    assert result == "3", "replicated data in subscriber table"

    # Make sure that the two-phase is enabled on the subscriber
    result = node_subscriber.safe_sql(
        "SELECT subtwophasestate FROM pg_subscription "
        "WHERE subname = 'tap_sub_copy';")
    assert result == "e", "two-phase should be enabled"

    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub_copy;")
    node_publisher.safe_sql("DROP PUBLICATION tap_pub_copy;")

    ###############################
    # check all the cleanup
    ###############################

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_subscription")
    assert result == "0", "check subscription was dropped on subscriber"

    result = node_publisher.safe_sql(
        "SELECT count(*) FROM pg_replication_slots")
    assert result == "0", "check replication slot was dropped on publisher"

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_subscription_rel")
    assert result == "0", \
        "check subscription relation status was dropped on subscriber"

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_replication_origin")
    assert result == "0", "check replication origin was dropped on subscriber"

    node_subscriber.stop("fast")
    node_publisher.stop("fast")
