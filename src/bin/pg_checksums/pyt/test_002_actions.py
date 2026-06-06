# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_checksums enable/disable/verify actions and corruption detection."""

import os
import re
import sys

from pypg.util import append_to_file


def _check_relation_corruption(node, pg_bin, table, tablespace):
    """Create and check a table with corrupted checksums on a tablespace.

    Stops and starts the node multiple times, leaving it started at the end.
    """
    pgdata = node.data_dir

    # Create table and discover its filesystem location.
    node.safe_sql(
        f"CREATE TABLE {table} AS SELECT a FROM generate_series(1,10000) AS a;"
        f"ALTER TABLE {table} SET (autovacuum_enabled=false);"
    )
    node.safe_sql(f"ALTER TABLE {table} SET TABLESPACE {tablespace};")

    file_corrupted = node.safe_sql(f"SELECT pg_relation_filepath('{table}');")
    relfilenode_corrupted = node.safe_sql(
        f"SELECT relfilenode FROM pg_class WHERE relname = '{table}';"
    )

    node.stop()

    # Checksums are correct for single relfilenode as the table is not
    # corrupted yet.
    pg_bin.command_ok(
        [
            "pg_checksums",
            "--check",
            "--pgdata", pgdata,
            "--filenode", relfilenode_corrupted,
        ],
        f"succeeds for single relfilenode on tablespace {tablespace} with offline cluster",
    )

    # Time to create some corruption
    node.corrupt_page_checksum(file_corrupted, 0)

    # Checksum checks on single relfilenode fail
    pg_bin.command_checks_all(
        [
            "pg_checksums",
            "--check",
            "--pgdata", pgdata,
            "--filenode", relfilenode_corrupted,
        ],
        1,
        [re.compile(r"Bad checksums:.*1")],
        [re.compile(r"checksum verification failed")],
        f"fails with corrupted data for single relfilenode on tablespace {tablespace}",
    )

    # Global checksum checks fail as well
    pg_bin.command_checks_all(
        ["pg_checksums", "--check", "--pgdata", pgdata],
        1,
        [re.compile(r"Bad checksums:.*1")],
        [re.compile(r"checksum verification failed")],
        f"fails with corrupted data on tablespace {tablespace}",
    )

    # Drop corrupted table again and make sure there is no more corruption.
    node.start()
    node.safe_sql(f"DROP TABLE {table};")
    node.stop()
    pg_bin.command_ok(
        ["pg_checksums", "--check", "--pgdata", pgdata],
        f"succeeds again after table drop on tablespace {tablespace}",
    )

    node.start()


def _fail_corrupt(node, pg_bin, file):
    """Check that pg_checksums detects a correctly-named file with bad data."""
    pgdata = node.data_dir
    file_name = os.path.join(pgdata, "global", file)
    append_to_file(file_name, "foo")

    pg_bin.command_checks_all(
        ["pg_checksums", "--check", "--pgdata", pgdata],
        1,
        [re.compile(r"^$")],
        [re.compile(r"could not read block 0 in file.*" + re.escape(file) + r"\":")],
        f"fails for corrupted data in {file}",
    )

    os.unlink(file_name)


def test_pg_checksums_actions(create_pg, pg_bin):
    """Basic sanity checks supported by pg_checksums on an initialized cluster."""
    # initdb with checksums disabled.
    node = create_pg("main", start=False, initdb_extra=["--no-data-checksums"])
    pgdata = node.data_dir

    # Control file should know that checksums are disabled.
    pg_bin.command_like(
        ["pg_controldata", pgdata],
        re.compile(r"Data page checksum version:.*0"),
        "checksums disabled in control file",
    )

    # These are correct but empty files, so they should pass through.
    for empty in (
        "99999",
        "99999.123",
        "99999_fsm",
        "99999_init",
        "99999_vm",
        "99999_init.123",
        "99999_fsm.123",
        "99999_vm.123",
    ):
        append_to_file(os.path.join(pgdata, "global", empty), "")

    # These are temporary files and folders with dummy contents, which
    # should be ignored by the scan.
    append_to_file(os.path.join(pgdata, "global", "pgsql_tmp_123"), "foo")
    os.mkdir(os.path.join(pgdata, "global", "pgsql_tmp"))
    append_to_file(os.path.join(pgdata, "global", "pgsql_tmp", "1.1"), "foo")
    append_to_file(os.path.join(pgdata, "global", "pg_internal.init"), "foo")
    append_to_file(os.path.join(pgdata, "global", "pg_internal.init.123"), "foo")

    # These are non-postgres macOS files, which should be ignored by the scan.
    # Only perform this test on non-macOS systems though as creating incorrect
    # system files may have side effects on macOS.
    if sys.platform != "darwin":
        append_to_file(os.path.join(pgdata, "global", ".DS_Store"), "foo")

    # Enable checksums.
    pg_bin.command_ok(
        ["pg_checksums", "--enable", "--no-sync", "--pgdata", pgdata],
        "checksums successfully enabled in cluster",
    )

    # Successive attempt to enable checksums fails.
    pg_bin.command_fails(
        ["pg_checksums", "--enable", "--no-sync", "--pgdata", pgdata],
        "enabling checksums fails if already enabled",
    )

    # Control file should know that checksums are enabled.
    pg_bin.command_like(
        ["pg_controldata", pgdata],
        re.compile(r"Data page checksum version:.*1"),
        "checksums enabled in control file",
    )

    # Disable checksums again.  Flush result here as that should be cheap.
    pg_bin.command_ok(
        ["pg_checksums", "--disable", "--pgdata", pgdata],
        "checksums successfully disabled in cluster",
    )

    # Successive attempt to disable checksums fails.
    pg_bin.command_fails(
        ["pg_checksums", "--disable", "--no-sync", "--pgdata", pgdata],
        "disabling checksums fails if already disabled",
    )

    # Control file should know that checksums are disabled.
    pg_bin.command_like(
        ["pg_controldata", pgdata],
        re.compile(r"Data page checksum version:.*0"),
        "checksums disabled in control file",
    )

    # Enable checksums again for follow-up tests.
    pg_bin.command_ok(
        ["pg_checksums", "--enable", "--no-sync", "--pgdata", pgdata],
        "checksums successfully enabled in cluster",
    )

    # Control file should know that checksums are enabled.
    pg_bin.command_like(
        ["pg_controldata", pgdata],
        re.compile(r"Data page checksum version:.*1"),
        "checksums enabled in control file",
    )

    # Checksums pass on a newly-created cluster
    pg_bin.command_ok(
        ["pg_checksums", "--check", "--pgdata", pgdata],
        "succeeds with offline cluster",
    )

    # Checksums are verified if no other arguments are specified
    pg_bin.command_ok(
        ["pg_checksums", "--pgdata", pgdata],
        "verifies checksums as default action",
    )

    # Specific relation files cannot be requested when action is --disable
    # or --enable.
    pg_bin.command_fails(
        ["pg_checksums", "--disable", "--filenode", "1234", "--pgdata", pgdata],
        "fails when relfilenodes are requested and action is --disable",
    )
    pg_bin.command_fails(
        ["pg_checksums", "--enable", "--filenode", "1234", "--pgdata", pgdata],
        "fails when relfilenodes are requested and action is --enable",
    )

    # Test postgres -C for an offline cluster.
    # Run-time GUCs are safe to query here.  Note that a lock file is created,
    # then removed, leading to an extra LOG entry showing in stderr.  This uses
    # log_min_messages=fatal to remove any noise.  This test uses a startup
    # wrapped with pg_ctl to allow the case where this runs under a privileged
    # account on Windows.
    pg_bin.command_checks_all(
        [
            "pg_ctl", "start",
            "--silent",
            "--pgdata", pgdata,
            "-o", "-C data_checksums -c log_min_messages=fatal",
        ],
        1,
        [re.compile(r"^on$", re.MULTILINE)],
        [re.compile(r"could not start server")],
        "data_checksums=on is reported on an offline cluster",
    )

    # Checks cannot happen with an online cluster
    node.start()
    pg_bin.command_fails(
        ["pg_checksums", "--check", "--pgdata", pgdata],
        "fails with online cluster",
    )

    # Check corruption of table on default tablespace.
    _check_relation_corruption(node, pg_bin, "corrupt1", "pg_default")

    # Create tablespace to check corruptions in a non-default tablespace.
    basedir = node.basedir
    tablespace_dir = os.path.join(basedir, "ts_corrupt_dir")
    os.mkdir(tablespace_dir)
    node.safe_sql(f"CREATE TABLESPACE ts_corrupt LOCATION '{tablespace_dir}';")
    _check_relation_corruption(node, pg_bin, "corrupt2", "ts_corrupt")

    # Stop instance for the follow-up checks.
    node.stop()

    # Create a fake tablespace location that should not be scanned
    # when verifying checksums.
    os.mkdir(os.path.join(tablespace_dir, "PG_99_999999991"))
    append_to_file(os.path.join(tablespace_dir, "PG_99_999999991", "foo"), "123")
    pg_bin.command_ok(
        ["pg_checksums", "--check", "--pgdata", pgdata],
        "succeeds with foreign tablespace",
    )

    # Authorized relation files filled with corrupted data cause the
    # checksum checks to fail.  Make sure to use file names different
    # than the previous ones.
    _fail_corrupt(node, pg_bin, "99990")
    _fail_corrupt(node, pg_bin, "99990.123")
    _fail_corrupt(node, pg_bin, "99990_fsm")
    _fail_corrupt(node, pg_bin, "99990_init")
    _fail_corrupt(node, pg_bin, "99990_vm")
    _fail_corrupt(node, pg_bin, "99990_init.123")
    _fail_corrupt(node, pg_bin, "99990_fsm.123")
    _fail_corrupt(node, pg_bin, "99990_vm.123")
