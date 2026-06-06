# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_amcheck option handling and argument validation."""


def test_001_basic(pg_bin):
    """pg_amcheck --help / --version / invalid-option handling."""
    pg_bin.program_help_ok("pg_amcheck")
    pg_bin.program_version_ok("pg_amcheck")
    pg_bin.program_options_handling_ok("pg_amcheck")
