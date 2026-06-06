# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_test_fsync option handling and argument validation."""

import re


def test_pg_test_fsync_basic(pg_bin):
    # Basic checks
    pg_bin.program_help_ok("pg_test_fsync")
    pg_bin.program_version_ok("pg_test_fsync")
    pg_bin.program_options_handling_ok("pg_test_fsync")

    # Invalid option combinations
    pg_bin.command_fails_like(
        ["pg_test_fsync", "--secs-per-test", "a"],
        re.escape("pg_test_fsync: error: invalid argument for option --secs-per-test"),
        "pg_test_fsync: invalid argument for option --secs-per-test",
    )
    pg_bin.command_fails_like(
        ["pg_test_fsync", "--secs-per-test", "0"],
        re.escape("pg_test_fsync: error: --secs-per-test must be in range 1..4294967295"),
        "pg_test_fsync: --secs-per-test must be in range",
    )
