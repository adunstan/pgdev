# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test streaming of a large transaction with DDL and subtransactions.

This file is mainly to test the DDL/DML interaction of the publisher side,
so we didn't add a parallel apply version for the tests in this file.
"""


def test_017_stream_ddl(create_pg):
    # Create publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical", start=False)
    node_publisher.append_conf("logical_decoding_work_mem = 64kB")
    node_publisher.start()

    # Create subscriber node
    node_subscriber = create_pg("subscriber")

    # Create some preexisting content on publisher
    node_publisher.safe_sql("CREATE TABLE test_tab (a int primary key, b varchar)")
    node_publisher.safe_sql("INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")

    # Setup structure on subscriber
    node_subscriber.safe_sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, c INT, d INT, "
        "e INT, f INT)"
    )

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql("CREATE PUBLICATION tap_pub FOR TABLE test_tab")

    appname = "tap_sub"
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION "
        f"'{publisher_connstr} application_name={appname}' "
        "PUBLICATION tap_pub WITH (streaming = on)"
    )

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, appname)

    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab"
    )
    assert result == "2|0|0", "check initial data was copied to subscriber"

    # a small (non-streamed) transaction with DDL and DML
    h = node_publisher.connect()
    h.do(
        "BEGIN",
        "INSERT INTO test_tab VALUES (3, sha256(3::text::bytea))",
        "ALTER TABLE test_tab ADD COLUMN c INT",
        "SAVEPOINT s1",
        "INSERT INTO test_tab VALUES (4, sha256(4::text::bytea), -4)",
        "COMMIT",
    )
    h.close()

    # large (streamed) transaction with DDL and DML
    h = node_publisher.connect()
    h.do(
        "BEGIN",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea), -i "
        "FROM generate_series(5, 1000) s(i)",
        "ALTER TABLE test_tab ADD COLUMN d INT",
        "SAVEPOINT s1",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea), -i, 2*i "
        "FROM generate_series(1001, 2000) s(i)",
        "COMMIT",
    )
    h.close()

    # a small (non-streamed) transaction with DDL and DML
    h = node_publisher.connect()
    h.do(
        "BEGIN",
        "INSERT INTO test_tab VALUES (2001, sha256(2001::text::bytea), "
        "-2001, 2*2001)",
        "ALTER TABLE test_tab ADD COLUMN e INT",
        "SAVEPOINT s1",
        "INSERT INTO test_tab VALUES (2002, sha256(2002::text::bytea), "
        "-2002, 2*2002, -3*2002)",
        "COMMIT",
    )
    h.close()

    node_publisher.wait_for_catchup(appname)

    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d), count(e) FROM test_tab"
    )
    assert result == "2002|1999|1002|1", (
        "check data was copied to subscriber in streaming mode and extra "
        "columns contain local defaults"
    )

    # A large (streamed) transaction with DDL and DML. One of the DDL is
    # performed after DML to ensure that we invalidate the schema sent for
    # test_tab so that the next transaction has to send the schema again.
    h = node_publisher.connect()
    h.do(
        "BEGIN",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea), -i, 2*i, -3*i "
        "FROM generate_series(2003,5000) s(i)",
        "ALTER TABLE test_tab ADD COLUMN f INT",
        "COMMIT",
    )
    h.close()

    # A small transaction that won't get streamed. This is just to ensure that
    # we send the schema again to reflect the last column added in the previous
    # test.
    h = node_publisher.connect()
    h.do(
        "BEGIN",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea), -i, 2*i, -3*i, "
        "4*i FROM generate_series(5001,5005) s(i)",
        "COMMIT",
    )
    h.close()

    node_publisher.wait_for_catchup(appname)

    result = node_subscriber.safe_sql(
        "SELECT count(*), count(c), count(d), count(e), count(f) FROM test_tab"
    )
    assert result == "5005|5002|4005|3004|5", (
        "check data was copied to subscriber for both streaming and "
        "non-streaming transactions"
    )

    node_subscriber.stop()
    node_publisher.stop()
