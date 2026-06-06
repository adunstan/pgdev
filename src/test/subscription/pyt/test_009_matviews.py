# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test materialized views behavior."""


def test_009_matviews(create_pg):
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    node_publisher.safe_sql(
        "CREATE TABLE test1 (a int PRIMARY KEY, b text)")
    node_subscriber.safe_sql(
        "CREATE TABLE test1 (a int PRIMARY KEY, b text);")

    node_publisher.safe_sql(
        "CREATE PUBLICATION mypub FOR ALL TABLES;")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION mysub CONNECTION '{publisher_connstr}' "
        "PUBLICATION mypub;")

    node_publisher.safe_sql(
        "INSERT INTO test1 (a, b) VALUES (1, 'one'), (2, 'two');")

    node_publisher.wait_for_catchup("mysub")

    # Materialized views are not supported by logical replication, but
    # logical decoding does produce change information for them, so we
    # need to make sure they are properly ignored. (bug #15044)

    # create a MV with some data
    node_publisher.safe_sql(
        "CREATE MATERIALIZED VIEW testmv1 AS SELECT * FROM test1;")
    node_publisher.wait_for_catchup("mysub")

    # There is no equivalent relation on the subscriber, but MV data is
    # not replicated, so this does not hang.

    # pass: materialized view data not replicated

    node_subscriber.stop()
    node_publisher.stop()
