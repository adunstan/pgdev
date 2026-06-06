# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test the basebackup_to_shell module: streaming a base backup to a shell command."""

import os
import subprocess

import pytest

# For nearly all pg_basebackup invocations some options should be specified,
# to keep test times reasonable.  Used as the leading elements of the argument
# list passed to the node command_* helpers.
PG_BASEBACKUP_DEFS = ["pg_basebackup", "--no-sync", "--checkpoint", "fast"]


def test_001_basic(create_pg, tmp_path):
    # For testing purposes, we just want basebackup_to_shell to write standard
    # input to a file.  We use gzip for this, and skip when gzip is not
    # available.
    gzip = os.environ.get("GZIP_PROGRAM")
    if not gzip:
        pytest.skip("gzip not available")
    # The command is stored in postgresql.conf and run by the server; use
    # forward slashes so backslashes in the Windows path are not mangled.
    gzip = gzip.replace("\\", "/")

    # allows_streaming=True sets up postgresql.conf for replication.  The
    # cluster uses trust auth, so backupuser can connect without extra
    # pg_hba setup.
    node = create_pg("primary", start=False, allows_streaming=True)

    node.append_conf("shared_preload_libraries = 'basebackup_to_shell'")
    node.start()
    node.safe_sql("CREATE USER backupuser REPLICATION")
    node.safe_sql("CREATE ROLE trustworthy")

    # This particular test module generally wants to run with --wal-method
    # fetch, because stream is not supported with a backup target, and with
    # -U backupuser.
    pg_basebackup_cmd = PG_BASEBACKUP_DEFS + [
        "--username",
        "backupuser",
        "--wal-method",
        "fetch",
    ]

    # Can't use this module without setting basebackup_to_shell.command.
    node.command_fails_like(
        pg_basebackup_cmd + ["--target", "shell"],
        r"shell command for backup is not configured",
        "fails if basebackup_to_shell.command is not set",
    )

    # Configure basebackup_to_shell.command and reload the configuration file.
    backup_path = str(tmp_path / "backup")
    os.mkdir(backup_path)
    backup_path_fwd = backup_path.replace("\\", "/")
    shell_command = f'"{gzip}" --fast > "{backup_path_fwd}/%f.gz"'
    node.append_conf(f"basebackup_to_shell.command='{shell_command}'")
    node.reload()

    # Should work now.
    node.command_ok(
        pg_basebackup_cmd + ["--target", "shell"],
        "backup with no detail: pg_basebackup",
    )
    _verify_backup(node, gzip, "", backup_path, tmp_path, "backup with no detail")

    # Should fail with a detail.
    node.command_fails_like(
        pg_basebackup_cmd + ["--target", "shell:foo"],
        r"a target detail is not permitted because the configured command "
        r"does not include %d",
        "fails if detail provided without %d",
    )

    # Reconfigure to restrict access and require a detail.
    shell_command = f'"{gzip}" --fast > "{backup_path_fwd}/%d.%f.gz"'
    node.append_conf(f"basebackup_to_shell.command='{shell_command}'")
    node.append_conf("basebackup_to_shell.required_role='trustworthy'")
    node.reload()

    # Should fail due to lack of permission.
    node.command_fails_like(
        pg_basebackup_cmd + ["--target", "shell"],
        r"permission denied to use basebackup_to_shell",
        "fails if required_role not granted",
    )

    # Should fail due to lack of a detail.
    node.safe_sql("GRANT trustworthy TO backupuser")
    node.command_fails_like(
        pg_basebackup_cmd + ["--target", "shell"],
        "a target detail is required because the configured command includes %d",
        "fails if %d is present and detail not given",
    )

    # Should work.
    node.command_ok(
        pg_basebackup_cmd + ["--target", "shell:bar"],
        "backup with detail: pg_basebackup",
    )
    _verify_backup(node, gzip, "bar.", backup_path, tmp_path, "backup with detail")


def _verify_backup(node, gzip, prefix, backup_dir, tmp_path, test_name):
    """Verify that a gzipped base backup and manifest were created and are valid."""
    assert os.path.isfile(
        os.path.join(backup_dir, f"{prefix}backup_manifest.gz")
    ), f"{test_name}: backup_manifest.gz was created"
    assert os.path.isfile(
        os.path.join(backup_dir, f"{prefix}base.tar.gz")
    ), f"{test_name}: base.tar.gz was created"

    tar = os.environ.get("TAR")
    if not tar:
        print("# no tar program available")
        return

    # Decompress.
    subprocess.run(
        [gzip, "-d", os.path.join(backup_dir, f"{prefix}backup_manifest.gz")],
        check=True,
    )
    subprocess.run(
        [gzip, "-d", os.path.join(backup_dir, f"{prefix}base.tar.gz")],
        check=True,
    )

    # Untar.
    extract_path = str(tmp_path / f"extract_{prefix or 'nodetail'}")
    os.mkdir(extract_path)
    subprocess.run(
        [tar, "xf", os.path.join(backup_dir, f"{prefix}base.tar"), "-C", extract_path],
        check=True,
    )

    # Verify.
    node.command_ok(
        [
            "pg_verifybackup",
            "--no-parse-wal",
            "--manifest-path",
            os.path.join(backup_dir, f"{prefix}backup_manifest"),
            "--exit-on-error",
            extract_path,
        ],
        f"{test_name}: backup verifies ok",
    )
