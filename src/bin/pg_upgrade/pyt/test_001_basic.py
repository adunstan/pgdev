# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Basic pg_upgrade command-line option handling checks."""


def test_001_basic(pg_bin):
    pg_bin.program_help_ok("pg_upgrade")
    pg_bin.program_version_ok("pg_upgrade")
    pg_bin.program_options_handling_ok("pg_upgrade")
