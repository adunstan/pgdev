# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_walsummary option handling and argument validation."""

import re


def test_pg_walsummary_basic(pg_bin):
    """pg_walsummary --help / --version / invalid-option handling."""
    pg_bin.program_help_ok("pg_walsummary")
    pg_bin.program_version_ok("pg_walsummary")
    pg_bin.program_options_handling_ok("pg_walsummary")

    pg_bin.command_fails_like(
        ["pg_walsummary"],
        re.compile(r"no input files specified"),
        "input files must be specified",
    )
