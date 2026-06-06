# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test TRUNCATE."""


def test_010_truncate(create_pg):
    # setup

    node_publisher = create_pg("publisher", allows_streaming="logical")

    node_subscriber = create_pg("subscriber", start=False)
    node_subscriber.append_conf("max_logical_replication_workers = 6")
    node_subscriber.start()

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} " "dbname=postgres"
    )

    node_publisher.safe_sql("CREATE TABLE tab1 (a int PRIMARY KEY)")
    node_subscriber.safe_sql("CREATE TABLE tab1 (a int PRIMARY KEY)")

    node_publisher.safe_sql("CREATE TABLE tab2 (a int PRIMARY KEY)")
    node_subscriber.safe_sql("CREATE TABLE tab2 (a int PRIMARY KEY)")

    node_publisher.safe_sql("CREATE TABLE tab3 (a int PRIMARY KEY)")
    node_subscriber.safe_sql("CREATE TABLE tab3 (a int PRIMARY KEY)")

    node_publisher.safe_sql(
        "CREATE TABLE tab4 (x int PRIMARY KEY, y int REFERENCES tab3)"
    )
    node_subscriber.safe_sql(
        "CREATE TABLE tab4 (x int PRIMARY KEY, y int REFERENCES tab3)"
    )

    node_subscriber.safe_sql("CREATE SEQUENCE seq1 OWNED BY tab1.a")
    node_subscriber.safe_sql("ALTER SEQUENCE seq1 START 101")

    node_publisher.safe_sql("CREATE PUBLICATION pub1 FOR TABLE tab1")
    node_publisher.safe_sql(
        "CREATE PUBLICATION pub2 FOR TABLE tab2 WITH (publish = insert)"
    )
    node_publisher.safe_sql("CREATE PUBLICATION pub3 FOR TABLE tab3, tab4")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' " "PUBLICATION pub1"
    )
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub2 CONNECTION '{publisher_connstr}' " "PUBLICATION pub2"
    )
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub3 CONNECTION '{publisher_connstr}' " "PUBLICATION pub3"
    )

    # Wait for initial sync of all subscriptions
    node_subscriber.wait_for_subscription_sync()

    # insert data to truncate

    node_subscriber.safe_sql("INSERT INTO tab1 VALUES (1), (2), (3)")

    node_publisher.wait_for_catchup("sub1")

    # truncate and check

    node_publisher.safe_sql("TRUNCATE tab1")

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab1")
    assert result == "0||", "truncate replicated"

    result = node_subscriber.safe_sql("SELECT nextval('seq1')")
    assert result == "1", "sequence not restarted"

    # truncate with restart identity

    node_publisher.safe_sql("TRUNCATE tab1 RESTART IDENTITY")

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT nextval('seq1')")
    assert result == "101", "truncate restarted identities"

    # test publication that does not replicate truncate

    node_subscriber.safe_sql("INSERT INTO tab2 VALUES (1), (2), (3)")

    node_publisher.safe_sql("TRUNCATE tab2")

    node_publisher.wait_for_catchup("sub2")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab2")
    assert result == "3|1|3", "truncate not replicated"

    node_publisher.safe_sql("ALTER PUBLICATION pub2 SET (publish = 'insert, truncate')")

    node_publisher.safe_sql("TRUNCATE tab2")

    node_publisher.wait_for_catchup("sub2")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab2")
    assert result == "0||", "truncate replicated after publication change"

    # test multiple tables connected by foreign keys

    node_subscriber.safe_sql("INSERT INTO tab3 VALUES (1), (2), (3)")
    node_subscriber.safe_sql("INSERT INTO tab4 VALUES (11, 1), (111, 1), (22, 2)")

    node_publisher.safe_sql("TRUNCATE tab3, tab4")

    node_publisher.wait_for_catchup("sub3")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab3")
    assert result == "0||", "truncate of multiple tables replicated"
    result = node_subscriber.safe_sql("SELECT count(*), min(x), max(x) FROM tab4")
    assert result == "0||", "truncate of multiple tables replicated"

    # test truncate of multiple tables, some of which are not published

    node_subscriber.safe_sql("DROP SUBSCRIPTION sub2")
    node_publisher.safe_sql("DROP PUBLICATION pub2")

    node_subscriber.safe_sql("INSERT INTO tab1 VALUES (1), (2), (3)")
    node_subscriber.safe_sql("INSERT INTO tab2 VALUES (1), (2), (3)")

    node_publisher.safe_sql("TRUNCATE tab1, tab2")

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab1")
    assert result == "0||", "truncate of multiple tables some not published"
    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab2")
    assert result == "3|1|3", "truncate of multiple tables some not published"

    # Test that truncate works for synchronous logical replication

    node_publisher.safe_sql("ALTER SYSTEM SET synchronous_standby_names TO 'sub1'")
    node_publisher.safe_sql("SELECT pg_reload_conf()")

    # insert data to truncate

    node_publisher.safe_sql("INSERT INTO tab1 VALUES (1), (2), (3)")

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab1")
    assert result == "3|1|3", "check synchronous logical replication"

    node_publisher.safe_sql("TRUNCATE tab1")

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab1")
    assert result == "0||", "truncate replicated in synchronous logical replication"

    node_publisher.safe_sql("ALTER SYSTEM RESET synchronous_standby_names")
    node_publisher.safe_sql("SELECT pg_reload_conf()")

    # test that truncate works for logical replication when there are multiple
    # subscriptions for a single table

    node_publisher.safe_sql("CREATE TABLE tab5 (a int)")

    node_subscriber.safe_sql("CREATE TABLE tab5 (a int)")

    node_publisher.safe_sql("CREATE PUBLICATION pub5 FOR TABLE tab5")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub5_1 CONNECTION '{publisher_connstr}' "
        "PUBLICATION pub5"
    )
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub5_2 CONNECTION '{publisher_connstr}' "
        "PUBLICATION pub5"
    )

    # wait for initial data sync
    node_subscriber.wait_for_subscription_sync()

    # insert data to truncate

    node_publisher.safe_sql("INSERT INTO tab5 VALUES (1), (2), (3)")

    node_publisher.wait_for_catchup("sub5_1")
    node_publisher.wait_for_catchup("sub5_2")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab5")
    assert result == "6|1|3", "insert replicated for multiple subscriptions"

    node_publisher.safe_sql("TRUNCATE tab5")

    node_publisher.wait_for_catchup("sub5_1")
    node_publisher.wait_for_catchup("sub5_2")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM tab5")
    assert result == "0||", "truncate replicated for multiple subscriptions"

    # check deadlocks
    result = node_subscriber.safe_sql(
        "SELECT deadlocks FROM pg_stat_database WHERE datname='postgres'"
    )
    assert result == "0", "no deadlocks detected"

    node_subscriber.stop()
    node_publisher.stop()
