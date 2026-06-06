# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_isready against stopped and running servers."""

from pypg.util import TIMEOUT_DEFAULT


def test_pg_isready(pg_bin, create_pg):
    """pg_isready fails with no server, then succeeds once it is up."""
    pg_bin.program_help_ok("pg_isready")
    pg_bin.program_version_ok("pg_isready")
    pg_bin.program_options_handling_ok("pg_isready")

    node = create_pg("main", start=False)

    node.command_fails(["pg_isready"], "fails with no server running")

    node.start()

    node.command_ok(
        ["pg_isready", "--timeout", TIMEOUT_DEFAULT],
        "succeeds with server running",
    )
