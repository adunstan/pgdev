# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test replication between databases with different encodings."""


def test_005_encoding(create_pg):
    node_publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        initdb_extra=["--locale=C", "--encoding=UTF8"],
    )

    node_subscriber = create_pg(
        "subscriber",
        initdb_extra=["--locale=C", "--encoding=LATIN1"],
    )

    ddl = "CREATE TABLE test1 (a int, b text);"
    node_publisher.safe_sql(ddl)
    node_subscriber.safe_sql(ddl)

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    node_publisher.safe_sql("CREATE PUBLICATION mypub FOR ALL TABLES;")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION mysub CONNECTION '{publisher_connstr}' "
        "PUBLICATION mypub;"
    )

    # Wait for initial sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "mysub")

    node_publisher.safe_sql(
        r"INSERT INTO test1 VALUES (1, E'Mot\xc3\xb6rhead')")  # hand-rolled UTF-8

    node_publisher.wait_for_catchup("mysub")

    assert node_subscriber.poll_query_until(
        r"SELECT a FROM test1 WHERE b = E'Mot\xf6rhead'", expected="1"  # LATIN1
    ), "data replicated to subscriber"

    node_subscriber.stop()
    node_publisher.stop()
