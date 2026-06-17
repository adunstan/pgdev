# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test logical replication behavior with heap rewrites."""


def test_006_rewrite(create_pg):
    node_publisher = create_pg("publisher", allows_streaming="logical")

    node_subscriber = create_pg("subscriber")

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

    node_publisher.safe_sql("INSERT INTO test1 (a, b) VALUES (1, 'one'), (2, 'two');")

    node_publisher.wait_for_catchup("mysub")

    assert node_subscriber.safe_sql("SELECT a, b FROM test1") == (
        "1|one\n" "2|two"
    ), "initial data replicated to subscriber"

    # DDL that causes a heap rewrite
    ddl2 = "ALTER TABLE test1 ADD c int NOT NULL DEFAULT 0;"
    node_subscriber.safe_sql(ddl2)
    node_publisher.safe_sql(ddl2)

    node_publisher.wait_for_catchup("mysub")

    node_publisher.safe_sql("INSERT INTO test1 (a, b, c) VALUES (3, 'three', 33);")

    node_publisher.wait_for_catchup("mysub")

    assert node_subscriber.safe_sql("SELECT a, b, c FROM test1") == (
        "1|one|0\n" "2|two|0\n" "3|three|33"
    ), "data replicated to subscriber"

    node_subscriber.stop()
    node_publisher.stop()
