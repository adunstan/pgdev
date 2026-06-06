# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_checksums option handling and argument validation."""


def test_pg_checksums_basic(pg_bin):
    """pg_checksums --help / --version / invalid-option handling."""
    pg_bin.program_help_ok("pg_checksums")
    pg_bin.program_version_ok("pg_checksums")
    pg_bin.program_options_handling_ok("pg_checksums")
