# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic sanity checks for the postgres program."""


def test_basic(pg_bin):
    pg_bin.program_help_ok("postgres")
    pg_bin.program_version_ok("postgres")
    pg_bin.program_options_handling_ok("postgres")
