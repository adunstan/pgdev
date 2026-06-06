# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_recvlogical, the command-line client for logical decoding."""

import os
import re
import signal
import subprocess

from pypg.util import poll_until, slurp_file


def _wait_for_file(path, pattern, offset=0):
    """Wait until *pattern* matches the contents of *path* at/after *offset*.

    Returns the file length when matched; raises on timeout.
    """
    regex = re.compile(pattern)

    def _found():
        if not os.path.exists(path):
            return False
        return regex.search(slurp_file(path, offset)) is not None

    if not poll_until(_found):
        raise TimeoutError(f"timed out waiting for {pattern!r} in {path}")
    return os.path.getsize(path)


def _chmod_recursive(directory, dir_mode, file_mode):
    """Recursively chmod a tree: *dir_mode* for dirs, *file_mode* for files."""
    os.chmod(directory, dir_mode)
    for root, dirs, files in os.walk(directory):
        for name in dirs:
            os.chmod(os.path.join(root, name), dir_mode)
        for name in files:
            os.chmod(os.path.join(root, name), file_mode)


def _slot_restart_lsn(node, slot_name):
    return node.safe_sql(
        "SELECT restart_lsn FROM pg_replication_slots "
        f"WHERE slot_name = '{slot_name}'"
    )


def test_030_pg_recvlogical(create_pg, pg_bin):
    pg_bin.program_help_ok("pg_recvlogical")
    pg_bin.program_version_ok("pg_recvlogical")
    pg_bin.program_options_handling_ok("pg_recvlogical")

    # Initialize node without replication settings
    node = create_pg("main", start=False, allows_streaming=True, has_archiving=True)
    node.append_conf(
        "\n".join(
            [
                "wal_level = 'logical'",
                "max_replication_slots = 4",
                "max_wal_senders = 4",
                "log_min_messages = 'debug1'",
                "log_error_verbosity = verbose",
                "max_prepared_transactions = 10",
            ]
        )
    )
    node.start()

    connstr = node.connstr("postgres")

    node.command_fails(["pg_recvlogical"], "pg_recvlogical needs a slot name")
    node.command_fails(
        ["pg_recvlogical", "--slot", "test"], "pg_recvlogical needs a database"
    )
    node.command_fails(
        ["pg_recvlogical", "--slot", "test", "--dbname", "postgres"],
        "pg_recvlogical needs an action",
    )
    node.command_fails(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--dbname", connstr,
            "--start",
        ],
        "no destination file",
    )

    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--dbname", connstr,
            "--create-slot",
        ],
        "slot created",
    )

    assert _slot_restart_lsn(node, "test") != "", "restart lsn is defined for new slot"

    node.safe_sql("CREATE TABLE test_table(x integer)")
    node.safe_sql(
        "INSERT INTO test_table(x) SELECT y FROM generate_series(1, 10) a(y);"
    )
    nextlsn = node.safe_sql("SELECT pg_current_wal_insert_lsn()").strip()

    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--dbname", connstr,
            "--start",
            "--endpos", nextlsn,
            "--no-loop",
            "--file", "-",
        ],
        "replayed a transaction",
    )

    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--dbname", connstr,
            "--drop-slot",
        ],
        "slot dropped",
    )

    # test with two-phase option enabled
    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--dbname", connstr,
            "--create-slot",
            "--two-phase",
        ],
        "slot with two-phase created",
    )

    assert _slot_restart_lsn(node, "test") != "", "restart lsn is defined for new slot"

    node.safe_sql(
        "BEGIN; INSERT INTO test_table values (11); PREPARE TRANSACTION 'test'"
    )
    node.safe_sql("COMMIT PREPARED 'test'")
    nextlsn = node.safe_sql("SELECT pg_current_wal_insert_lsn()").strip()

    node.command_fails(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--dbname", connstr,
            "--start",
            "--endpos", nextlsn,
            "--enable-two-phase", "--no-loop",
            "--file", "-",
        ],
        "incorrect usage",
    )

    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--dbname", connstr,
            "--start",
            "--endpos", nextlsn,
            "--no-loop",
            "--file", "-",
        ],
        "replayed a two-phase transaction",
    )

    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--drop-slot",
        ],
        "drop could work without dbname",
    )

    # test with failover option enabled
    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "test",
            "--dbname", connstr,
            "--create-slot",
            "--enable-failover",
        ],
        "slot with failover created",
    )

    result = node.safe_sql(
        "SELECT failover FROM pg_catalog.pg_replication_slots "
        "WHERE slot_name = 'test'"
    )
    assert result == "t", "failover is enabled for the new slot"

    # Test that when pg_recvlogical reconnects, it does not write duplicate
    # records to the output file
    outfile = os.path.join(node.basedir, "reconnect.out")

    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "reconnect_test",
            "--dbname", connstr,
            "--create-slot",
        ],
        "slot created for reconnection test",
    )

    # Insert the first record for this test
    node.safe_sql("INSERT INTO test_table VALUES (1)")

    pg_recvlogical_cmd = [
        os.path.join(node.bindir, "pg_recvlogical"),
        "--slot", "reconnect_test",
        "--dbname", connstr,
        "--start",
        "--file", outfile,
        "--fsync-interval", "1",
        "--status-interval", "100",
        "--verbose",
    ]

    # This test targets non-Windows platforms only, using signals to terminate
    # pg_recvlogical.
    recv = subprocess.Popen(
        pg_recvlogical_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        # Wait for pg_recvlogical to receive and write the first INSERT
        first_ins = _wait_for_file(outfile, r"INSERT")

        # Terminate the walsender to force pg_recvlogical to reconnect
        backend_pid = node.safe_sql(
            "SELECT active_pid FROM pg_replication_slots "
            "WHERE slot_name = 'reconnect_test'"
        )
        node.safe_sql(f"SELECT pg_terminate_backend({backend_pid})")

        # Wait for pg_recvlogical to reconnect
        assert node.poll_query_until(
            "SELECT active_pid IS NOT NULL AND active_pid != "
            f"{backend_pid} FROM pg_replication_slots "
            "WHERE slot_name = 'reconnect_test'"
        ), "Timed out while waiting for pg_recvlogical to reconnect"

        # Insert the second record for this test
        node.safe_sql("INSERT INTO test_table VALUES (2)")

        # Wait for pg_recvlogical to receive and write the second INSERT
        _wait_for_file(outfile, r"INSERT", first_ins)

        # Terminate pg_recvlogical by sending a TERM signal.
        recv.send_signal(signal.SIGTERM)
    finally:
        recv.wait()

    outfiledata = slurp_file(outfile)
    count = len(re.findall(r"INSERT", outfiledata))
    assert count == 2, "pg_recvlogical has received and written two INSERTs"

    # Check that pg_recvlogical derives output file permissions from the source
    # cluster.  (unix-style permissions; Windows is out of scope for this port.)

    # The cluster was initialized without group access, so pg_recvlogical
    # should create the output file as 0600 (-rw-------).
    mode = oct(os.stat(outfile).st_mode & 0o7777)
    assert mode == oct(0o600), (
        "pg_recvlogical output file has no group permissions (0600)"
    )

    # Enable group access on the source cluster and its files, then restart
    # so pg_recvlogical observes the updated source cluster permissions.
    node.stop()
    _chmod_recursive(node.data_dir, 0o750, 0o640)
    node.start()

    outfile = os.path.join(node.basedir, "group_access.out")
    pg_recvlogical_cmd = [
        os.path.join(node.bindir, "pg_recvlogical"),
        "--slot", "reconnect_test",
        "--dbname", connstr,
        "--start",
        "--file", outfile,
        "--fsync-interval", "1",
    ]

    recv = subprocess.Popen(
        pg_recvlogical_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        node.safe_sql("INSERT INTO test_table VALUES (3)")
        _wait_for_file(outfile, r"INSERT")
        recv.send_signal(signal.SIGTERM)
    finally:
        recv.wait()

    # With group access enabled on the source cluster, pg_recvlogical should
    # create the output file as 0640 (-rw-r-----).
    mode = oct(os.stat(outfile).st_mode & 0o7777)
    assert mode == oct(0o640), (
        "pg_recvlogical output file respects group permissions (0640)"
    )

    node.command_ok(
        [
            "pg_recvlogical",
            "--slot", "reconnect_test",
            "--dbname", connstr,
            "--drop-slot",
        ],
        "reconnect_test slot dropped",
    )
