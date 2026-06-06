# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_controldata output and its handling of a corrupted pg_control."""

import os
import re


def test_pg_controldata(pg_bin, create_pg):
    pg_bin.program_help_ok("pg_controldata")
    pg_bin.program_version_ok("pg_controldata")
    pg_bin.program_options_handling_ok("pg_controldata")
    pg_bin.command_fails(["pg_controldata"], "pg_controldata without arguments fails")
    pg_bin.command_fails(
        ["pg_controldata", "nonexistent"],
        "pg_controldata with nonexistent directory fails",
    )

    node = create_pg("main", start=False)

    pg_bin.command_like(
        ["pg_controldata", node.data_dir],
        re.compile(r"checkpoint"),
        "pg_controldata produces output",
    )

    # Corrupt pg_control by overwriting everything after the first 16 bytes
    # (the pg_control version number) with zeros, so we get a checksum
    # mismatch rather than a version-number error.
    pg_control = os.path.join(node.data_dir, "global", "pg_control")
    size = os.path.getsize(pg_control)
    with open(pg_control, "r+b") as fh:
        fh.seek(16)
        fh.write(b"\x00" * (size - 16))

    pg_bin.command_checks_all(
        ["pg_controldata", node.data_dir],
        0,
        [re.compile(r".")],
        [
            re.compile(
                r"warning: calculated CRC checksum does not match value stored in control file"
            ),
            re.compile(r"warning: invalid WAL segment size"),
        ],
        "pg_controldata with corrupted pg_control",
    )
