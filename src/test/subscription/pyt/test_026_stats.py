# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for subscription stats."""


def create_sub_pub_w_errors(
    node_publisher, node_subscriber, db, table_name, sequence_name
):
    # Initial table and sequence setup on both publisher and subscriber.
    #
    # Tables: Created on both nodes, but the subscriber version includes
    # primary keys and pre-populated data that will intentionally conflict
    # with replicated data from the publisher.
    #
    # Sequences: Created on both nodes with different INCREMENT values to
    # intentionally trigger replication conflicts.
    node_publisher.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE {table_name}(a int);
        ALTER TABLE {table_name} REPLICA IDENTITY FULL;
        INSERT INTO {table_name} VALUES (1);
        CREATE SEQUENCE {sequence_name};
        COMMIT;
        """,
        dbname=db,
    )
    node_subscriber.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE {table_name}(a int primary key);
        INSERT INTO {table_name} VALUES (1);
        CREATE SEQUENCE {sequence_name} INCREMENT BY 10;
        COMMIT;
        """,
        dbname=db,
    )

    # Set up publication.
    pub_name = table_name + "_pub"
    pub_seq_name = sequence_name + "_pub"
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname={db}"
    )

    node_publisher.safe_sql(
        f"""
        CREATE PUBLICATION {pub_name} FOR TABLE {table_name};
        CREATE PUBLICATION {pub_seq_name} FOR ALL SEQUENCES;
        """,
        dbname=db,
    )

    # Create subscription. The tablesync for table on subscription will enter
    # into infinite error loop due to violating the unique constraint. The
    # sequencesync will also fail due to different sequence increment values on
    # publisher and subscriber.
    sub_name = table_name + "_sub"
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION {sub_name} CONNECTION '{publisher_connstr}' "
        f"PUBLICATION {pub_name}, {pub_seq_name}",
        dbname=db,
    )

    node_publisher.wait_for_catchup(sub_name)

    # Wait for the tablesync and sequencesync error to be reported.
    assert node_subscriber.poll_query_until(
        f"""
        SELECT count(1) = 1 FROM pg_stat_subscription_stats
        WHERE subname = '{sub_name}' AND sync_seq_error_count > 0 AND sync_table_error_count > 0
        """,
        dbname=db,
    ), (
        f"Timed out while waiting for sequencesync errors and tablesync errors "
        f"for subscription '{sub_name}'"
    )

    # Change the sequence INCREMENT value back to the default on the subscriber
    # so it doesn't error out.
    node_subscriber.safe_sql(f"ALTER SEQUENCE {sequence_name} INCREMENT 1", dbname=db)

    # Wait for sequencesync to finish.
    assert node_subscriber.poll_query_until(
        f"""
        SELECT count(1) = 1 FROM pg_subscription_rel
        WHERE srrelid = '{sequence_name}'::regclass AND srsubstate = 'r'
        """,
        dbname=db,
    ), (
        f"Timed out while waiting for subscriber to synchronize data for "
        f"sequence '{sequence_name}'."
    )

    # Truncate test_tab1 so that tablesync worker can continue.
    node_subscriber.safe_sql(f"TRUNCATE {table_name}", dbname=db)

    # Wait for initial tablesync to finish.
    assert node_subscriber.poll_query_until(
        f"""
        SELECT count(1) = 1 FROM pg_subscription_rel
        WHERE srrelid = '{table_name}'::regclass AND srsubstate in ('r', 's')
        """,
        dbname=db,
    ), (
        f"Timed out while waiting for subscriber to synchronize data for "
        f"table '{table_name}'."
    )

    # Check test table on the subscriber has one row.
    result = node_subscriber.safe_sql(f"SELECT a FROM {table_name}", dbname=db)
    assert result == "1", f"Check that table '{table_name}' now has 1 row."

    # Insert data to test table on the publisher, raising an error on the
    # subscriber due to violation of the unique constraint on test table.
    node_publisher.safe_sql(f"INSERT INTO {table_name} VALUES (1)", dbname=db)

    # Wait for the subscriber to report both an apply error and an
    # insert_exists conflict.
    assert node_subscriber.poll_query_until(
        f"""
        SELECT apply_error_count > 0 AND confl_insert_exists > 0
        FROM pg_stat_subscription_stats
        WHERE subname = '{sub_name}'
        """,
        dbname=db,
    ), (
        f"Timed out while waiting for apply error and insert_exists conflict "
        f"for subscription '{sub_name}'"
    )

    # Truncate test table so that apply worker can continue.
    node_subscriber.safe_sql(f"TRUNCATE {table_name}", dbname=db)

    # Delete data from the test table on the publisher. This delete operation
    # should be skipped on the subscriber since the table is already empty.
    node_publisher.safe_sql(f"DELETE FROM {table_name};", dbname=db)

    # Wait for the subscriber to report a delete_missing conflict.
    assert node_subscriber.poll_query_until(
        f"""
        SELECT confl_delete_missing > 0
        FROM pg_stat_subscription_stats
        WHERE subname = '{sub_name}'
        """,
        dbname=db,
    ), (
        f"Timed out while waiting for delete_missing conflict for "
        f"subscription '{sub_name}'"
    )

    return (pub_name, sub_name)


def test_026_stats(create_pg):
    # Create publisher node.
    node_publisher = create_pg("publisher", allows_streaming="logical")

    # Create subscriber node.
    node_subscriber = create_pg("subscriber")

    db = "postgres"

    # There shouldn't be any subscription errors before starting logical
    # replication.
    result = node_subscriber.safe_sql(
        "SELECT count(1) FROM pg_stat_subscription_stats", dbname=db
    )
    assert result == "0", (
        "Check that there are no subscription errors before starting logical "
        "replication."
    )

    # Create the publication and subscription with sync and apply errors
    table1_name = "test_tab1"
    sequence1_name = "test_seq1"
    (_, sub1_name) = create_sub_pub_w_errors(
        node_publisher, node_subscriber, db, table1_name, sequence1_name
    )

    # Apply errors, sequencesync errors, tablesync errors, and conflicts are
    # > 0 and stats_reset timestamp is NULL.
    assert (
        node_subscriber.safe_sql(
            f"""SELECT apply_error_count > 0,
        sync_seq_error_count > 0,
        sync_table_error_count > 0,
        confl_insert_exists > 0,
        confl_delete_missing > 0,
        stats_reset IS NULL
        FROM pg_stat_subscription_stats
        WHERE subname = '{sub1_name}'""",
            dbname=db,
        )
        == "t|t|t|t|t|t"
    ), (
        f"Check that apply errors, sequencesync errors, tablesync errors, and "
        f"conflicts are > 0 and stats_reset is NULL for subscription "
        f"'{sub1_name}'."
    )

    # Reset a single subscription
    node_subscriber.safe_sql(
        f"SELECT pg_stat_reset_subscription_stats((SELECT subid FROM "
        f"pg_stat_subscription_stats WHERE subname = '{sub1_name}'))",
        dbname=db,
    )

    # Apply errors, sequencesync errors, tablesync errors, and conflicts are 0
    # and stats_reset timestamp is not NULL.
    assert (
        node_subscriber.safe_sql(
            f"""SELECT apply_error_count = 0,
        sync_seq_error_count = 0,
        sync_table_error_count = 0,
        confl_insert_exists = 0,
        confl_delete_missing = 0,
        stats_reset IS NOT NULL
        FROM pg_stat_subscription_stats
        WHERE subname = '{sub1_name}'""",
            dbname=db,
        )
        == "t|t|t|t|t|t"
    ), (
        f"Confirm that apply errors, sequencesync errors, tablesync errors, "
        f"and conflicts are 0 and stats_reset is not NULL after reset for "
        f"subscription '{sub1_name}'."
    )

    # Get reset timestamp
    reset_time1 = node_subscriber.safe_sql(
        f"SELECT stats_reset FROM pg_stat_subscription_stats "
        f"WHERE subname = '{sub1_name}'",
        dbname=db,
    )

    # Reset single sub again
    node_subscriber.safe_sql(
        f"SELECT pg_stat_reset_subscription_stats((SELECT subid FROM "
        f"pg_stat_subscription_stats WHERE subname = '{sub1_name}'))",
        dbname=db,
    )

    # check reset timestamp is newer after reset
    assert (
        node_subscriber.safe_sql(
            f"SELECT stats_reset > '{reset_time1}'::timestamptz FROM "
            f"pg_stat_subscription_stats WHERE subname = '{sub1_name}'",
            dbname=db,
        )
        == "t"
    ), f"Check reset timestamp for '{sub1_name}' is newer after second reset."

    # Make second subscription and publication
    table2_name = "test_tab2"
    sequence2_name = "test_seq2"
    (_, sub2_name) = create_sub_pub_w_errors(
        node_publisher, node_subscriber, db, table2_name, sequence2_name
    )

    # Apply errors, sequencesync errors, tablesync errors, and conflicts are
    # > 0 and stats_reset timestamp is NULL
    assert (
        node_subscriber.safe_sql(
            f"""SELECT apply_error_count > 0,
        sync_seq_error_count > 0,
        sync_table_error_count > 0,
        confl_insert_exists > 0,
        confl_delete_missing > 0,
        stats_reset IS NULL
        FROM pg_stat_subscription_stats
        WHERE subname = '{sub2_name}'""",
            dbname=db,
        )
        == "t|t|t|t|t|t"
    ), (
        f"Confirm that apply errors, sequencesync errors, tablesync errors, "
        f"and conflicts are > 0 and stats_reset is NULL for sub '{sub2_name}'."
    )

    # Reset all subscriptions
    node_subscriber.safe_sql("SELECT pg_stat_reset_subscription_stats(NULL)", dbname=db)

    # Apply errors, sequencesync errors, tablesync errors, and conflicts are 0
    # and stats_reset timestamp is not NULL.
    assert (
        node_subscriber.safe_sql(
            f"""SELECT apply_error_count = 0,
        sync_seq_error_count = 0,
        sync_table_error_count = 0,
        confl_insert_exists = 0,
        confl_delete_missing = 0,
        stats_reset IS NOT NULL
        FROM pg_stat_subscription_stats
        WHERE subname = '{sub1_name}'""",
            dbname=db,
        )
        == "t|t|t|t|t|t"
    ), (
        f"Confirm that apply errors, sequencesync errors, tablesync errors, "
        f"and conflicts are 0 and stats_reset is not NULL for sub "
        f"'{sub1_name}' after reset."
    )

    assert (
        node_subscriber.safe_sql(
            f"""SELECT apply_error_count = 0,
        sync_seq_error_count = 0,
        sync_table_error_count = 0,
        confl_insert_exists = 0,
        confl_delete_missing = 0,
        stats_reset IS NOT NULL
        FROM pg_stat_subscription_stats
        WHERE subname = '{sub2_name}'""",
            dbname=db,
        )
        == "t|t|t|t|t|t"
    ), (
        f"Confirm that apply errors, sequencesync errors, tablesync errors, "
        f"errors, and conflicts are 0 and stats_reset is not NULL for sub "
        f"'{sub2_name}' after reset."
    )

    reset_time1 = node_subscriber.safe_sql(
        f"SELECT stats_reset FROM pg_stat_subscription_stats "
        f"WHERE subname = '{sub1_name}'",
        dbname=db,
    )
    reset_time2 = node_subscriber.safe_sql(
        f"SELECT stats_reset FROM pg_stat_subscription_stats "
        f"WHERE subname = '{sub2_name}'",
        dbname=db,
    )

    # Reset all subscriptions
    node_subscriber.safe_sql("SELECT pg_stat_reset_subscription_stats(NULL)", dbname=db)

    # check reset timestamp for sub1 is newer after reset
    assert (
        node_subscriber.safe_sql(
            f"SELECT stats_reset > '{reset_time1}'::timestamptz FROM "
            f"pg_stat_subscription_stats WHERE subname = '{sub1_name}'",
            dbname=db,
        )
        == "t"
    ), (
        f"Confirm that reset timestamp for '{sub1_name}' is newer after "
        f"second reset."
    )

    # check reset timestamp for sub2 is newer after reset
    assert (
        node_subscriber.safe_sql(
            f"SELECT stats_reset > '{reset_time2}'::timestamptz FROM "
            f"pg_stat_subscription_stats WHERE subname = '{sub2_name}'",
            dbname=db,
        )
        == "t"
    ), (
        f"Confirm that reset timestamp for '{sub2_name}' is newer after "
        f"second reset."
    )

    # Get subscription 1 oid
    sub1_oid = node_subscriber.safe_sql(
        f"SELECT oid FROM pg_subscription WHERE subname = '{sub1_name}'",
        dbname=db,
    )

    # Drop subscription 1
    node_subscriber.safe_sql(f"DROP SUBSCRIPTION {sub1_name}", dbname=db)

    # Subscription stats for sub1 should be gone
    assert (
        node_subscriber.safe_sql(
            f"SELECT pg_stat_have_stats('subscription', 0, {sub1_oid})", dbname=db
        )
        == "f"
    ), f"Subscription stats for subscription '{sub1_name}' should be removed."

    # Get subscription 2 oid
    sub2_oid = node_subscriber.safe_sql(
        f"SELECT oid FROM pg_subscription WHERE subname = '{sub2_name}'",
        dbname=db,
    )

    # Disassociate the subscription 2 from its replication slot and drop it
    node_subscriber.safe_sql(f"ALTER SUBSCRIPTION {sub2_name} DISABLE", dbname=db)
    node_subscriber.safe_sql(
        f"ALTER SUBSCRIPTION {sub2_name} SET (slot_name = NONE)", dbname=db
    )
    node_subscriber.safe_sql(f"DROP SUBSCRIPTION {sub2_name}", dbname=db)

    # Subscription stats for sub2 should be gone
    assert (
        node_subscriber.safe_sql(
            f"SELECT pg_stat_have_stats('subscription', 0, {sub2_oid})", dbname=db
        )
        == "f"
    ), f"Subscription stats for subscription '{sub2_name}' should be removed."

    # Since disabling subscription doesn't wait for walsender to release the
    # replication slot and exit, wait for the slot to become inactive.
    assert node_publisher.poll_query_until(
        f"SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
        f"WHERE slot_name = '{sub2_name}' AND active_pid IS NULL)",
        dbname=db,
    ), "slot never became inactive"

    node_publisher.safe_sql(
        f"SELECT pg_drop_replication_slot('{sub2_name}')", dbname=db
    )

    node_subscriber.stop("fast")
    node_publisher.stop("fast")
