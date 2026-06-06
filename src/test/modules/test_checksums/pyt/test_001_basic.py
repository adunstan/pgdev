# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test suite for testing enabling data checksums in an online cluster."""


def test_001_basic(create_pg, checksums):
    # Initialize node with checksums disabled.
    node = create_pg("basic_node", start=False, initdb_extra=["--no-data-checksums"])
    node.start()

    # Create some content to have un-checksummed data in the cluster
    node.safe_sql("CREATE TABLE t AS SELECT generate_series(1,10000) AS a;")

    # Ensure that checksums are turned off
    checksums.test_checksum_state(node, "off")

    # Enable data checksums and wait for the state transition to 'on'
    checksums.enable_data_checksums(node, wait="on")

    # Run a dummy query just to make sure we can read back data
    result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1 ")
    assert result == "9999", "ensure checksummed pages can be read back"

    # Enable data checksums again which should be a no-op so we explicitly don't
    # wait for any state transition as none should happen here.
    checksums.enable_data_checksums(node)
    checksums.test_checksum_state(node, "on")
    # ..and make sure we can still read/write data
    node.safe_sql("UPDATE t SET a = a + 1;")
    result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1")
    assert result == "10000", "ensure checksummed pages can be read back"

    # Disable checksums again and wait for the state transition
    checksums.disable_data_checksums(node, wait=1)

    # Test reading data again
    result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1")
    assert result == "10000", "ensure previously checksummed pages can be read back"

    # Re-enable checksums and make sure that the underlying data has changed to
    # ensure that checksums will be different.
    node.safe_sql("UPDATE t SET a = a + 1;")
    checksums.enable_data_checksums(node, wait="on")

    # Run a dummy query just to make sure we can read back the data
    result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1")
    assert result == "10000", "ensure checksummed pages can be read back"

    node.stop()
