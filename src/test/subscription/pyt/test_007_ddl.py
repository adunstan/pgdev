# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test some logical replication DDL behavior."""

import re


def test_007_ddl(create_pg):
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

    node_publisher.wait_for_catchup("mysub")

    # Disable and drop a subscription in the same transaction.  safe_sql
    # runs the explicit BEGIN/COMMIT block below as one transaction.
    node_subscriber.safe_sql(
        "BEGIN;\n"
        "ALTER SUBSCRIPTION mysub DISABLE;\n"
        "ALTER SUBSCRIPTION mysub SET (slot_name = NONE);\n"
        "DROP SUBSCRIPTION mysub;\n"
        "COMMIT;\n"
    )

    # pass: subscription disable and drop in same transaction did not hang

    # One of the specified publications exists.
    sess = node_subscriber.connect()
    try:
        sess.clear_notices()
        res = sess.query(
            f"CREATE SUBSCRIPTION mysub1 CONNECTION '{publisher_connstr}' "
            "PUBLICATION mypub, non_existent_pub"
        )
        assert res.error_message is None, res.error_message
        stderr = sess.get_notices_str()
    finally:
        sess.close()
    assert re.search(
        'WARNING:  publication "non_existent_pub" does not exist on the publisher',
        stderr,
    ), "Create subscription throws warning for non-existent publication"

    # Wait for initial table sync to finish.
    node_subscriber.wait_for_subscription_sync(node_publisher, "mysub1")

    # Specifying non-existent publication along with add publication.
    sess = node_subscriber.connect()
    try:
        sess.clear_notices()
        res = sess.query(
            "ALTER SUBSCRIPTION mysub1 ADD PUBLICATION non_existent_pub1, "
            "non_existent_pub2"
        )
        assert res.error_message is None, res.error_message
        stderr = sess.get_notices_str()
    finally:
        sess.close()
    assert re.search(
        r'WARNING:  publications "non_existent_pub1", "non_existent_pub2" do '
        r"not exist on the publisher",
        stderr,
    ), (
        "Alter subscription add publication throws warning for non-existent "
        "publications"
    )

    # Specifying non-existent publication along with set publication.
    sess = node_subscriber.connect()
    try:
        sess.clear_notices()
        res = sess.query("ALTER SUBSCRIPTION mysub1 SET PUBLICATION non_existent_pub")
        assert res.error_message is None, res.error_message
        stderr = sess.get_notices_str()
    finally:
        sess.close()
    assert re.search(
        'WARNING:  publication "non_existent_pub" does not exist on the publisher',
        stderr,
    ), (
        "Alter subscription set publication throws warning for non-existent "
        "publication"
    )

    # Cleanup
    node_publisher.safe_sql(
        "DROP PUBLICATION mypub;\nSELECT pg_drop_replication_slot('mysub');\n"
    )
    node_subscriber.safe_sql("DROP SUBSCRIPTION mysub1")

    #
    # Test ALTER PUBLICATION RENAME command during the replication
    #

    def test_swap(table_name, pubname, appname):
        # Confirms tuples can be replicated
        node_publisher.safe_sql(f"INSERT INTO {table_name} VALUES (1);")
        node_publisher.wait_for_catchup(appname)
        result = node_subscriber.safe_sql(f"SELECT a FROM {table_name}")
        assert (
            result == "1"
        ), "check replication worked well before renaming a publication"

        # Swap the name of publications; pubname <-> pub_empty
        node_publisher.safe_sql(
            f"ALTER PUBLICATION {pubname} RENAME TO tap_pub_tmp;\n"
            f"ALTER PUBLICATION pub_empty RENAME TO {pubname};\n"
            "ALTER PUBLICATION tap_pub_tmp RENAME TO pub_empty;\n"
        )

        # Insert the data again
        node_publisher.safe_sql(f"INSERT INTO {table_name} VALUES (2);")
        node_publisher.wait_for_catchup(appname)

        # Confirms the second tuple won't be replicated because pubname does
        # not contain relations anymore.
        result = node_subscriber.safe_sql(f"SELECT a FROM {table_name} ORDER BY a")
        assert (
            result == "1"
        ), "check the tuple inserted after the RENAME was not replicated"

        # Restore the name of publications because it can be called several
        # times
        node_publisher.safe_sql(
            f"ALTER PUBLICATION {pubname} RENAME TO tap_pub_tmp;\n"
            f"ALTER PUBLICATION pub_empty RENAME TO {pubname};\n"
            "ALTER PUBLICATION tap_pub_tmp RENAME TO pub_empty;\n"
        )

    # Create another table
    ddl = "CREATE TABLE test2 (a int, b text);"
    node_publisher.safe_sql(ddl)
    node_subscriber.safe_sql(ddl)

    # Create publications and a subscription
    node_publisher.safe_sql(
        "CREATE PUBLICATION pub_empty;\n"
        "CREATE PUBLICATION pub_for_tab FOR TABLE test1;\n"
        "CREATE PUBLICATION pub_for_all_tables FOR ALL TABLES;\n"
    )
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION pub_for_tab"
    )
    node_subscriber.wait_for_subscription_sync(node_publisher, "tap_sub")

    # Confirms RENAME command works well for a publication
    test_swap("test1", "pub_for_tab", "tap_sub")

    # Switches a publication which includes all tables
    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION tap_sub SET PUBLICATION pub_for_all_tables;"
    )
    node_subscriber.wait_for_subscription_sync(node_publisher, "tap_sub")

    # Confirms RENAME command works well for ALL TABLES publication
    test_swap("test2", "pub_for_all_tables", "tap_sub")

    # Cleanup
    node_publisher.safe_sql(
        "DROP PUBLICATION pub_empty, pub_for_tab, pub_for_all_tables;\n"
        "DROP TABLE test1, test2;\n"
    )
    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub;")
    node_subscriber.safe_sql("DROP TABLE test1, test2;")

    node_subscriber.stop()
    node_publisher.stop()
