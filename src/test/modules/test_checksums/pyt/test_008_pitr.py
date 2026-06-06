# Copyright (c) 2026, PostgreSQL Global Development Group

"""Tests point-in-time recovery interaction with online checksum enabling.  A
base backup is taken from a primary started with checksums disabled, then
checksums are enabled (recording the WAL LSN right after the flip), more work
is done, and a restore point is created.  A recovery node is initialized from
the backup and recovered to the recorded LSN, after which we verify that the
checksum state and table data match what the primary had at that point and
that no checksum validation errors were logged.
"""

import os
import re

import pytest


def test_008_pitr(create_pg, checksums):
    # This test suite is expensive to execute.  It honours two PG_TEST_EXTRA
    # options, "checksum" (pared-down) and "checksum_extended" (full); without
    # either it skips entirely.
    pg_test_extra = os.environ.get("PG_TEST_EXTRA", "")
    if not re.search(r"\bchecksum(_extended)?\b", pg_test_extra):
        pytest.skip("Expensive data checksums test disabled")

    data_checksum_state = "off"

    # Invert the state of data checksums in the cluster.  Returns the WAL LSNs
    # recorded immediately before and after the flip.
    def flip_data_checksums(node):
        nonlocal data_checksum_state

        # First, make sure the cluster is in the state we expect it to be.
        checksums.test_checksum_state(node, data_checksum_state)

        if data_checksum_state == "off":
            lsn_pre = node.safe_sql("SELECT pg_current_wal_lsn()")
            checksums.enable_data_checksums(node, wait="on")
            lsn_post = node.safe_sql("SELECT pg_current_wal_lsn()")
            data_checksum_state = "on"
        else:
            lsn_pre = node.safe_sql("SELECT pg_current_wal_lsn()")
            checksums.disable_data_checksums(node, wait=1)
            lsn_post = node.safe_sql("SELECT pg_current_wal_lsn()")
            data_checksum_state = "off"

        return lsn_pre, lsn_post

    # Start a primary node with WAL archiving enabled and with enough
    # connections available to handle the workload.
    node_primary = create_pg(
        "pitr_main",
        start=False,
        initdb_extra=["--no-data-checksums"],
        has_archiving=True,
        allows_streaming=True,
    )
    node_primary.append_conf(
        "\n".join(
            [
                "max_connections = 100",
                "log_statement = none",
            ]
        )
    )
    node_primary.start()

    # Prime the cluster with a bit of known data which we can read back to
    # check for data consistency as well as page verification faults in the
    # logfile.
    node_primary.safe_sql("CREATE TABLE t AS SELECT generate_series(1, 100000) AS a;")

    # Take a backup to use for PITR.
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    _, post_lsn = flip_data_checksums(node_primary)

    node_primary.safe_sql("UPDATE t SET a = a + 1;")
    node_primary.safe_sql("SELECT pg_create_restore_point('a');")
    node_primary.safe_sql("UPDATE t SET a = a + 1;")
    node_primary.stop("fast")

    # Recover from the backup up to the LSN recorded just after the checksum
    # flip, so the recovered cluster should have checksums enabled but should
    # not include the post-flip updates.
    node_pitr = create_pg("pitr_backup", start=False)
    node_pitr.init_from_backup(
        node_primary, backup_name, standby=False, has_restoring=True
    )
    node_pitr.append_conf(
        "\n".join(
            [
                f"recovery_target_lsn = '{post_lsn}'",
                "recovery_target_action = 'promote'",
                "recovery_target_inclusive = on",
            ]
        )
    )

    node_pitr.start()

    assert node_pitr.poll_query_until(
        "SELECT pg_is_in_recovery() = 'f';"
    ), "Timed out while waiting for PITR promotion"

    checksums.test_checksum_state(node_pitr, data_checksum_state)
    result = node_pitr.safe_sql("SELECT count(*) FROM t WHERE a > 1")
    assert result == "99999", "ensure data pages can be read back on primary"

    node_pitr.stop()

    log = node_pitr.log_content()
    assert not re.search(
        r"page verification failed,.+\d$", log, re.MULTILINE
    ), "no checksum validation errors in pitr log"
