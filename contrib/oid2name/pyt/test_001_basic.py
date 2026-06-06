# Copyright (c) 2021-2026, PostgreSQL Global Development Group
"""Basic sanity checks for the oid2name command-line program."""


def test_basic(pg_bin):
    # Basic checks
    pg_bin.program_help_ok("oid2name")
    pg_bin.program_version_ok("oid2name")
    pg_bin.program_options_handling_ok("oid2name")
