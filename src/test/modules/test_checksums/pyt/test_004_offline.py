# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test enabling data checksums offline from various states of checksum processing.

Uses the pg_checksums binary on a stopped cluster.
"""


def _checksum_enable_offline(node, pg_bin):
    """Enable data page checksums in an offline cluster with pg_checksums."""
    pg_bin.command_ok(
        ["pg_checksums", "-D", node.data_dir, "-e"],
        f"enable checksums offline in {node.name}",
    )


def _checksum_disable_offline(node, pg_bin):
    """Disable data page checksums in an offline cluster with pg_checksums."""
    pg_bin.command_ok(
        ["pg_checksums", "-D", node.data_dir, "-d"],
        f"disable checksums offline in {node.name}",
    )


def test_offline(create_pg, pg_bin, checksums):
    """Enable/disable/verify data checksums offline from various states."""
    # Initialize node with checksums disabled.
    node = create_pg("offline_node", initdb_extra=["--no-data-checksums"])

    # Create some content to have un-checksummed data in the cluster.
    node.safe_sql("CREATE TABLE t AS SELECT generate_series(1,10000) AS a;")

    # Ensure that checksums are disabled.
    checksums.test_checksum_state(node, "off")

    # Enable checksums offline using pg_checksums.
    node.stop()
    _checksum_enable_offline(node, pg_bin)
    node.start()

    # Ensure that checksums are enabled.
    checksums.test_checksum_state(node, "on")

    # Run a dummy query just to make sure we can read back some data.
    result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1")
    assert result == "9999", "ensure checksummed pages can be read back"

    # Disable checksums offline again using pg_checksums.
    node.stop()
    _checksum_disable_offline(node, pg_bin)
    node.start()

    # Ensure that checksums are disabled.
    checksums.test_checksum_state(node, "off")

    # Create a barrier for checksum enablement to block on, in this case a
    # pre-existing temporary table which is kept open while processing is
    # started.  We accomplish this with a dedicated session that keeps the
    # temporary table created as we enable checksums in another session.
    bsession = node.connect()
    bsession.do("CREATE TEMPORARY TABLE tt (a integer);")

    # In another session, make sure we can see the blocking temp table but
    # start processing anyways and check that we are blocked with a proper
    # wait event.
    result = node.safe_sql(
        "SELECT relpersistence FROM pg_catalog.pg_class WHERE relname = 'tt';"
    )
    assert result == "t", "ensure we can see the temporary table"

    # Enable, but stop waiting at inprogress-on since it will sit there until
    # the above temporary table is removed.
    checksums.enable_data_checksums(node, wait="inprogress-on")

    # Turn the cluster off and enable checksums offline, then start back up.
    # Stop the cluster before closing the background session since otherwise
    # checksums might have time to get enabled before shutting down the
    # cluster.
    node.stop("fast")
    bsession.close()
    _checksum_enable_offline(node, pg_bin)
    node.start()

    # Ensure that checksums are now enabled even though processing wasn't
    # restarted.
    checksums.test_checksum_state(node, "on")

    # Run a dummy query just to make sure we can read back some data.
    result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1")
    assert result == "9999", "ensure checksummed pages can be read back"

    node.stop()
