# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test that sequences are synced correctly to the subscriber."""


def test_036_sequences(create_pg):
    # Initialize publisher node
    #
    # No extra authentication setup is needed to allow connections from
    # regress_seq_repl: the framework initdb's with trust auth, which covers
    # both the local socket and loopback TCP.
    node_publisher = create_pg("publisher", allows_streaming="logical")

    # Initialize subscriber node
    node_subscriber = create_pg("subscriber")

    # Setup structure on the publisher
    node_publisher.safe_sql(
        "CREATE TABLE regress_seq_test (v BIGINT);\n"
        "CREATE SEQUENCE regress_s1;\n"
        "CREATE SEQUENCE \"regress'quote\";")

    # Setup the same structure on the subscriber, plus some extra sequences
    # that we'll create on the publisher later
    node_subscriber.safe_sql(
        "CREATE TABLE regress_seq_test (v BIGINT);\n"
        "CREATE SEQUENCE regress_s1;\n"
        "CREATE SEQUENCE regress_s2;\n"
        "CREATE SEQUENCE regress_s3;\n"
        "CREATE SEQUENCE \"regress'quote\";")

    # Insert initial test data
    node_publisher.safe_sql(
        "-- generate a number of values using the sequence\n"
        "INSERT INTO regress_seq_test SELECT nextval('regress_s1') "
        "FROM generate_series(1,100);\n"
        "INSERT INTO regress_seq_test SELECT nextval('\"regress''quote\"') "
        "FROM generate_series(1,100);")

    # Setup logical replication pub/sub
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres")
    node_publisher.safe_sql(
        "CREATE PUBLICATION regress_seq_pub FOR ALL SEQUENCES")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION regress_seq_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION regress_seq_pub")

    # Wait for initial sync to finish
    synced_query = (
        "SELECT count(1) = 0 FROM pg_subscription_rel "
        "WHERE srsubstate NOT IN ('r');")
    assert node_subscriber.poll_query_until(synced_query), \
        "Timed out while waiting for subscriber to synchronize data"

    # Check the initial data on subscriber
    result = node_subscriber.safe_sql(
        "SELECT last_value, is_called FROM regress_s1;")
    assert result == "100|t", "initial test data replicated"

    result = node_subscriber.safe_sql(
        "SELECT last_value, is_called FROM \"regress'quote\";")
    assert result == "100|t", \
        "initial test data replicated for sequence name having quotes"

    ##########
    # ALTER SUBSCRIPTION ... REFRESH PUBLICATION should cause sync of new
    # sequences of the publisher, but changes to existing sequences should
    # not be synced.
    ##########

    # Create a new sequence 'regress_s2', and update existing sequence
    # 'regress_s1'
    node_publisher.safe_sql(
        "CREATE SEQUENCE regress_s2;\n"
        "INSERT INTO regress_seq_test SELECT nextval('regress_s2') "
        "FROM generate_series(1,100);\n"
        "-- Existing sequence\n"
        "INSERT INTO regress_seq_test SELECT nextval('regress_s1') "
        "FROM generate_series(1,100);")

    # Do ALTER SUBSCRIPTION ... REFRESH PUBLICATION
    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION regress_seq_sub REFRESH PUBLICATION;")
    assert node_subscriber.poll_query_until(synced_query), \
        "Timed out while waiting for subscriber to synchronize data"

    result = node_publisher.safe_sql(
        "SELECT last_value, is_called FROM regress_s1;")
    assert result == "200|t", "Check sequence value in the publisher"

    # Check - existing sequence ('regress_s1') is not synced
    result = node_subscriber.safe_sql(
        "SELECT last_value, is_called FROM regress_s1;")
    assert result == "100|t", \
        "REFRESH PUBLICATION will not sync existing sequence"

    # Check - newly published sequence ('regress_s2') is synced
    result = node_subscriber.safe_sql(
        "SELECT last_value, is_called FROM regress_s2;")
    assert result == "100|t", \
        "REFRESH PUBLICATION will sync newly published sequence"

    ##########
    # Test: REFRESH SEQUENCES and REFRESH PUBLICATION (copy_data = false)
    #
    # 1. ALTER SUBSCRIPTION ... REFRESH SEQUENCES should re-synchronize all
    #    existing sequences, but not synchronize newly added ones.
    # 2. ALTER SUBSCRIPTION ... REFRESH PUBLICATION with (copy_data = false)
    #    should also not update sequence values for newly added sequences.
    ##########

    # Create a new sequence 'regress_s3', and update the existing sequence
    # 'regress_s2'.
    node_publisher.safe_sql(
        "CREATE SEQUENCE regress_s3;\n"
        "INSERT INTO regress_seq_test SELECT nextval('regress_s3') "
        "FROM generate_series(1,100);\n"
        "-- Existing sequence\n"
        "INSERT INTO regress_seq_test SELECT nextval('regress_s2') "
        "FROM generate_series(1,100);")

    # 1. Do ALTER SUBSCRIPTION ... REFRESH SEQUENCES
    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION regress_seq_sub REFRESH SEQUENCES;")
    assert node_subscriber.poll_query_until(synced_query), \
        "Timed out while waiting for subscriber to synchronize data"

    # Check - existing sequences ('regress_s1' and 'regress_s2') are synced
    result = node_subscriber.safe_sql(
        "SELECT last_value, is_called FROM regress_s1;")
    assert result == "200|t", "REFRESH SEQUENCES will sync existing sequences"
    result = node_subscriber.safe_sql(
        "SELECT last_value, is_called FROM regress_s2;")
    assert result == "200|t", "REFRESH SEQUENCES will sync existing sequences"

    # Check - newly published sequence ('regress_s3') is not synced
    result = node_subscriber.safe_sql(
        "SELECT last_value, is_called FROM regress_s3;")
    assert result == "1|f", \
        "REFRESH SEQUENCES will not sync newly published sequence"

    # 2. Do ALTER SUBSCRIPTION ... REFRESH PUBLICATION with copy_data as false
    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION regress_seq_sub REFRESH PUBLICATION "
        "WITH (copy_data = false);")
    assert node_subscriber.poll_query_until(synced_query), \
        "Timed out while waiting for subscriber to synchronize data"

    # Check - newly published sequence ('regress_s3') is not synced with
    # copy_data as false.
    result = node_subscriber.safe_sql(
        "SELECT last_value, is_called FROM regress_s3;")
    assert result == "1|f", \
        "REFRESH PUBLICATION will not sync newly published sequence with " \
        "copy_data as false"

    ##########
    # ALTER SUBSCRIPTION ... REFRESH PUBLICATION should report an error when:
    # a) sequence definitions differ between the publisher and subscriber, or
    # b) a sequence is missing on the publisher.
    ##########

    # Create a new sequence 'regress_s4' whose START value is not the same in
    # the publisher and subscriber.
    node_publisher.safe_sql(
        "CREATE SEQUENCE regress_s4 START 1 INCREMENT 2;")

    node_subscriber.safe_sql(
        "CREATE SEQUENCE regress_s4 START 10 INCREMENT 2;")

    log_offset = node_subscriber.log_position()

    # Do ALTER SUBSCRIPTION ... REFRESH PUBLICATION
    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION regress_seq_sub REFRESH PUBLICATION")

    # Verify that an error is logged for parameter differences on sequence
    # ('regress_s4').
    node_subscriber.wait_for_log(
        r'WARNING: ( [A-Z0-9]+:)? mismatched or renamed sequence on '
        r'subscriber \("public.regress_s4"\)',
        log_offset)

    # Verify that an error is logged for the missing sequence ('regress_s4').
    node_publisher.safe_sql("DROP SEQUENCE regress_s4;")

    node_subscriber.wait_for_log(
        r'WARNING: ( [A-Z0-9]+:)? missing sequence on publisher '
        r'\("public.regress_s4"\)',
        log_offset)

    # Recreate regress_s4 so later tests that reuse the subscription do not
    # keep reporting the intentionally-missing sequence from the previous test.
    node_publisher.safe_sql(
        "CREATE SEQUENCE regress_s4 START 10 INCREMENT 2;")

    ##########
    # Ensure that insufficient privileges on the publisher for a sequence do
    # not disrupt the subscriber. The subscriber should log a warning and
    # continue retrying.
    ##########

    node_publisher.safe_sql(
        "CREATE ROLE regress_seq_repl LOGIN REPLICATION;\n"
        "GRANT USAGE ON SCHEMA public TO regress_seq_repl;\n"
        "GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO regress_seq_repl;\n"
        "REVOKE ALL ON SEQUENCE regress_s2 FROM regress_seq_repl;")

    publisher_limited_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} "
        "dbname=postgres user=regress_seq_repl")
    log_offset = node_subscriber.log_position()

    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION regress_seq_sub CONNECTION "
        f"'{publisher_limited_connstr}'")

    node_subscriber.safe_sql(
        "ALTER SUBSCRIPTION regress_seq_sub REFRESH SEQUENCES")

    node_subscriber.wait_for_log(
        r'WARNING: ( [A-Z0-9]+:)? missing sequence on publisher '
        r'\("public.regress_s2"\)',
        log_offset)
