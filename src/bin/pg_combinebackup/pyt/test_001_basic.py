# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_combinebackup option handling."""

import os


def test_001_basic(pg_bin, tmp_path):
    tempdir = str(tmp_path / "tempdir")
    os.mkdir(tempdir)

    pg_bin.program_help_ok("pg_combinebackup")
    pg_bin.program_version_ok("pg_combinebackup")
    pg_bin.program_options_handling_ok("pg_combinebackup")

    pg_bin.command_fails_like(
        ["pg_combinebackup"],
        r"no input directories specified",
        "input directories must be specified",
    )
    pg_bin.command_fails_like(
        ["pg_combinebackup", tempdir],
        r"no output directory specified",
        "output directory must be specified",
    )
