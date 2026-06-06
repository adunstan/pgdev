# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test streaming of a transaction with subtransactions, DDLs, DMLs, and
rollbacks.

This file is mainly to test the DDL/DML interaction of the publisher side,
so we didn't add a parallel apply version for the tests in this file.
"""


def test_019_stream_subxact_ddl_abort(create_pg):
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
        "CREATE TABLE test_tab (a int primary key, b bytea, c INT, d INT, "
        "e INT)")

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres")
    node_publisher.safe_sql(
        "CREATE PUBLICATION tap_pub FOR TABLE test_tab")

    appname = "tap_sub"
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION "
        f"'{publisher_connstr} application_name={appname}' "
        "PUBLICATION tap_pub WITH (streaming = on)")

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, appname)

    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c) FROM test_tab")
    assert result == "2|0", "check initial data was copied to subscriber"

    # streamed transaction with DDL, DML and ROLLBACKs
    h = node_publisher.connect()
    h.do("""
    BEGIN;
    INSERT INTO test_tab VALUES (3, sha256(3::text::bytea));
    ALTER TABLE test_tab ADD COLUMN c INT;
    SAVEPOINT s1;
    INSERT INTO test_tab VALUES (4, sha256(4::text::bytea), -4);
    ALTER TABLE test_tab ADD COLUMN d INT;
    SAVEPOINT s2;
    INSERT INTO test_tab VALUES (5, sha256(5::text::bytea), -5, 5*2);
    ALTER TABLE test_tab ADD COLUMN e INT;
    SAVEPOINT s3;
    INSERT INTO test_tab VALUES (6, sha256(6::text::bytea), -6, 6*2, -6*3);
    ALTER TABLE test_tab DROP COLUMN c;
    ROLLBACK TO s1;
    INSERT INTO test_tab VALUES (4, sha256(4::text::bytea), 4);
    COMMIT;
    """)
    h.close()

    node_publisher.wait_for_catchup(appname)

    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c) FROM test_tab")
    assert result == "4|1", (
        "check rollback to savepoint was reflected on subscriber and extra "
        "columns contain local defaults")

    node_subscriber.stop()
    node_publisher.stop()
