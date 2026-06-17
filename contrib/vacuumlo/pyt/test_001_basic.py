# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic sanity checks for the vacuumlo command-line program."""


def test_vacuumlo_basic(pg_bin):
    pg_bin.program_help_ok("vacuumlo")
    pg_bin.program_version_ok("vacuumlo")
    pg_bin.program_options_handling_ok("vacuumlo")
