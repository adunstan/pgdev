# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests that logical decoding messages are decoded correctly."""


def test_020_messages(create_pg):
    # Create publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_publisher.append_conf("autovacuum = off")
    node_publisher.restart()

    # Create subscriber node
    node_subscriber = create_pg("subscriber")

    # Create some preexisting content on publisher
    node_publisher.safe_sql("CREATE TABLE tab_test (a int primary key)")

    # Setup structure on subscriber
    node_subscriber.safe_sql("CREATE TABLE tab_test (a int primary key)")

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql("CREATE PUBLICATION tap_pub FOR TABLE tab_test")

    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub"
    )

    node_publisher.wait_for_catchup("tap_sub")

    # Ensure a transactional logical decoding message shows up on the slot
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub DISABLE")

    # wait for the replication slot to become inactive on the publisher
    assert node_publisher.poll_query_until(
        "SELECT COUNT(*) FROM pg_catalog.pg_replication_slots "
        "WHERE slot_name = 'tap_sub' AND active='f'",
        "1",
    )

    node_publisher.safe_sql(
        "SELECT pg_logical_emit_message(true, 'pgoutput', 'a transactional message')"
    )

    result = node_publisher.safe_sql(
        "SELECT get_byte(data, 0)\n"
        "FROM pg_logical_slot_peek_binary_changes('tap_sub', NULL, NULL,\n"
        "    'proto_version', '1',\n"
        "    'publication_names', 'tap_pub',\n"
        "    'messages', 'true')"
    )

    # 66 77 67 == B M C == BEGIN MESSAGE COMMIT
    assert result == "66\n77\n67", "messages on slot are B M C with message option"

    result = node_publisher.safe_sql(
        "SELECT get_byte(data, 1), encode(substr(data, 11, 8), 'escape')\n"
        "FROM pg_logical_slot_peek_binary_changes('tap_sub', NULL, NULL,\n"
        "    'proto_version', '1',\n"
        "    'publication_names', 'tap_pub',\n"
        "    'messages', 'true')\n"
        "OFFSET 1 LIMIT 1"
    )

    assert (
        result == "1|pgoutput"
    ), "flag transactional is set to 1 and prefix is pgoutput"

    result = node_publisher.safe_sql(
        "SELECT get_byte(data, 0)\n"
        "FROM pg_logical_slot_get_binary_changes('tap_sub', NULL, NULL,\n"
        "    'proto_version', '1',\n"
        "    'publication_names', 'tap_pub')"
    )

    # no message and no BEGIN and COMMIT because of empty transaction
    # optimization
    assert (
        result == ""
    ), "option messages defaults to false so message (M) is not available on slot"

    node_publisher.safe_sql("INSERT INTO tab_test VALUES (1)")

    message_lsn = node_publisher.safe_sql(
        "SELECT pg_logical_emit_message(false, 'pgoutput', "
        "'a non-transactional message')"
    )

    node_publisher.safe_sql("INSERT INTO tab_test VALUES (2)")

    result = node_publisher.safe_sql(
        "SELECT get_byte(data, 0), get_byte(data, 1)\n"
        "FROM pg_logical_slot_get_binary_changes('tap_sub', NULL, NULL,\n"
        "    'proto_version', '1',\n"
        "    'publication_names', 'tap_pub',\n"
        "    'messages', 'true')\n"
        f"WHERE lsn = '{message_lsn}' AND xid = 0"
    )

    assert result == "77|0", "non-transactional message on slot is M"

    # Ensure a non-transactional logical decoding message shows up on the slot
    # when it is emitted within an aborted transaction. The message won't emit
    # until something advances the LSN, hence, we intentionally forces the
    # server to switch to a new WAL file.
    node_publisher.safe_sql(
        "BEGIN;\n"
        "SELECT pg_logical_emit_message(false, 'pgoutput',\n"
        "    'a non-transactional message is available even if the "
        "transaction is aborted 1');\n"
        "INSERT INTO tab_test VALUES (3);\n"
        "SELECT pg_logical_emit_message(true, 'pgoutput',\n"
        "    'a transactional message is not available if the transaction "
        "is aborted');\n"
        "SELECT pg_logical_emit_message(false, 'pgoutput',\n"
        "    'a non-transactional message is available even if the "
        "transaction is aborted 2');\n"
        "ROLLBACK;\n"
        "SELECT pg_switch_wal();"
    )

    result = node_publisher.safe_sql(
        "SELECT get_byte(data, 0), get_byte(data, 1)\n"
        "FROM pg_logical_slot_peek_binary_changes('tap_sub', NULL, NULL,\n"
        "    'proto_version', '1',\n"
        "    'publication_names', 'tap_pub',\n"
        "    'messages', 'true')"
    )

    assert (
        result == "77|0\n77|0"
    ), "non-transactional message on slot from aborted transaction is M"

    node_subscriber.stop("fast")
    node_publisher.stop("fast")
