# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_test_timing option handling and argument validation."""

import re


def test_pg_test_timing_basic(pg_bin):
    # Basic checks
    pg_bin.program_help_ok("pg_test_timing")
    pg_bin.program_version_ok("pg_test_timing")
    pg_bin.program_options_handling_ok("pg_test_timing")

    # Invalid option combinations
    pg_bin.command_fails_like(
        ["pg_test_timing", "--duration", "a"],
        re.escape("pg_test_timing: invalid argument for option --duration"),
        "pg_test_timing: invalid argument for option --duration",
    )
    pg_bin.command_fails_like(
        ["pg_test_timing", "--duration", "0"],
        re.escape("pg_test_timing: --duration must be in range 1..4294967295"),
        "pg_test_timing: --duration must be in range",
    )
    pg_bin.command_fails_like(
        ["pg_test_timing", "--cutoff", "101"],
        re.escape("pg_test_timing: --cutoff must be in range 0..100"),
        "pg_test_timing: --cutoff must be in range",
    )

    # We can't check for specific output, but a short run should produce the
    # expected section headings.  Note the output in the log for the record.
    pattern = re.compile(
        re.escape("Testing timing overhead for 1 second.")
        + r".*"
        + re.escape("Histogram of timing durations:")
        + r".*"
        + re.escape("Observed timing durations up to 99.9900%:"),
        re.DOTALL,
    )
    res = pg_bin.command_like(
        ["pg_test_timing", "--duration", "1"],
        pattern,
        "pg_test_timing: stdout passes sanity check",
    )
    print(f"# pg_test_timing results:\n{res.stdout}")
