# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that pg_stat_replication_slots data is sane after dropping a slot and restarting."""

import os
import shutil


def _test_slot_stats(node, expected, msg):
    # Check that replication slot stats are expected.
    result = node.safe_sql(
        """
        SELECT slot_name, total_txns > 0 AS total_txn,
               total_bytes > 0 AS total_bytes
               FROM pg_stat_replication_slots
               ORDER BY slot_name""")
    assert result == expected, msg


def test_001_repl_stats(create_pg):
    # Test set-up
    node = create_pg("test", allows_streaming="logical", start=False)
    node.append_conf("synchronous_commit = on")
    node.start()

    # Create table.
    node.safe_sql("CREATE TABLE test_repl_stat(col1 int)")

    # Create replication slots.
    node.safe_sql(
        """
        SELECT pg_create_logical_replication_slot('regression_slot1', 'test_decoding');
        SELECT pg_create_logical_replication_slot('regression_slot2', 'test_decoding');
        SELECT pg_create_logical_replication_slot('regression_slot3', 'test_decoding');
        SELECT pg_create_logical_replication_slot('regression_slot4', 'test_decoding');
    """)

    # Insert some data.
    node.safe_sql(
        "INSERT INTO test_repl_stat values(generate_series(1, 5));")

    node.safe_sql(
        """
        SELECT data FROM pg_logical_slot_get_changes('regression_slot1', NULL,
        NULL, 'include-xids', '0', 'skip-empty-xacts', '1');
        SELECT data FROM pg_logical_slot_get_changes('regression_slot2', NULL,
        NULL, 'include-xids', '0', 'skip-empty-xacts', '1');
        SELECT data FROM pg_logical_slot_get_changes('regression_slot3', NULL,
        NULL, 'include-xids', '0', 'skip-empty-xacts', '1');
        SELECT data FROM pg_logical_slot_get_changes('regression_slot4', NULL,
        NULL, 'include-xids', '0', 'skip-empty-xacts', '1');
    """)

    # Wait for the statistics to be updated.
    assert node.poll_query_until(
        """
        SELECT count(slot_name) >= 4 FROM pg_stat_replication_slots
        WHERE slot_name ~ 'regression_slot'
        AND total_txns > 0 AND total_bytes > 0;
    """), "Timed out while waiting for statistics to be updated"

    # Test to drop one of the replication slot and verify replication statistics
    # data is fine after restart.
    node.safe_sql(
        "SELECT pg_drop_replication_slot('regression_slot4')")

    node.stop()
    node.start()

    # Verify statistics data present in pg_stat_replication_slots are sane after
    # restart.
    _test_slot_stats(
        node,
        "regression_slot1|t|t\n"
        "regression_slot2|t|t\n"
        "regression_slot3|t|t",
        "check replication statistics are updated")

    # Test to remove one of the replication slots and adjust
    # max_replication_slots accordingly to the number of slots. This leads
    # to a mismatch between the number of slots present in the stats file and the
    # number of stats present in shared memory. We verify
    # replication statistics data is fine after restart.

    node.stop()
    datadir = node.data_dir
    slot3_replslotdir = os.path.join(datadir, "pg_replslot", "regression_slot3")

    shutil.rmtree(slot3_replslotdir)

    node.append_conf("max_replication_slots = 2")
    node.start()

    # Verify statistics data present in pg_stat_replication_slots are sane after
    # restart.
    _test_slot_stats(
        node,
        "regression_slot1|t|t\n"
        "regression_slot2|t|t",
        "check replication statistics after removing the slot file")

    # cleanup
    node.safe_sql("DROP TABLE test_repl_stat")
    node.safe_sql(
        "SELECT pg_drop_replication_slot('regression_slot1')")
    node.safe_sql(
        "SELECT pg_drop_replication_slot('regression_slot2')")

    # shutdown
    node.stop()

    # Test replication slot stats persistence in a single session.  The slot
    # is dropped and created concurrently of a session peeking at its data
    # repeatedly, hence holding in its local cache a reference to the stats.
    node.start()

    slot_name_restart = "regression_slot5"
    node.safe_sql(
        f"SELECT pg_create_logical_replication_slot('{slot_name_restart}', 'test_decoding');"
    )

    # Look at slot data, with a persistent connection.
    bgsession = node.connect()

    # Look at slot data on this persistent session, incrementing the refcount
    # of the stats entry.  Run it to completion (so the slot is no longer
    # active and can be dropped) while the session stays open to keep the
    # stats reference held.
    bgsession.query(
        f"SELECT pg_logical_slot_peek_binary_changes('{slot_name_restart}', NULL, NULL)"
    )

    # Drop the slot entry.  The stats entry is not dropped yet as the previous
    # session still holds a reference to it.
    node.safe_sql(
        f"SELECT pg_drop_replication_slot('{slot_name_restart}')")

    # Create again the same slot.  The stats entry is reinitialized, not marked
    # as dropped anymore.
    node.safe_sql(
        f"SELECT pg_create_logical_replication_slot('{slot_name_restart}', 'test_decoding');"
    )

    # Look again at the slot data.  The local stats reference should be refreshed
    # to the reinitialized entry.
    bgsession.query(
        f"SELECT pg_logical_slot_peek_binary_changes('{slot_name_restart}', NULL, NULL)"
    )
    # Drop again the slot, the entry is not dropped yet as the previous session
    # still has a refcount on it.
    node.safe_sql(
        f"SELECT pg_drop_replication_slot('{slot_name_restart}')")

    # Shutdown the node, which should happen cleanly with the stats file written
    # to disk.  Note that the background session created previously needs to be
    # hold *while* the node is shutting down to check that it drops the stats
    # entry of the slot before writing the stats file.
    node.stop()

    # Make sure that the node is correctly shut down.  Checking the control file
    # is not enough, as the node may detect that something is incorrect after the
    # control file has been updated and the shutdown checkpoint is finished, so
    # also check that the stats file has been written out.
    node.command_like(
        ["pg_controldata", node.data_dir],
        r"Database cluster state:\s+shut down\n",
        "node shut down ok")

    stats_file = os.path.join(datadir, "pg_stat", "pgstat.stat")
    assert os.path.isfile(stats_file), "stats file must exist after shutdown"

    bgsession.close()
