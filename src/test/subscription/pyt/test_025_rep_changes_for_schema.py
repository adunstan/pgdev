# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Logical replication tests for schema publications."""


def test_025_rep_changes_for_schema(create_pg):
    # Initialize publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical")

    # Create subscriber node
    node_subscriber = create_pg("subscriber")

    # Test replication with publications created using FOR TABLES IN SCHEMA
    # option.
    # Create schemas and tables on publisher
    node_publisher.safe_sql("CREATE SCHEMA sch1")
    node_publisher.safe_sql(
        "CREATE TABLE sch1.tab1 AS SELECT generate_series(1,10) AS a"
    )
    node_publisher.safe_sql(
        "CREATE TABLE sch1.tab2 AS SELECT generate_series(1,10) AS a"
    )
    node_publisher.safe_sql(
        "CREATE TABLE sch1.tab1_parent (a int PRIMARY KEY, b text) "
        "PARTITION BY LIST (a)"
    )
    node_publisher.safe_sql(
        "CREATE TABLE public.tab1_child1 PARTITION OF sch1.tab1_parent "
        "FOR VALUES IN (1, 2, 3)"
    )
    node_publisher.safe_sql(
        "CREATE TABLE public.tab1_child2 PARTITION OF sch1.tab1_parent "
        "FOR VALUES IN (4, 5, 6)"
    )

    node_publisher.safe_sql("INSERT INTO sch1.tab1_parent values (1),(4)")

    # Create schemas and tables on subscriber
    node_subscriber.safe_sql("CREATE SCHEMA sch1")
    node_subscriber.safe_sql("CREATE TABLE sch1.tab1 (a int)")
    node_subscriber.safe_sql("CREATE TABLE sch1.tab2 (a int)")
    node_subscriber.safe_sql(
        "CREATE TABLE sch1.tab1_parent (a int PRIMARY KEY, b text) "
        "PARTITION BY LIST (a)"
    )
    node_subscriber.safe_sql(
        "CREATE TABLE public.tab1_child1 PARTITION OF sch1.tab1_parent "
        "FOR VALUES IN (1, 2, 3)"
    )
    node_subscriber.safe_sql(
        "CREATE TABLE public.tab1_child2 PARTITION OF sch1.tab1_parent "
        "FOR VALUES IN (4, 5, 6)"
    )

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql(
        "CREATE PUBLICATION tap_pub_schema FOR TABLES IN SCHEMA sch1"
    )

    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub_schema CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub_schema"
    )

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "tap_sub_schema")

    # Check the schema table data is synced up
    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM sch1.tab1")
    assert result == "10|1|10", "check rows on subscriber catchup"

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM sch1.tab2")
    assert result == "10|1|10", "check rows on subscriber catchup"

    result = node_subscriber.safe_sql("SELECT * FROM sch1.tab1_parent order by 1")
    assert result == "1|\n4|", "check rows on subscriber catchup"

    # Insert some data into few tables and verify that inserted data is replicated
    node_publisher.safe_sql("INSERT INTO sch1.tab1 VALUES(generate_series(11,20))")

    node_publisher.safe_sql("INSERT INTO sch1.tab1_parent values (2),(5)")

    node_publisher.wait_for_catchup("tap_sub_schema")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM sch1.tab1")
    assert result == "20|1|20", "check replicated inserts on subscriber"

    result = node_subscriber.safe_sql("SELECT * FROM sch1.tab1_parent order by 1")
    assert result == "1|\n2|\n4|\n5|", "check replicated inserts on subscriber"

    # Create new table in the publication schema, verify that subscriber does not get
    # the new table data before refresh.
    node_publisher.safe_sql(
        "CREATE TABLE sch1.tab3 AS SELECT generate_series(1,10) AS a"
    )

    node_subscriber.safe_sql("CREATE TABLE sch1.tab3(a int)")

    node_publisher.wait_for_catchup("tap_sub_schema")

    result = node_subscriber.safe_sql("SELECT count(*) FROM sch1.tab3")
    assert result == "0", "check replicated inserts on subscriber"

    # Table data should be reflected after refreshing the publication in
    # subscriber.
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub_schema REFRESH PUBLICATION")

    # Wait for sync to finish
    node_subscriber.wait_for_subscription_sync()

    node_publisher.safe_sql("INSERT INTO sch1.tab3 VALUES(11)")

    node_publisher.wait_for_catchup("tap_sub_schema")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM sch1.tab3")
    assert result == "11|1|11", "check rows on subscriber catchup"

    # Set the schema of a publication schema table to a non publication schema and
    # verify that inserted data is not reflected by the subscriber.
    node_publisher.safe_sql("ALTER TABLE sch1.tab3 SET SCHEMA public")
    node_publisher.safe_sql("INSERT INTO public.tab3 VALUES(12)")

    node_publisher.wait_for_catchup("tap_sub_schema")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM sch1.tab3")
    assert result == "11|1|11", "check replicated inserts on subscriber"

    # Verify that the subscription relation list is updated after refresh
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_subscription_rel WHERE srsubid IN "
        "(SELECT oid FROM pg_subscription WHERE subname = 'tap_sub_schema')"
    )
    assert (
        result == "5"
    ), "check subscription relation status is not yet dropped on subscriber"

    # Ask for data sync
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub_schema REFRESH PUBLICATION")

    # Wait for sync to finish
    node_subscriber.wait_for_subscription_sync()

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_subscription_rel WHERE srsubid IN "
        "(SELECT oid FROM pg_subscription WHERE subname = 'tap_sub_schema')"
    )
    assert result == "4", "check subscription relation status was dropped on subscriber"

    # Drop table from the publication schema, verify that subscriber removes the
    # table entry after refresh.
    node_publisher.safe_sql("DROP TABLE sch1.tab2")
    node_publisher.wait_for_catchup("tap_sub_schema")
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_subscription_rel WHERE srsubid IN "
        "(SELECT oid FROM pg_subscription WHERE subname = 'tap_sub_schema')"
    )
    assert (
        result == "4"
    ), "check subscription relation status is not yet dropped on subscriber"

    # Table should be removed from pg_subscription_rel after refreshing the
    # publication in subscriber.
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub_schema REFRESH PUBLICATION")

    # Wait for sync to finish
    node_subscriber.wait_for_subscription_sync()

    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_subscription_rel WHERE srsubid IN "
        "(SELECT oid FROM pg_subscription WHERE subname = 'tap_sub_schema')"
    )
    assert result == "3", "check subscription relation status was dropped on subscriber"

    # Drop schema from publication, verify that the inserts are not published after
    # dropping the schema from publication. Here 2nd insert should not be
    # published.
    node_publisher.safe_sql(
        "INSERT INTO sch1.tab1 VALUES(21);\n"
        "ALTER PUBLICATION tap_pub_schema DROP TABLES IN SCHEMA sch1;\n"
        "INSERT INTO sch1.tab1 values(22);"
    )

    node_publisher.wait_for_catchup("tap_sub_schema")

    result = node_subscriber.safe_sql("SELECT count(*), min(a), max(a) FROM sch1.tab1")
    assert result == "21|1|21", "check replicated inserts on subscriber"

    node_subscriber.stop("fast")
    node_publisher.stop("fast")
