# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test for archive recovery of WAL generated with wal_level=minimal."""

import os
import re

from pypg.util import TIMEOUT_DEFAULT, poll_until, slurp_file

REPLICA_CONFIG = """
wal_level = replica
archive_mode = on
max_wal_senders = 10
hot_standby = off
"""


def test_024_archive_recovery(create_pg):
    # Initialize and start node with wal_level = replica and WAL archiving
    # enabled.
    node = create_pg("orig", start=False, has_archiving=True, allows_streaming=True)
    node.append_conf(REPLICA_CONFIG)
    node.start()

    # Take backup
    backup_name = "my_backup"
    node.backup(backup_name)

    # Restart node with wal_level = minimal and WAL archiving disabled
    # to generate WAL with that setting. Note that such WAL has not been
    # archived yet at this moment because WAL archiving is not enabled.
    node.append_conf(
        """
wal_level = minimal
archive_mode = off
max_wal_senders = 0
"""
    )
    node.restart()

    # Restart node with wal_level = replica and WAL archiving enabled
    # to archive WAL previously generated with wal_level = minimal.
    # We ensure the WAL file containing the record indicating the change
    # of wal_level to minimal is archived by checking pg_stat_archiver.
    node.append_conf(REPLICA_CONFIG)
    node.restart()

    # Find next WAL segment to be archived
    walfile_to_be_archived = node.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn());"
    )

    # Make WAL segment eligible for archival
    node.safe_sql("SELECT pg_switch_wal()")
    archive_wait_query = (
        f"SELECT '{walfile_to_be_archived}' <= last_archived_wal "
        "FROM pg_stat_archiver;"
    )

    # Wait until the WAL segment has been archived.
    assert node.poll_query_until(
        archive_wait_query
    ), "Timed out while waiting for WAL segment to be archived"

    node.stop()

    # Initialize new node from backup, and start archive recovery. Check that
    # archive recovery fails with an error when it detects the WAL record
    # indicating the change of wal_level to minimal and node stops.
    def test_recovery_wal_level_minimal(node_name, node_text, standby_setting):
        recovery_node = create_pg(node_name, start=False)
        recovery_node.init_from_backup(
            node, backup_name, has_restoring=True, standby=standby_setting
        )

        # Start the server directly with pg_ctl (no -w wait) because this test
        # expects that the server ends with an error during recovery, so a
        # waited start would never report success.
        recovery_node.pg_bin.result(
            [
                "pg_ctl",
                "--pgdata",
                recovery_node.data_dir,
                "--log",
                recovery_node.logfile,
                "start",
            ]
        )

        # wait for postgres to terminate
        pidfile = os.path.join(recovery_node.data_dir, "postmaster.pid")
        poll_until(lambda: not os.path.isfile(pidfile), timeout=TIMEOUT_DEFAULT)

        # Confirm that the archive recovery fails with an expected error
        logfile = slurp_file(recovery_node.logfile)
        assert re.search(
            r'FATAL: .* WAL was generated with "wal_level=minimal", '
            r"cannot continue recovering",
            logfile,
        ), (
            f"{node_text} ends with an error because it finds WAL generated "
            'with "wal_level=minimal"'
        )

    # Test for archive recovery
    test_recovery_wal_level_minimal("archive_recovery", "archive recovery", False)

    # Test for standby server
    test_recovery_wal_level_minimal("standby", "standby", True)
