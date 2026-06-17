# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test streaming of a transaction containing multiple subtransactions and
rollbacks.
"""


def check_parallel_log(node_subscriber, offset, is_parallel, type_):
    """Check that the parallel apply worker has finished applying the streaming
    transaction.
    """
    if is_parallel:
        node_subscriber.wait_for_log(
            r"DEBUG: ( [A-Z0-9]+:)? finished processing the STREAM "
            + type_
            + " command",
            offset,
        )


def do_streaming(node_publisher, node_subscriber, appname, is_parallel):
    """Common test steps for both the streaming=on and streaming=parallel
    cases.
    """
    offset = 0

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    # streamed transaction with DDL, DML and ROLLBACKs
    h = node_publisher.connect()
    h.do(
        """
    BEGIN;
    INSERT INTO test_tab VALUES (3, sha256(3::text::bytea));
    SAVEPOINT s1;
    INSERT INTO test_tab VALUES (4, sha256(4::text::bytea));
    SAVEPOINT s2;
    INSERT INTO test_tab VALUES (5, sha256(5::text::bytea));
    SAVEPOINT s3;
    INSERT INTO test_tab VALUES (6, sha256(6::text::bytea));
    ROLLBACK TO s2;
    INSERT INTO test_tab VALUES (7, sha256(7::text::bytea));
    ROLLBACK TO s1;
    INSERT INTO test_tab VALUES (8, sha256(8::text::bytea));
    SAVEPOINT s4;
    INSERT INTO test_tab VALUES (9, sha256(9::text::bytea));
    SAVEPOINT s5;
    INSERT INTO test_tab VALUES (10, sha256(10::text::bytea));
    COMMIT;
    """
    )
    h.close()

    node_publisher.wait_for_catchup(appname)

    check_parallel_log(node_subscriber, offset, is_parallel, "COMMIT")

    result = node_subscriber.safe_sql("SELECT count(*), count(c) FROM test_tab")
    assert result == "6|0", (
        "check rollback to savepoint was reflected on subscriber and extra "
        "columns contain local defaults"
    )

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    # streamed transaction with subscriber receiving out of order
    # subtransaction ROLLBACKs
    h = node_publisher.connect()
    h.do(
        """
    BEGIN;
    INSERT INTO test_tab VALUES (11, sha256(11::text::bytea));
    SAVEPOINT s1;
    INSERT INTO test_tab VALUES (12, sha256(12::text::bytea));
    SAVEPOINT s2;
    INSERT INTO test_tab VALUES (13, sha256(13::text::bytea));
    SAVEPOINT s3;
    INSERT INTO test_tab VALUES (14, sha256(14::text::bytea));
    RELEASE s2;
    INSERT INTO test_tab VALUES (15, sha256(15::text::bytea));
    ROLLBACK TO s1;
    COMMIT;
    """
    )
    h.close()

    node_publisher.wait_for_catchup(appname)

    check_parallel_log(node_subscriber, offset, is_parallel, "COMMIT")

    result = node_subscriber.safe_sql("SELECT count(*), count(c) FROM test_tab")
    assert result == "7|0", "check rollback to savepoint was reflected on subscriber"

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    # streamed transaction with subscriber receiving rollback
    h = node_publisher.connect()
    h.do(
        """
    BEGIN;
    INSERT INTO test_tab VALUES (16, sha256(16::text::bytea));
    SAVEPOINT s1;
    INSERT INTO test_tab VALUES (17, sha256(17::text::bytea));
    SAVEPOINT s2;
    INSERT INTO test_tab VALUES (18, sha256(18::text::bytea));
    ROLLBACK;
    """
    )
    h.close()

    node_publisher.wait_for_catchup(appname)

    check_parallel_log(node_subscriber, offset, is_parallel, "ABORT")

    result = node_subscriber.safe_sql("SELECT count(*), count(c) FROM test_tab")
    assert result == "7|0", "check rollback was reflected on subscriber"

    # Cleanup the test data
    node_publisher.safe_sql("DELETE FROM test_tab WHERE (a > 2)")
    node_publisher.wait_for_catchup(appname)


def test_018_stream_subxact_abort(create_pg):
    # Create publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical", start=False)
    node_publisher.append_conf("debug_logical_replication_streaming = immediate")
    node_publisher.start()

    # Create subscriber node
    node_subscriber = create_pg("subscriber")

    # Create some preexisting content on publisher
    node_publisher.safe_sql("CREATE TABLE test_tab (a int primary key, b bytea)")
    node_publisher.safe_sql("INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")
    node_publisher.safe_sql("CREATE TABLE test_tab_2 (a int)")

    # Setup structure on subscriber
    node_subscriber.safe_sql(
        "CREATE TABLE test_tab (a int primary key, b text, c INT, d INT, e INT)"
    )
    node_subscriber.safe_sql("CREATE TABLE test_tab_2 (a int)")

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql("CREATE PUBLICATION tap_pub FOR TABLE test_tab, test_tab_2")

    appname = "tap_sub"

    ################################
    # Test using streaming mode 'on'
    ################################
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION "
        f"'{publisher_connstr} application_name={appname}' "
        "PUBLICATION tap_pub WITH (streaming = on)"
    )

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, appname)

    result = node_subscriber.safe_sql("SELECT count(*), count(c) FROM test_tab")
    assert result == "2|0", "check initial data was copied to subscriber"

    do_streaming(node_publisher, node_subscriber, appname, 0)

    ######################################
    # Test using streaming mode 'parallel'
    ######################################
    oldpid = node_publisher.safe_sql(
        "SELECT pid FROM pg_stat_replication "
        f"WHERE application_name = '{appname}' AND state = 'streaming';"
    )

    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub SET(streaming = parallel)")

    assert node_publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        f"WHERE application_name = '{appname}' AND state = 'streaming';"
    ), "Timed out while waiting for apply to restart after changing SUBSCRIPTION"

    # We need to check DEBUG logs to ensure that the parallel apply worker has
    # applied the transaction. So, bump up the log verbosity.
    node_subscriber.append_conf("log_min_messages = debug1")
    node_subscriber.reload()

    # Run a query to make sure that the reload has taken effect.
    node_subscriber.safe_sql("SELECT 1")

    do_streaming(node_publisher, node_subscriber, appname, 1)

    # Test serializing changes to files and notify the parallel apply worker to
    # apply them at the end of the transaction.
    node_subscriber.append_conf("debug_logical_replication_streaming = immediate")
    # Reset the log_min_messages to default.
    node_subscriber.append_conf("log_min_messages = warning")
    node_subscriber.reload()

    # Run a query to make sure that the reload has taken effect.
    node_subscriber.safe_sql("SELECT 1")

    offset = node_subscriber.log_position()

    h = node_publisher.connect()
    h.do(
        """
    BEGIN;
    INSERT INTO test_tab_2 values(1);
    ROLLBACK;
    """
    )
    h.close()

    # Ensure that the changes are serialized.
    node_subscriber.wait_for_log(
        r"LOG: ( [A-Z0-9]+:)? logical replication apply worker will serialize "
        r"the remaining changes of remote transaction \d+ to a file",
        offset,
    )

    node_publisher.wait_for_catchup(appname)

    # Check that transaction is aborted on subscriber
    result = node_subscriber.safe_sql("SELECT count(*) FROM test_tab_2")
    assert result == "0", "check rollback was reflected on subscriber"

    # Serialize the ABORT sub-transaction.
    offset = node_subscriber.log_position()

    h = node_publisher.connect()
    h.do(
        """
    BEGIN;
    INSERT INTO test_tab_2 values(1);
    SAVEPOINT sp;
    INSERT INTO test_tab_2 values(1);
    ROLLBACK TO sp;
    COMMIT;
    """
    )
    h.close()

    # Ensure that the changes are serialized.
    node_subscriber.wait_for_log(
        r"LOG: ( [A-Z0-9]+:)? logical replication apply worker will serialize "
        r"the remaining changes of remote transaction \d+ to a file",
        offset,
    )

    node_publisher.wait_for_catchup(appname)

    # Check that only sub-transaction is aborted on subscriber.
    result = node_subscriber.safe_sql("SELECT count(*) FROM test_tab_2")
    assert result == "1", "check rollback to savepoint was reflected on subscriber"

    node_subscriber.stop()
    node_publisher.stop()
