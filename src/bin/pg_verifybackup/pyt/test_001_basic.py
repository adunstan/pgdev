# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_verifybackup option handling and output."""

import os


def test_001_basic(pg_bin, tmp_path):
    pg_bin.program_help_ok("pg_verifybackup")
    pg_bin.program_version_ok("pg_verifybackup")
    pg_bin.program_options_handling_ok("pg_verifybackup")

    tempdir = str(tmp_path / "tempdir")
    os.mkdir(tempdir)

    pg_bin.command_fails_like(
        ["pg_verifybackup"],
        r"no backup directory specified",
        "target directory must be specified")
    pg_bin.command_fails_like(
        ["pg_verifybackup", tempdir],
        r'could not open file.*/backup_manifest"',
        "pg_verifybackup requires a manifest")
    pg_bin.command_fails_like(
        ["pg_verifybackup", tempdir, tempdir],
        r"too many command-line arguments",
        "multiple target directories not allowed")

    # create fake manifest file
    with open(os.path.join(tempdir, "backup_manifest"), "w"):
        pass

    # but then try to use an alternate, nonexisting manifest
    pg_bin.command_fails_like(
        [
            "pg_verifybackup",
            "--manifest-path", os.path.join(tempdir, "not_the_manifest"),
            tempdir,
        ],
        r'could not open file.*/not_the_manifest"',
        "pg_verifybackup respects -m flag")
