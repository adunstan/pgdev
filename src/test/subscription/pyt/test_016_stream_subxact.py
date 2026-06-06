# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test streaming of a transaction containing subtransactions."""


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
    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    # Insert, update and delete some rows.  This is one deliberately-grouped
    # transaction containing subtransactions (savepoints).
    h = node_publisher.connect()
    h.do(
        "BEGIN",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(3, 5) s(i)",
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
        "DELETE FROM test_tab WHERE mod(a,3) = 0",
        "SAVEPOINT s1",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(6, 8) s(i)",
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
        "DELETE FROM test_tab WHERE mod(a,3) = 0",
        "SAVEPOINT s2",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(9, 11) s(i)",
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
        "DELETE FROM test_tab WHERE mod(a,3) = 0",
        "SAVEPOINT s3",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(12, 14) s(i)",
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
        "DELETE FROM test_tab WHERE mod(a,3) = 0",
        "SAVEPOINT s4",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) "
        "FROM generate_series(15, 17) s(i)",
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
        "DELETE FROM test_tab WHERE mod(a,3) = 0",
        "COMMIT",
    )
    h.close()

    node_publisher.wait_for_catchup(appname)

    check_parallel_log(node_subscriber, offset, is_parallel, "COMMIT")

    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab")
    assert result == "12|12|12", (
        "check data was copied to subscriber in streaming mode and extra "
        "columns contain local defaults")

    # Cleanup the test data
    node_publisher.safe_sql("DELETE FROM test_tab WHERE (a > 2)")
    node_publisher.wait_for_catchup(appname)


def test_016_stream_subxact(create_pg):
    # Create publisher node
    node_publisher = create_pg(
        "publisher", allows_streaming="logical", start=False)
    node_publisher.append_conf(
        "debug_logical_replication_streaming = immediate")
    node_publisher.start()

    # Create subscriber node
    node_subscriber = create_pg("subscriber")

    # Create some preexisting content on publisher
    node_publisher.safe_sql(
        "CREATE TABLE test_tab (a int primary key, b bytea)")
    node_publisher.safe_sql(
        "INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")

    # Setup structure on subscriber
    node_subscriber.safe_sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, "
        "c timestamptz DEFAULT now(), d bigint DEFAULT 999)")

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres")
    node_publisher.safe_sql(
        "CREATE PUBLICATION tap_pub FOR TABLE test_tab")

    appname = "tap_sub"

    ################################
    # Test using streaming mode 'on'
    ################################
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION "
        f"'{publisher_connstr} application_name={appname}' "
        "PUBLICATION tap_pub WITH (streaming = on)")

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, appname)

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

    node_subscriber.stop()
    node_publisher.stop()
