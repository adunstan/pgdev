# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_verifybackup's WAL verification."""

import os
import re


def test_007_wal(create_pg, tmp_path):
    # Start up the server and take a backup.
    primary = create_pg("primary", allows_streaming=True)

    backup_path = str(tmp_path / "test_wal")
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata",
            backup_path,
            "--no-sync",
            "--checkpoint",
            "fast",
        ],
        "base backup ok",
    )

    # Rename pg_wal.
    original_pg_wal = os.path.join(backup_path, "pg_wal")
    relocated_pg_wal = str(tmp_path / "relocated_pg_wal")
    os.rename(original_pg_wal, relocated_pg_wal)

    # WAL verification should fail.
    primary.command_fails_like(
        ["pg_verifybackup", backup_path],
        r"WAL parsing failed for timeline 1",
        "missing pg_wal causes failure",
    )

    # Should work if we skip WAL verification.
    primary.command_ok(
        ["pg_verifybackup", "--no-parse-wal", backup_path],
        "missing pg_wal OK if not verifying WAL",
    )

    # Should also work if we specify the correct WAL location.
    primary.command_ok(
        [
            "pg_verifybackup",
            "--wal-path",
            relocated_pg_wal,
            backup_path,
        ],
        "--wal-path can be used to specify WAL directory",
    )

    # Move directory back to original location.
    os.rename(relocated_pg_wal, original_pg_wal)

    # Get a list of files in that directory that look like WAL files.
    walfiles = sorted(
        f for f in os.listdir(original_pg_wal) if re.fullmatch(r"[0-9A-F]{24}", f)
    )

    # Replace the contents of one of the files with garbage of equal length.
    wal_corruption_target = os.path.join(original_pg_wal, walfiles[0])
    wal_size = os.path.getsize(wal_corruption_target)
    with open(wal_corruption_target, "wb") as fh:
        fh.write(b"w" * wal_size)

    # WAL verification should fail.
    primary.command_fails_like(
        ["pg_verifybackup", backup_path],
        r"WAL parsing failed for timeline 1",
        "corrupt WAL file causes failure",
    )

    # Check that WAL-Ranges has correct values with a history file and
    # a timeline > 1.  Rather than plugging in a new standby, do a
    # self-promotion of this node.
    primary.stop()
    primary.append_conf("", filename="standby.signal")
    primary.start()
    primary.promote()
    primary.safe_sql("SELECT pg_switch_wal()")
    backup_path2 = str(tmp_path / "test_tli")
    # The base backup run below does a checkpoint, that removes the first
    # segment of the current timeline.
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata",
            backup_path2,
            "--no-sync",
            "--checkpoint",
            "fast",
        ],
        "base backup 2 ok",
    )
    primary.command_ok(
        ["pg_verifybackup", backup_path2], "valid base backup with timeline > 1"
    )

    # Test WAL verification for a tar-format backup with a separate pg_wal.tar,
    # as produced by pg_basebackup --format=tar --wal-method=stream.
    backup_path3 = str(tmp_path / "test_tar_wal")
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata",
            backup_path3,
            "--no-sync",
            "--format",
            "tar",
            "--checkpoint",
            "fast",
        ],
        "tar backup with separate pg_wal.tar",
    )
    primary.command_ok(
        ["pg_verifybackup", backup_path3],
        "WAL verification succeeds with separate pg_wal.tar",
    )
