# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for logical replication table syncing."""


def test_004_sync(create_pg):
    # Initialize publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical")

    # Create subscriber node
    node_subscriber = create_pg("subscriber", start=False)
    node_subscriber.append_conf(
        "postgresql.conf", "wal_retrieve_retry_interval = 1ms")
    node_subscriber.start()

    # Create some preexisting content on publisher
    node_publisher.safe_sql("CREATE TABLE tab_rep (a int primary key)")
    node_publisher.safe_sql("INSERT INTO tab_rep SELECT generate_series(1,10)")

    # Setup structure on subscriber
    node_subscriber.safe_sql("CREATE TABLE tab_rep (a int primary key)")

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql("CREATE PUBLICATION tap_pub FOR ALL TABLES")

    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub")

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "tap_sub")

    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_rep")
    assert result == "10", "initial data synced for first sub"

    # drop subscription so that there is unreplicated data
    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub")

    node_publisher.safe_sql(
        "INSERT INTO tab_rep SELECT generate_series(11,20)")

    # recreate the subscription, it will try to do initial copy
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub")

    # but it will be stuck on data copy as it will fail on constraint
    started_query = "SELECT srsubstate = 'd' FROM pg_subscription_rel;"
    assert node_subscriber.poll_query_until(started_query), \
        "Timed out while waiting for subscriber to start sync"

    # remove the conflicting data
    node_subscriber.safe_sql("DELETE FROM tab_rep;")

    # wait for sync to finish this time
    node_subscriber.wait_for_subscription_sync()

    # check that all data is synced
    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_rep")
    assert result == "20", "initial data synced for second sub"

    # now check another subscription for the same node pair
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub2 CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub WITH (copy_data = false)")

    # wait for it to start
    assert node_subscriber.poll_query_until(
        "SELECT pid IS NOT NULL FROM pg_stat_subscription "
        "WHERE subname = 'tap_sub2' AND worker_type = 'apply'"), \
        "Timed out while waiting for subscriber to start"

    # and drop both subscriptions
    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub")
    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub2")

    # check subscriptions are removed
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_subscription")
    assert result == "0", "second and third sub are dropped"

    # remove the conflicting data
    node_subscriber.safe_sql("DELETE FROM tab_rep;")

    # recreate the subscription again
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub")

    # and wait for data sync to finish again
    node_subscriber.wait_for_subscription_sync()

    # check that all data is synced
    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_rep")
    assert result == "20", "initial data synced for fourth sub"

    # add new table on subscriber
    node_subscriber.safe_sql("CREATE TABLE tab_rep_next (a int)")

    # setup structure with existing data on publisher
    node_publisher.safe_sql(
        "CREATE TABLE tab_rep_next (a) AS SELECT generate_series(1,10)")

    node_publisher.wait_for_catchup("tap_sub")

    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_rep_next")
    assert result == "0", \
        "no data for table added after subscription initialized"

    # ask for data sync
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub REFRESH PUBLICATION")

    # wait for sync to finish
    node_subscriber.wait_for_subscription_sync()

    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_rep_next")
    assert result == "10", \
        "data for table added after subscription initialized are now synced"

    # Add some data
    node_publisher.safe_sql(
        "INSERT INTO tab_rep_next SELECT generate_series(1,10)")

    node_publisher.wait_for_catchup("tap_sub")

    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_rep_next")
    assert result == "20", \
        "changes for table added after subscription initialized replicated"

    # clean up
    node_publisher.safe_sql("DROP TABLE tab_rep_next")
    node_subscriber.safe_sql("DROP TABLE tab_rep_next")
    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub")

    # Table tab_rep already has the same records on both publisher and
    # subscriber at this time. Recreate the subscription which will do the
    # initial copy of the table again and fails due to unique constraint
    # violation.
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub")

    assert node_subscriber.poll_query_until(started_query), \
        "Timed out while waiting for subscriber to start sync"

    # DROP SUBSCRIPTION must clean up slots on the publisher side when the
    # subscriber is stuck on data copy for constraint violation.
    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub")

    # When DROP SUBSCRIPTION tries to drop the tablesync slot, the slot may not
    # have been created, which causes the slot to be created after the DROP
    # SUBSCRIPTION finishes. Such slots eventually get dropped at walsender exit
    # time. So, to prevent being affected by such ephemeral tablesync slots, we
    # wait until all the slots have been cleaned.
    assert node_publisher.poll_query_until(
        "SELECT count(*) = 0 FROM pg_replication_slots"), \
        "DROP SUBSCRIPTION during error can clean up the slots on the publisher"

    # After dropping the subscription, all replication origins, whether created
    # by an apply worker or table sync worker, should have been cleaned up.
    result = node_subscriber.safe_sql(
        "SELECT count(*) FROM pg_replication_origin_status")
    assert result == "0", "all replication origins have been cleaned up"

    node_subscriber.stop("fast")
    node_publisher.stop("fast")
