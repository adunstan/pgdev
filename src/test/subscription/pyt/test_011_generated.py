# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test generated columns."""


def test_011_generated(create_pg):
    # setup
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} "
        "dbname=postgres"
    )

    node_publisher.safe_sql(
        "CREATE TABLE tab1 (a int PRIMARY KEY, "
        "b int GENERATED ALWAYS AS (a * 2) STORED, "
        "c int GENERATED ALWAYS AS (a * 3) VIRTUAL)"
    )

    node_subscriber.safe_sql(
        "CREATE TABLE tab1 (a int PRIMARY KEY, "
        "b int GENERATED ALWAYS AS (a * 22) STORED, "
        "c int GENERATED ALWAYS AS (a * 33) VIRTUAL, d int)"
    )

    # data for initial sync
    node_publisher.safe_sql("INSERT INTO tab1 (a) VALUES (1), (2), (3)")

    node_publisher.safe_sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' "
        "PUBLICATION pub1"
    )

    # Wait for initial sync of all subscriptions
    node_subscriber.wait_for_subscription_sync()

    result = node_subscriber.safe_sql("SELECT a, b, c FROM tab1")
    assert result == "1|22|33\n2|44|66\n3|66|99", \
        "generated columns initial sync"

    # data to replicate
    node_publisher.safe_sql("INSERT INTO tab1 VALUES (4), (5)")

    node_publisher.safe_sql("UPDATE tab1 SET a = 6 WHERE a = 5")

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT * FROM tab1")
    assert result == ("1|22|33|\n2|44|66|\n3|66|99|\n4|88|132|\n6|132|198|"), \
        "generated columns replicated"

    # try it with a subscriber-side trigger
    node_subscriber.safe_sql(
        """
CREATE FUNCTION tab1_trigger_func() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.d := NEW.a + 10;
  RETURN NEW;
END $$;

CREATE TRIGGER test1 BEFORE INSERT OR UPDATE ON tab1
  FOR EACH ROW
  EXECUTE PROCEDURE tab1_trigger_func();

ALTER TABLE tab1 ENABLE REPLICA TRIGGER test1;
"""
    )

    node_publisher.safe_sql("INSERT INTO tab1 VALUES (7), (8)")

    node_publisher.safe_sql("UPDATE tab1 SET a = 9 WHERE a = 7")

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT * FROM tab1 ORDER BY 1")
    assert result == (
        "1|22|33|\n2|44|66|\n3|66|99|\n4|88|132|\n"
        "6|132|198|\n8|176|264|18\n9|198|297|19"
    ), "generated columns replicated with trigger"

    # cleanup
    node_subscriber.safe_sql("DROP SUBSCRIPTION sub1")
    node_publisher.safe_sql("DROP PUBLICATION pub1")

    # =====================================================================
    # Exercise logical replication of a generated column to a subscriber
    # side regular column. This is done both when the publication parameter
    # 'publish_generated_columns' is set to 'none' (to confirm existing
    # default behavior), and is set to 'stored' (to confirm replication
    # occurs).
    #
    # The test environment is set up as follows:
    #
    # - Publication pub1 on the 'postgres' database.
    #   pub1 has publish_generated_columns as 'none'.
    #
    # - Publication pub2 on the 'postgres' database.
    #   pub2 has publish_generated_columns as 'stored'.
    #
    # - Subscription sub1 on the 'postgres' database for publication pub1.
    #
    # - Subscription sub2 on the 'test_pgc_true' database for publication
    #   pub2.
    # =====================================================================

    node_subscriber.safe_sql("CREATE DATABASE test_pgc_true")

    # ----------------------------------------------------------------
    # Test Case: Generated to regular column replication
    # Publisher table has generated column 'b'.
    # Subscriber table has regular column 'b'.
    # ----------------------------------------------------------------

    # Create table and publications. Insert data to verify initial sync.
    node_publisher.safe_sql(
        """
        CREATE TABLE tab_gen_to_nogen (a int, b int GENERATED ALWAYS AS (a * 2) STORED);
        INSERT INTO tab_gen_to_nogen (a) VALUES (1), (2), (3);
        CREATE PUBLICATION regress_pub1_gen_to_nogen FOR TABLE tab_gen_to_nogen WITH (publish_generated_columns = none);
        CREATE PUBLICATION regress_pub2_gen_to_nogen FOR TABLE tab_gen_to_nogen WITH (publish_generated_columns = stored);
        """
    )

    # Create the table and subscription in the 'postgres' database.
    # NOTE: CREATE SUBSCRIPTION (which implicitly creates a slot) cannot run
    # inside a transaction block, so it is issued separately from the CREATE
    # TABLE.  The in-process libpq session wraps a multi-statement query
    # in a single transaction, so we must split them.
    node_subscriber.safe_sql("CREATE TABLE tab_gen_to_nogen (a int, b int);")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub1_gen_to_nogen CONNECTION '{publisher_connstr}' PUBLICATION regress_pub1_gen_to_nogen WITH (copy_data = true);"
    )

    # Create the table and subscription in the 'test_pgc_true' database.
    node_subscriber.safe_sql(
        "CREATE TABLE tab_gen_to_nogen (a int, b int);",
        dbname="test_pgc_true",
    )
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub2_gen_to_nogen CONNECTION '{publisher_connstr}' PUBLICATION regress_pub2_gen_to_nogen WITH (copy_data = true);",
        dbname="test_pgc_true",
    )

    # Wait for the initial synchronization of both subscriptions.
    node_subscriber.wait_for_subscription_sync(
        node_publisher, "regress_sub1_gen_to_nogen", "postgres")
    node_subscriber.wait_for_subscription_sync(
        node_publisher, "regress_sub2_gen_to_nogen", "test_pgc_true")

    # Verify that generated column data is not copied during the initial
    # synchronization when publish_generated_columns is set to 'none'.
    result = node_subscriber.safe_sql(
        "SELECT a, b FROM tab_gen_to_nogen ORDER BY a")
    assert result == "1|\n2|\n3|", \
        "tab_gen_to_nogen initial sync, when publish_generated_columns=none"

    # Verify that generated column data is copied during the initial
    # synchronization when publish_generated_columns is set to 'stored'.
    result = node_subscriber.safe_sql(
        "SELECT a, b FROM tab_gen_to_nogen ORDER BY a",
        dbname="test_pgc_true")
    assert result == "1|2\n2|4\n3|6", \
        "tab_gen_to_nogen initial sync, when publish_generated_columns=stored"

    # Insert data to verify incremental replication.
    node_publisher.safe_sql("INSERT INTO tab_gen_to_nogen VALUES (4), (5)")

    # Verify that the generated column data is not replicated during
    # incremental replication when publish_generated_columns is set to 'none'.
    node_publisher.wait_for_catchup("regress_sub1_gen_to_nogen")
    result = node_subscriber.safe_sql(
        "SELECT a, b FROM tab_gen_to_nogen ORDER BY a")
    assert result == "1|\n2|\n3|\n4|\n5|", (
        "tab_gen_to_nogen incremental replication, "
        "when publish_generated_columns=none"
    )

    # Verify that generated column data is replicated during incremental
    # synchronization when publish_generated_columns is set to 'stored'.
    node_publisher.wait_for_catchup("regress_sub2_gen_to_nogen")
    result = node_subscriber.safe_sql(
        "SELECT a, b FROM tab_gen_to_nogen ORDER BY a",
        dbname="test_pgc_true")
    assert result == "1|2\n2|4\n3|6\n4|8\n5|10", (
        "tab_gen_to_nogen incremental replication, "
        "when publish_generated_columns=stored"
    )

    # cleanup
    node_subscriber.safe_sql("DROP SUBSCRIPTION regress_sub1_gen_to_nogen")
    node_subscriber.safe_sql(
        "DROP SUBSCRIPTION regress_sub2_gen_to_nogen",
        dbname="test_pgc_true")
    node_publisher.safe_sql(
        """
        DROP PUBLICATION regress_pub1_gen_to_nogen;
        DROP PUBLICATION regress_pub2_gen_to_nogen;
        """
    )
    node_subscriber.safe_sql(
        "DROP table tab_gen_to_nogen", dbname="test_pgc_true")
    # The in-process framework caches a libpq session per database, so close
    # the cached test_pgc_true session before dropping the database; otherwise
    # DROP DATABASE fails with "database is being accessed by other users".
    _sess = node_subscriber._sessions.pop("test_pgc_true", None)
    if _sess is not None:
        _sess.close()
    node_subscriber.safe_sql("DROP DATABASE test_pgc_true")

    # =====================================================================
    # The following test cases demonstrate how publication column lists
    # interact with the publication parameter 'publish_generated_columns'.
    #
    # Test: Column lists take precedence, so generated columns in a column
    # list will be replicated even when publish_generated_columns is 'none'.
    #
    # Test: When there is a column list, only those generated columns named
    # in the column list will be replicated even when
    # publish_generated_columns is 'stored'.
    # =====================================================================

    # ----------------------------------------------------------------
    # Test Case: Publisher replicates the column list, including generated
    # columns, even when the publish_generated_columns option is set to
    # 'none'.
    # ----------------------------------------------------------------

    # Create table and publication. Insert data to verify initial sync.
    node_publisher.safe_sql(
        """
        CREATE TABLE tab2 (a int, gen1 int GENERATED ALWAYS AS (a * 2) STORED);
        INSERT INTO tab2 (a) VALUES (1), (2);
        CREATE PUBLICATION pub1 FOR table tab2(gen1) WITH (publish_generated_columns=none);
        """
    )

    # Create table and subscription.
    node_subscriber.safe_sql("CREATE TABLE tab2 (a int, gen1 int);")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' PUBLICATION pub1 WITH (copy_data = true);"
    )

    # Wait for initial sync.
    node_subscriber.wait_for_subscription_sync(node_publisher, "sub1")

    # Initial sync test when publish_generated_columns is 'none'.
    # Verify 'gen1' is replicated regardless of the 'none' parameter value.
    result = node_subscriber.safe_sql("SELECT * FROM tab2 ORDER BY gen1")
    assert result == "|2\n|4", \
        "tab2 initial sync, when publish_generated_columns=none"

    # Insert data to verify incremental replication.
    node_publisher.safe_sql("INSERT INTO tab2 VALUES (3), (4)")

    # Incremental replication test when publish_generated_columns is 'none'.
    # Verify 'gen1' is replicated regardless of the 'none' parameter value.
    node_publisher.wait_for_catchup("sub1")
    result = node_subscriber.safe_sql("SELECT * FROM tab2 ORDER BY gen1")
    assert result == "|2\n|4\n|6\n|8", \
        "tab2 incremental replication, when publish_generated_columns=none"

    # cleanup
    node_subscriber.safe_sql("DROP SUBSCRIPTION sub1")
    node_publisher.safe_sql("DROP PUBLICATION pub1")

    # ----------------------------------------------------------------
    # Test Case: Even when publish_generated_columns is set to 'stored', the
    # publisher only publishes the data of columns specified in the column
    # list, skipping other generated and non-generated columns.
    # ----------------------------------------------------------------

    # Create table and publication. Insert data to verify initial sync.
    node_publisher.safe_sql(
        """
        CREATE TABLE tab3 (a int, gen1 int GENERATED ALWAYS AS (a * 2) STORED, gen2 int GENERATED ALWAYS AS (a * 2) STORED);
        INSERT INTO tab3 (a) VALUES (1), (2);
        CREATE PUBLICATION pub1 FOR table tab3(gen1) WITH (publish_generated_columns=stored);
        """
    )

    # Create table and subscription.
    node_subscriber.safe_sql("CREATE TABLE tab3 (a int, gen1 int, gen2 int);")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' PUBLICATION pub1 WITH (copy_data = true);"
    )

    # Wait for initial sync.
    node_subscriber.wait_for_subscription_sync(node_publisher, "sub1")

    # Initial sync test when publish_generated_columns is 'stored'.
    # Verify only 'gen1' is replicated regardless of the 'stored' parameter
    # value.
    result = node_subscriber.safe_sql("SELECT * FROM tab3 ORDER BY gen1")
    assert result == "|2|\n|4|", \
        "tab3 initial sync, when publish_generated_columns=stored"

    # Insert data to verify incremental replication.
    node_publisher.safe_sql("INSERT INTO tab3 VALUES (3), (4)")

    # Incremental replication test when publish_generated_columns is 'stored'.
    # Verify only 'gen1' is replicated regardless of the 'stored' parameter
    # value.
    node_publisher.wait_for_catchup("sub1")
    result = node_subscriber.safe_sql("SELECT * FROM tab3 ORDER BY gen1")
    assert result == "|2|\n|4|\n|6|\n|8|", \
        "tab3 incremental replication, when publish_generated_columns=stored"

    # cleanup
    node_subscriber.safe_sql("DROP SUBSCRIPTION sub1")
    node_publisher.safe_sql("DROP PUBLICATION pub1")

    # =====================================================================
    # The following test verifies the expected error when replicating to a
    # generated subscriber column. Test the following combinations:
    # - regular -> generated
    # - generated -> generated
    # =====================================================================

    # ----------------------------------------------------------------
    # A "regular -> generated" or "generated -> generated" replication
    # fails, reporting an error that the generated column on the subscriber
    # side cannot be replicated.
    #
    # Test Case: regular -> generated and generated -> generated
    # Publisher table has regular column 'c2' and generated column 'c3'.
    # Subscriber table has generated columns 'c2' and 'c3'.
    # ----------------------------------------------------------------

    # Create table and publication. Insert data into the table.
    node_publisher.safe_sql(
        """
        CREATE TABLE t1(c1 int, c2 int, c3 int GENERATED ALWAYS AS (c1 * 2) STORED);
        CREATE PUBLICATION pub1 for table t1(c1, c2, c3);
        INSERT INTO t1 VALUES (1);
        """
    )

    # Create table and subscription.
    node_subscriber.safe_sql(
        "CREATE TABLE t1(c1 int, c2 int GENERATED ALWAYS AS (c1 + 2) STORED, c3 int GENERATED ALWAYS AS (c1 + 2) STORED);"
    )
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' PUBLICATION pub1;"
    )

    # Verify that an error occurs.
    offset = node_subscriber.log_position()
    node_subscriber.wait_for_log(
        r'ERROR: ( [A-Z0-9]+:)? logical replication target relation '
        r'"public.t1" has incompatible generated columns: "c2", "c3"',
        offset)

    # cleanup
    node_subscriber.safe_sql("DROP SUBSCRIPTION sub1")
    node_publisher.safe_sql("DROP PUBLICATION pub1")

    node_subscriber.stop()
    node_publisher.stop()
