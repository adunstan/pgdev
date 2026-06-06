# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test logical replication slots are always flushed to disk during a shutdown
checkpoint.
"""

import re


def compare_confirmed_flush(node, confirmed_flush_from_log):
    # Fetch Latest checkpoint location from the control file
    res = node.pg_bin.result(["pg_controldata", node.data_dir])
    latest_checkpoint = None
    for line in res.stdout.splitlines():
        m = re.match(r"^Latest checkpoint location:\s*(.*)$", line)
        if m:
            latest_checkpoint = m.group(1)
            break
    assert (
        latest_checkpoint is not None
    ), "Latest checkpoint location not found in control file"

    # Is it same as the value read from log?
    assert latest_checkpoint == confirmed_flush_from_log, (
        "Check that the slot's confirmed_flush LSN is the same as the "
        "latest_checkpoint location"
    )


def test_038_save_logical_slots_shutdown(create_pg):
    # Initialize publisher node
    node_publisher = create_pg("pub", start=False, allows_streaming="logical")
    # Avoid checkpoint during the test, otherwise, the latest checkpoint
    # location will change.
    node_publisher.append_conf("checkpoint_timeout = 1h\nautovacuum = off\n")
    node_publisher.start()

    # Create subscriber node
    node_subscriber = create_pg("sub")

    # Create tables
    node_publisher.safe_sql("CREATE TABLE test_tbl (id int)")
    node_subscriber.safe_sql("CREATE TABLE test_tbl (id int)")

    # To avoid a shutdown checkpoint WAL record (that gets generated as part of
    # the publisher restart below) falling into a new page, advance the WAL
    # segment. Otherwise, the confirmed_flush_lsn and shutdown_checkpoint
    # location won't match.
    node_publisher.advance_wal(1)

    # Insert some data
    node_publisher.safe_sql("INSERT INTO test_tbl VALUES (generate_series(1, 5));")

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql("CREATE PUBLICATION pub FOR ALL TABLES")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub CONNECTION '{publisher_connstr}' " "PUBLICATION pub"
    )

    node_subscriber.wait_for_subscription_sync(node_publisher, "sub")

    result = node_subscriber.safe_sql("SELECT count(*) FROM test_tbl")
    assert result == "5", "check initial copy was done"

    offset = node_publisher.log_position()

    # Note: Don't insert any data on the publisher that may cause the shutdown
    # checkpoint to fall into a new WAL file. See the comments atop
    # advance_wal() above.

    # Restart the publisher to ensure that the slot will be flushed if required
    node_publisher.restart()

    # Wait until the walsender creates decoding context
    pattern = (
        r"Streaming transactions committing after ([A-F0-9]+/[A-F0-9]+), "
        r"reading WAL from ([A-F0-9]+/[A-F0-9]+)\."
    )
    node_publisher.wait_for_log(pattern, offset)

    # Extract confirmed_flush from the logfile
    log_contents = node_publisher.log_content()[offset:]
    m = re.search(pattern, log_contents)
    assert m is not None, "could not get confirmed_flush_lsn"

    # Ensure that the slot's confirmed_flush LSN is the same as the
    # latest_checkpoint location.
    compare_confirmed_flush(node_publisher, m.group(1))
