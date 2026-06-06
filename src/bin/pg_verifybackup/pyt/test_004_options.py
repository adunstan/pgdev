# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_verifybackup command-line options."""

import os
import re
import shutil


def test_004_options(create_pg, tmp_path):
    # Start up the server and take a backup.
    primary = create_pg("primary", allows_streaming=True)
    backup_path = str(tmp_path / "test_options")
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

    # Verify that pg_verifybackup --quiet succeeds and produces no output.
    res = primary.pg_bin.result(["pg_verifybackup", "--quiet", backup_path])
    assert res.returncode == 0, "--quiet succeeds: exit code 0"
    assert res.stdout == "", "--quiet succeeds: no stdout"
    assert res.stderr == "", "--quiet succeeds: no stderr"

    # Should still work if we specify --format=plain.
    primary.command_ok(
        ["pg_verifybackup", "--format", "plain", backup_path],
        "verifies with --format=plain",
    )

    # Should not work if we specify --format=y because that's invalid.
    primary.command_fails_like(
        ["pg_verifybackup", "--format", "y", backup_path],
        r'invalid backup format "y", must be "plain" or "tar"',
        "does not verify with --format=y",
    )

    # Should produce a lengthy list of errors; we test for just one of those.
    primary.command_fails_like(
        [
            "pg_verifybackup",
            "--format",
            "tar",
            "--no-parse-wal",
            backup_path,
        ],
        r'"pg_multixact" is not a regular file',
        "does not verify with --format=tar --no-parse-wal",
    )

    # Test invalid options
    primary.command_fails_like(
        ["pg_verifybackup", "--progress", "--quiet", backup_path],
        r"cannot specify both -P/--progress and -q/--quiet",
        "cannot use --progress and --quiet at the same time",
    )

    # Corrupt the PG_VERSION file.
    version_pathname = os.path.join(backup_path, "PG_VERSION")
    with open(version_pathname, encoding="utf-8") as fh:
        version_contents = fh.read()
    with open(version_pathname, "w", encoding="utf-8") as fh:
        fh.write("q" * len(version_contents))

    # Verify that pg_verifybackup -q now fails.
    primary.command_fails_like(
        ["pg_verifybackup", "--quiet", backup_path],
        r"checksum mismatch for file \"PG_VERSION\"",
        "--quiet checksum mismatch",
    )

    # Since we didn't change the length of the file, verification should
    # succeed if we ignore checksums. Check that we get the right message, too.
    primary.command_like(
        ["pg_verifybackup", "--skip-checksums", backup_path],
        r"backup successfully verified",
        "--skip-checksums skips checksumming",
    )

    # Validation should succeed if we ignore the problem file. Also, check
    # the progress information.
    primary.command_checks_all(
        [
            "pg_verifybackup",
            "--progress",
            "--ignore",
            "PG_VERSION",
            backup_path,
        ],
        0,
        [r"backup successfully verified"],
        [r"(\d+/\d+ kB \(\d+%\) verified)+"],
        "--ignore ignores problem file",
    )

    # PG_VERSION is already corrupt; let's try also removing all of pg_xact.
    shutil.rmtree(os.path.join(backup_path, "pg_xact"))

    # We're ignoring the problem with PG_VERSION, but not the problem with
    # pg_xact, so verification should fail here.
    primary.command_fails_like(
        ["pg_verifybackup", "--ignore", "PG_VERSION", backup_path],
        r"pg_xact.*is present in the manifest but not on disk",
        "--ignore does not ignore all problems",
    )

    # If we use --ignore twice, we should be able to ignore all of the
    # problems.
    primary.command_like(
        [
            "pg_verifybackup",
            "--ignore",
            "PG_VERSION",
            "--ignore",
            "pg_xact",
            backup_path,
        ],
        r"backup successfully verified",
        "multiple --ignore options work",
    )

    # Verify that when --ignore is not used, both problems are reported.
    res = primary.pg_bin.result(["pg_verifybackup", backup_path])
    assert res.returncode != 0, "multiple problems: fails"
    assert re.search(
        r"pg_xact.*is present in the manifest but not on disk", res.stderr
    ), "multiple problems: missing files reported"
    assert re.search(
        r"checksum mismatch for file \"PG_VERSION\"", res.stderr
    ), "multiple problems: checksum mismatch reported"

    # Verify that when --exit-on-error is used, only the problem detected
    # first is reported.
    res = primary.pg_bin.result(["pg_verifybackup", "--exit-on-error", backup_path])
    assert res.returncode != 0, "--exit-on-error reports 1 error: fails"
    assert re.search(
        r"pg_xact.*is present in the manifest but not on disk", res.stderr
    ), "--exit-on-error reports 1 error: missing files reported"
    assert not re.search(
        r"checksum mismatch for file \"PG_VERSION\"", res.stderr
    ), "--exit-on-error reports 1 error: checksum mismatch not reported"

    # Test valid manifest with nonexistent backup directory.
    primary.command_fails_like(
        [
            "pg_verifybackup",
            "--manifest-path",
            os.path.join(backup_path, "backup_manifest"),
            os.path.join(backup_path, "fake"),
        ],
        r"could not open directory",
        "nonexistent backup directory",
    )
