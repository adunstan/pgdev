# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test logical replication of two-phase commit (2PC) with streaming."""


def check_parallel_log(node_subscriber, offset, is_parallel, type_):
    """Check that the parallel apply worker has finished applying the streaming
    transaction.
    """
    if is_parallel:
        node_subscriber.wait_for_log(
            r"DEBUG: ( [A-Z0-9]+:)? finished processing the STREAM "
            + type_ + " command",
            offset)


def do_streaming(node_publisher, node_subscriber, appname, is_parallel):
    """Common test steps for both the streaming=on and streaming=parallel
    cases.
    """
    ###############################
    # Test 2PC PREPARE / COMMIT PREPARED.
    # 1. Data is streamed as a 2PC transaction.
    # 2. Then do commit prepared.
    #
    # Expect all data is replicated on subscriber side after the commit.
    ###############################

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    # check that 2PC gets replicated to subscriber
    # Insert, update and delete some rows.
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(3, 5) s(i);\n"
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0;\n"
        "DELETE FROM test_tab WHERE mod(a,3) = 0;\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_publisher.wait_for_catchup(appname)

    check_parallel_log(node_subscriber, offset, is_parallel, "PREPARE")

    # check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # 2PC transaction gets committed
    node_publisher.safe_sql("COMMIT PREPARED 'test_prepared_tab';")

    node_publisher.wait_for_catchup(appname)

    # check that transaction is committed on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab")
    assert result == "4|4|4", (
        "Rows inserted by 2PC have committed on subscriber, and extra "
        "columns contain local defaults")
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "transaction is committed on subscriber"

    ###############################
    # Test 2PC PREPARE / ROLLBACK PREPARED.
    # 1. Table is deleted back to 2 rows which are replicated on subscriber.
    # 2. Data is streamed using 2PC.
    # 3. Do rollback prepared.
    #
    # Expect data rolls back leaving only the original 2 rows.
    ###############################

    # First, delete the data except for 2 rows (will be replicated)
    node_publisher.safe_sql("DELETE FROM test_tab WHERE a > 2;")

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    # Then insert, update and delete some rows.
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(3, 5) s(i);\n"
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0;\n"
        "DELETE FROM test_tab WHERE mod(a,3) = 0;\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_publisher.wait_for_catchup(appname)

    check_parallel_log(node_subscriber, offset, is_parallel, "PREPARE")

    # check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # 2PC transaction gets aborted
    node_publisher.safe_sql("ROLLBACK PREPARED 'test_prepared_tab';")

    node_publisher.wait_for_catchup(appname)

    # check that transaction is aborted on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab")
    assert result == "2|2|2", (
        "Rows inserted by 2PC are rolled back, leaving only the original "
        "2 rows")

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "transaction is aborted on subscriber"

    ###############################
    # Check that 2PC COMMIT PREPARED is decoded properly on crash restart.
    # 1. insert, update and delete some rows.
    # 2. Then server crashes before the 2PC transaction is committed.
    # 3. After servers are restarted the pending transaction is committed.
    #
    # Expect all data is replicated on subscriber side after the commit.
    # Note: both publisher and subscriber do crash/restart.
    ###############################

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(3, 5) s(i);\n"
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0;\n"
        "DELETE FROM test_tab WHERE mod(a,3) = 0;\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_subscriber.stop("immediate")
    node_publisher.stop("immediate")

    node_publisher.start()
    node_subscriber.start()

    # We don't try to check the log for parallel option here as the subscriber
    # may have stopped after finishing the prepare and before logging the
    # appropriate message.

    # commit post the restart
    node_publisher.safe_sql("COMMIT PREPARED 'test_prepared_tab';")
    node_publisher.wait_for_catchup(appname)

    # check inserts are visible
    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab")
    assert result == "4|4|4", (
        "Rows inserted by 2PC have committed on subscriber, and extra "
        "columns contain local defaults")

    ###############################
    # Do INSERT after the PREPARE but before ROLLBACK PREPARED.
    # 1. Table is deleted back to 2 rows which are replicated on subscriber.
    # 2. Data is streamed using 2PC.
    # 3. A single row INSERT is done which is after the PREPARE.
    # 4. Then do a ROLLBACK PREPARED.
    #
    # Expect the 2PC data rolls back leaving only 3 rows on the subscriber
    # (the original 2 + inserted 1).
    ###############################

    # First, delete the data except for 2 rows (will be replicated)
    node_publisher.safe_sql("DELETE FROM test_tab WHERE a > 2;")

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    # Then insert, update and delete some rows.
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(3, 5) s(i);\n"
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0;\n"
        "DELETE FROM test_tab WHERE mod(a,3) = 0;\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_publisher.wait_for_catchup(appname)

    check_parallel_log(node_subscriber, offset, is_parallel, "PREPARE")

    # check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # Insert a different record (now we are outside of the 2PC transaction)
    # Note: the 2PC transaction still holds row locks so make sure this insert
    # is for a separate primary key
    node_publisher.safe_sql("INSERT INTO test_tab VALUES (99999, 'foobar')")

    # 2PC transaction gets aborted
    node_publisher.safe_sql("ROLLBACK PREPARED 'test_prepared_tab';")

    node_publisher.wait_for_catchup(appname)

    # check that transaction is aborted on subscriber,
    # but the extra INSERT outside of the 2PC still was replicated
    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab")
    assert result == "3|3|3", "check the outside insert was copied to subscriber"

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "transaction is aborted on subscriber"

    ###############################
    # Do INSERT after the PREPARE but before COMMIT PREPARED.
    # 1. Table is deleted back to 2 rows which are replicated on subscriber.
    # 2. Data is streamed using 2PC.
    # 3. A single row INSERT is done which is after the PREPARE.
    # 4. Then do a COMMIT PREPARED.
    #
    # Expect 2PC data + the extra row are on the subscriber
    # (the 3334 + inserted 1 = 3335).
    ###############################

    # First, delete the data except for 2 rows (will be replicated)
    node_publisher.safe_sql("DELETE FROM test_tab WHERE a > 2;")

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    # Then insert, update and delete some rows.
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(3, 5) s(i);\n"
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0;\n"
        "DELETE FROM test_tab WHERE mod(a,3) = 0;\n"
        "PREPARE TRANSACTION 'test_prepared_tab';")

    node_publisher.wait_for_catchup(appname)

    check_parallel_log(node_subscriber, offset, is_parallel, "PREPARE")

    # check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # Insert a different record (now we are outside of the 2PC transaction)
    # Note: the 2PC transaction still holds row locks so make sure this insert
    # is for a separate primary key
    node_publisher.safe_sql("INSERT INTO test_tab VALUES (99999, 'foobar')")

    # 2PC transaction gets committed
    node_publisher.safe_sql("COMMIT PREPARED 'test_prepared_tab';")

    node_publisher.wait_for_catchup(appname)

    # check that transaction is committed on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab")
    assert result == "5|5|5", (
        "Rows inserted by 2PC (as well as outside insert) have committed on "
        "subscriber, and extra columns contain local defaults")

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "0", "transaction is committed on subscriber"

    # Cleanup the test data
    node_publisher.safe_sql("DELETE FROM test_tab WHERE a > 2;")
    node_publisher.wait_for_catchup(appname)


def test_023_twophase_stream(create_pg):
    ###############################
    # Setup
    ###############################

    # Initialize publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_publisher.append_conf(
        "max_prepared_transactions = 10\n"
        "debug_logical_replication_streaming = immediate")
    node_publisher.restart()

    # Create subscriber node
    node_subscriber = create_pg("subscriber")
    node_subscriber.append_conf("max_prepared_transactions = 10")
    node_subscriber.restart()

    # Create some pre-existing content on publisher
    node_publisher.safe_sql(
        "CREATE TABLE test_tab (a int primary key, b bytea)")
    node_publisher.safe_sql(
        "INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")
    node_publisher.safe_sql("CREATE TABLE test_tab_2 (a int)")

    # Setup structure on subscriber (columns a and b are compatible with same
    # table name on publisher)
    node_subscriber.safe_sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, "
        "c timestamptz DEFAULT now(), d bigint DEFAULT 999)")
    node_subscriber.safe_sql("CREATE TABLE test_tab_2 (a int)")

    # Setup logical replication (streaming = on)
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres")
    node_publisher.safe_sql(
        "CREATE PUBLICATION tap_pub FOR TABLE test_tab, test_tab_2")

    appname = "tap_sub"

    ################################
    # Test using streaming mode 'on'
    ################################
    node_subscriber.safe_sql(
        "CREATE SUBSCRIPTION tap_sub "
        f"CONNECTION '{publisher_connstr} application_name={appname}' "
        "PUBLICATION tap_pub "
        "WITH (streaming = on, two_phase = on)")

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, appname)

    # Also wait for two-phase to be enabled
    twophase_query = (
        "SELECT count(1) = 0 FROM pg_subscription "
        "WHERE subtwophasestate NOT IN ('e');")
    assert node_subscriber.poll_query_until(twophase_query), \
        "Timed out while waiting for subscriber to enable twophase"

    # Check initial data was copied to subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab")
    assert result == "2|2|2", "check initial data was copied to subscriber"

    do_streaming(node_publisher, node_subscriber, appname, 0)

    ######################################
    # Test using streaming mode 'parallel'
    ######################################
    oldpid = node_publisher.safe_sql(
        "SELECT pid FROM pg_stat_replication "
        f"WHERE application_name = '{appname}' AND state = 'streaming';")

    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION tap_sub SET(streaming = parallel)")

    assert node_publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        f"WHERE application_name = '{appname}' AND state = 'streaming';"), \
        "Timed out while waiting for apply to restart after changing SUBSCRIPTION"

    # We need to check DEBUG logs to ensure that the parallel apply worker has
    # applied the transaction. So, bump up the log verbosity.
    node_subscriber.append_conf("log_min_messages = debug1")
    node_subscriber.reload()

    # Run a query to make sure that the reload has taken effect.
    node_subscriber.safe_sql("SELECT 1")

    do_streaming(node_publisher, node_subscriber, appname, 1)

    # Test serializing changes to files and notify the parallel apply worker to
    # apply them at the end of the transaction.
    reload_offset = node_subscriber.log_position()
    node_subscriber.append_conf(
        "debug_logical_replication_streaming = immediate")
    # Reset the log_min_messages to default.
    node_subscriber.append_conf("log_min_messages = warning")
    node_subscriber.reload()

    # Run a query to make sure that the reload has taken effect.
    node_subscriber.safe_sql("SELECT 1")

    # Spawning a psql subprocess used to provide enough latency to give the
    # apply leader time to re-read debug_logical_replication_streaming before
    # the publisher transaction below is streamed to it; the in-process libpq
    # session here returns too quickly, so wait until the reload has been
    # processed (the parallel apply leader only serializes to a file once it
    # observes the GUC set to "immediate").
    node_subscriber.wait_for_log(
        r'parameter "debug_logical_replication_streaming" changed to "immediate"',
        reload_offset)

    offset = node_subscriber.log_position()

    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO test_tab_2 values(1);\n"
        "PREPARE TRANSACTION 'xact';")

    # Ensure that the changes are serialized.
    node_subscriber.wait_for_log(
        r"LOG: ( [A-Z0-9]+:)? logical replication apply worker will serialize "
        r"the remaining changes of remote transaction \d+ to a file",
        offset)

    node_publisher.wait_for_catchup(appname)

    # Check that transaction is in prepared state on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_prepared_xacts;")
    assert result == "1", "transaction is prepared on subscriber"

    # Check that 2PC gets committed on subscriber
    node_publisher.safe_sql("COMMIT PREPARED 'xact';")

    node_publisher.wait_for_catchup(appname)

    # Check that transaction is committed on subscriber
    result = node_subscriber.safe_sql("SELECT count(*) FROM test_tab_2")
    assert result == "1", "transaction is committed on subscriber"

    # Test the ability to re-apply a transaction when a parallel apply worker
    # fails to prepare the transaction due to insufficient
    # max_prepared_transactions setting.
    node_subscriber.append_conf(
        "max_prepared_transactions = 0\n"
        "debug_logical_replication_streaming = buffered")
    node_subscriber.restart()

    # COMMIT PREPARED cannot run inside a transaction block; under libpq's
    # simple query protocol a multi-statement string is one implicit
    # transaction, so issue the COMMIT PREPARED separately.
    node_publisher.safe_sql(
        "BEGIN;\n"
        "INSERT INTO test_tab_2 values(2);\n"
        "PREPARE TRANSACTION 'xact';")
    node_publisher.safe_sql("COMMIT PREPARED 'xact';")

    offset = node_subscriber.log_position()

    # Confirm the ERROR is reported because max_prepared_transactions is zero
    node_subscriber.wait_for_log(
        r"ERROR: ( [A-Z0-9]+:)? prepared transactions are disabled", offset)

    # Confirm that the parallel apply worker has encountered an error. The check
    # focuses on the worker type as a keyword, since the error message content
    # may differ based on whether the leader initially detected the parallel
    # apply worker's failure or received a signal from it.
    node_subscriber.wait_for_log(
        r"ERROR: .*logical replication parallel apply worker.*", offset)

    # Set max_prepared_transactions to correct value to resume the replication
    node_subscriber.append_conf("max_prepared_transactions = 10")
    node_subscriber.restart()

    node_publisher.wait_for_catchup(appname)

    # Check that transaction is committed on subscriber
    result = node_subscriber.safe_sql("SELECT count(*) FROM test_tab_2")
    assert result == "2", "transaction is committed on subscriber after retrying"

    ###############################
    # check all the cleanup
    ###############################

    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub")

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
