# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_resetwal handling of a corrupted pg_control file."""

import os
import re


def test_pg_resetwal_corrupted(pg_bin, create_pg):
    """Tests for handling a corrupted pg_control."""
    node = create_pg("main", start=False)

    pg_control = os.path.join(node.data_dir, "global", "pg_control")
    size = os.path.getsize(pg_control)

    # Read out the head of the file to get PG_CONTROL_VERSION in particular.
    with open(pg_control, "rb") as fh:
        data = fh.read(16)
    assert len(data) == 16

    # Fill pg_control with zeros
    with open(pg_control, "wb") as fh:
        fh.write(b"\x00" * size)

    pg_bin.command_checks_all(
        ["pg_resetwal", "--dry-run", node.data_dir],
        0,
        [re.compile(r"pg_control version number")],
        [
            re.compile(
                r"pg_resetwal: warning: pg_control exists but is broken or "
                r"wrong version; ignoring it"
            )
        ],
        "processes corrupted pg_control all zeroes",
    )

    # Put in the previously saved header data.  This uses a different code
    # path internally, allowing us to process a zero WAL segment size.
    with open(pg_control, "wb") as fh:
        fh.write(data + b"\x00" * (size - 16))

    pg_bin.command_checks_all(
        ["pg_resetwal", "--dry-run", node.data_dir],
        0,
        [re.compile(r"pg_control version number")],
        [
            re.compile(
                re.escape(
                    "pg_resetwal: warning: pg_control specifies invalid WAL "
                    "segment size (0 bytes); proceed with caution"
                )
            )
        ],
        "processes zero WAL segment size",
    )

    # now try to run it
    pg_bin.command_fails_like(
        ["pg_resetwal", node.data_dir],
        re.compile(r"not proceeding because control file values were guessed"),
        "does not run when control file values were guessed",
    )
    pg_bin.command_ok(
        ["pg_resetwal", "--force", node.data_dir],
        "runs with force when control file values were guessed",
    )
