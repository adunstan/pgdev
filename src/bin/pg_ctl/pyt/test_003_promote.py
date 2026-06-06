# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_ctl promote, including failure cases and promoting a standby."""

import os


def test_003_promote(pg_bin, create_pg, tmp_path):
    pg_bin.command_fails_like(
        ["pg_ctl", "--pgdata", os.path.join(str(tmp_path), "nonexistent"), "promote"],
        r"directory .* does not exist",
        "pg_ctl promote with nonexistent directory",
    )

    node_primary = create_pg("primary", start=False, allows_streaming=True)

    node_primary.command_fails_like(
        ["pg_ctl", "--pgdata", node_primary.data_dir, "promote"],
        r"PID file .* does not exist",
        "pg_ctl promote of not running instance fails",
    )

    node_primary.start()

    node_primary.command_fails_like(
        ["pg_ctl", "--pgdata", node_primary.data_dir, "promote"],
        r"not in standby mode",
        "pg_ctl promote of primary instance fails",
    )

    node_standby = create_pg("standby", start=False)
    node_primary.backup("my_backup")
    node_standby.init_from_backup(node_primary, "my_backup", has_streaming=True)
    node_standby.start()

    assert node_standby.safe_sql("SELECT pg_is_in_recovery()") == "t", \
        "standby is in recovery"

    node_standby.command_ok(
        ["pg_ctl", "--pgdata", node_standby.data_dir, "--no-wait", "promote"],
        "pg_ctl --no-wait promote of standby runs",
    )

    assert node_standby.poll_query_until(
        "SELECT NOT pg_is_in_recovery()"
    ), "promoted standby is not in recovery"

    # same again with default wait option
    node_standby = create_pg("standby2", start=False)
    node_standby.init_from_backup(node_primary, "my_backup", has_streaming=True)
    node_standby.start()

    assert node_standby.safe_sql("SELECT pg_is_in_recovery()") == "t", \
        "standby is in recovery"

    node_standby.command_ok(
        ["pg_ctl", "--pgdata", node_standby.data_dir, "promote"],
        "pg_ctl promote of standby runs",
    )

    # no wait here

    assert node_standby.safe_sql("SELECT pg_is_in_recovery()") == "f", \
        "promoted standby is not in recovery"
