# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_config option handling and its single/multiple-option output."""

import re


def test_pg_config(pg_bin):
    pg_bin.program_help_ok("pg_config")
    pg_bin.program_version_ok("pg_config")
    pg_bin.program_options_handling_ok("pg_config")

    pg_bin.command_like(
        ["pg_config", "--bindir"], re.compile(r"bin"), "pg_config single option"
    )
    pg_bin.command_like(
        ["pg_config", "--bindir", "--libdir"],
        re.compile(r"bin.*\n.*lib"),
        "pg_config two options",
    )
    pg_bin.command_like(
        ["pg_config", "--libdir", "--bindir"],
        re.compile(r"lib.*\n.*bin"),
        "pg_config two options different order",
    )
    pg_bin.command_like(
        ["pg_config"],
        re.compile(r".*\n.*\n.*"),
        "pg_config without options prints many lines",
    )
