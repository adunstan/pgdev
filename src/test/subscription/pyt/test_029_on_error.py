# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for disable_on_error and SKIP transaction features."""

import re


def skip_lsn(
    node_publisher, node_subscriber, offset, nonconflict_data, expected, msg
):
    """Test skipping the transaction.

    This function must be called after the caller has inserted data that
    conflicts with the subscriber.  The finish LSN of the error transaction
    that is used to specify to ALTER SUBSCRIPTION ... SKIP is fetched from
    the server logs.  After executing ALTER SUBSCRIPTION ... SKIP, we check
    if logical replication can continue working by inserting nonconflict_data
    on the publisher.

    Returns the new log offset.
    """

    # Wait until a conflict occurs on the subscriber.
    node_subscriber.poll_query_until(
        "SELECT subenabled = FALSE FROM pg_subscription WHERE subname = 'sub'"
    )

    # Get the finish LSN of the error transaction, mapping the expected
    # ERROR with its CONTEXT when retrieving this information.
    contents = node_subscriber.log_content()[offset:]
    match = re.search(
        r'conflict detected on relation "public.tbl".*\n.*DETAIL:.* Could not '
        r'apply remote change.*\n.*Key already exists in unique index '
        r'"tbl_pkey", modified by .*origin.* in transaction \d+ at .*: '
        r'key .*, local row .*\n.*CONTEXT:.* for replication target relation '
        r'"public.tbl" in transaction \d+, finished at '
        r"([0-9a-fA-F]+/[0-9a-fA-F]+)",
        contents,
    )
    assert match, "could not get error-LSN"
    lsn = match.group(1)

    # Set skip lsn.
    node_subscriber.safe_sql(f"ALTER SUBSCRIPTION sub SKIP (lsn = '{lsn}')")

    # Re-enable the subscription.
    node_subscriber.safe_sql("ALTER SUBSCRIPTION sub ENABLE")

    # Wait for the failed transaction to be skipped
    node_subscriber.poll_query_until(
        "SELECT subskiplsn = '0/0' FROM pg_subscription WHERE subname = 'sub'"
    )

    # Check the log to ensure that the transaction is skipped, and advance the
    # offset of the log file for the next test.
    offset = node_subscriber.wait_for_log(
        r"LOG: ( [A-Z0-9]+:)? logical replication completed skipping "
        rf"transaction at LSN {lsn}",
        offset,
    )

    # Insert non-conflict data
    node_publisher.safe_sql(f"INSERT INTO tbl VALUES {nonconflict_data}")

    node_publisher.wait_for_catchup("sub")

    # Check replicated data
    res = node_subscriber.safe_sql("SELECT count(*) FROM tbl")
    assert res == expected, msg

    return offset


def test_029_on_error(create_pg):
    offset = 0

    # Create publisher node. Set a low value of logical_decoding_work_mem to
    # test streaming cases.
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_publisher.append_conf(
        "\n".join(
            [
                "logical_decoding_work_mem = 64kB",
                "max_prepared_transactions = 10",
            ]
        )
    )
    node_publisher.restart()

    # Create subscriber node
    node_subscriber = create_pg("subscriber")
    node_subscriber.append_conf(
        "\n".join(
            [
                "max_prepared_transactions = 10",
                "track_commit_timestamp = on",
            ]
        )
    )
    node_subscriber.restart()

    # Initial table setup on both publisher and subscriber. On the subscriber,
    # we create the same tables but with a primary key. Also, insert some data
    # that will conflict with the data replicated from publisher later.
    node_publisher.safe_sql(
        """
        CREATE TABLE tbl (i INT, t BYTEA);
        ALTER TABLE tbl REPLICA IDENTITY FULL;
        INSERT INTO tbl VALUES (1, NULL);
        """
    )
    node_subscriber.safe_sql(
        """
        CREATE TABLE tbl (i INT PRIMARY KEY, t BYTEA);
        INSERT INTO tbl VALUES (1, NULL);
        """
    )

    # Create a pub/sub to set up logical replication. This tests that the
    # uniqueness violation will cause the subscription to fail during initial
    # synchronization and make it disabled.
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql("CREATE PUBLICATION pub FOR TABLE tbl")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION pub WITH (disable_on_error = true, streaming = on, "
        "two_phase = on)"
    )

    # Initial synchronization failure causes the subscription to be disabled.
    assert node_subscriber.poll_query_until(
        "SELECT subenabled = false FROM pg_catalog.pg_subscription "
        "WHERE subname = 'sub'"
    ), "Timed out while waiting for subscriber to be disabled"

    # Truncate the table on the subscriber which caused the subscription to be
    # disabled.
    node_subscriber.safe_sql("TRUNCATE tbl")

    # Re-enable the subscription "sub".
    node_subscriber.safe_sql("ALTER SUBSCRIPTION sub ENABLE")

    # Wait for the data to replicate.
    node_subscriber.wait_for_subscription_sync(node_publisher, "sub")

    # Confirm that we have finished the table sync.
    result = node_subscriber.safe_sql("SELECT COUNT(*) FROM tbl")
    assert result == "1", "subscription sub replicated data"

    # Insert data to tbl, raising an error on the subscriber due to violation
    # of the unique constraint on tbl. Then skip the transaction.
    node_publisher.safe_sql(
        """
        BEGIN;
        INSERT INTO tbl VALUES (1, NULL);
        COMMIT;
        """
    )
    offset = skip_lsn(
        node_publisher,
        node_subscriber,
        offset,
        "(2, NULL)",
        "2",
        "test skipping transaction",
    )

    # Test for PREPARE and COMMIT PREPARED. Update the data and PREPARE the
    # transaction, raising an error on the subscriber due to violation of the
    # unique constraint on tbl. Then skip the transaction.
    # COMMIT PREPARED must be issued outside a transaction block, so it is
    # sent as a separate command (safe_sql wraps a multi-statement string in
    # one implicit transaction).
    node_publisher.safe_sql(
        """
        BEGIN;
        UPDATE tbl SET i = 2;
        PREPARE TRANSACTION 'gtx';
        """
    )
    node_publisher.safe_sql("COMMIT PREPARED 'gtx';")
    offset = skip_lsn(
        node_publisher,
        node_subscriber,
        offset,
        "(3, NULL)",
        "3",
        "test skipping prepare and commit prepared ",
    )

    # Test for STREAM COMMIT. Insert enough rows to tbl to exceed the 64kB
    # limit, also raising an error on the subscriber during applying spooled
    # changes for the same reason. Then skip the transaction.
    node_publisher.safe_sql(
        """
        BEGIN;
        INSERT INTO tbl SELECT i, sha256(i::text::bytea) FROM generate_series(1, 10000) s(i);
        COMMIT;
        """
    )
    offset = skip_lsn(
        node_publisher,
        node_subscriber,
        offset,
        "(4, sha256(4::text::bytea))",
        "4",
        "test skipping stream-commit",
    )

    result = node_subscriber.safe_sql(
        "SELECT COUNT(*) FROM pg_prepared_xacts"
    )
    assert result == "0", (
        "check all prepared transactions are resolved on the subscriber"
    )

    node_subscriber.stop()
    node_publisher.stop()
