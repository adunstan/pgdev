# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_ctl status exit codes against missing, stopped, and running servers."""


def test_status(pg_bin, create_pg, tmp_path):
    """pg_ctl status reports the right exit code for missing/stopped/running."""
    pg_bin.command_exit_is(
        ["pg_ctl", "status", "--pgdata", str(tmp_path / "nonexistent")],
        4,
        "pg_ctl status with nonexistent directory",
    )

    node = create_pg("main", start=False)

    node.command_exit_is(
        ["pg_ctl", "status", "--pgdata", node.data_dir],
        3,
        "pg_ctl status with server not running",
    )

    node.start()

    node.command_exit_is(
        ["pg_ctl", "status", "--pgdata", node.data_dir],
        0,
        "pg_ctl status with server running",
    )

    node.stop()
