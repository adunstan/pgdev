# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test manipulations of replication slots with the single-user mode."""

import os
import subprocess
import sys

import pytest

SLOT_LOGICAL = "slot_logical"
SLOT_PHYSICAL = "slot_physical"


def run_single_mode(bindir, node, queries):
    """Run a set of queries in single-user mode and return success (exit 0).

    Runs ``postgres --single -F -c exit_on_error=true -D <datadir> postgres``
    with *queries* fed on stdin.
    """
    postgres = os.path.join(bindir, "postgres")
    if not os.path.exists(postgres):
        postgres = "postgres"
    argv = [
        postgres,
        "--single",
        "-F",
        "-c",
        "exit_on_error=true",
        "-D",
        node.data_dir,
        "postgres",
    ]
    print("# Running: " + " ".join(argv))
    proc = subprocess.run(
        argv,
        input=queries,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.stdout:
        print(proc.stdout)
    return proc.returncode == 0


def test_008_replslot_single_user(create_pg, bindir):
    # Skip on Windows, as single-user mode would fail on permission
    # failure with privileged accounts.
    if sys.platform == "win32":
        pytest.skip("this test is not supported by this platform")

    # Initialize a node
    node = create_pg("node", start=True, allows_streaming="logical")

    # Define initial table
    node.safe_sql("CREATE TABLE foo (id int)")

    node.stop()

    assert run_single_mode(
        bindir,
        node,
        f"SELECT pg_create_logical_replication_slot('{SLOT_LOGICAL}', 'test_decoding')",
    ), "logical slot creation"

    assert run_single_mode(
        bindir,
        node,
        f"SELECT pg_create_physical_replication_slot('{SLOT_PHYSICAL}', true)",
    ), "physical slot creation"

    assert run_single_mode(
        bindir,
        node,
        "SELECT pg_create_physical_replication_slot('slot_tmp', true, true)",
    ), "temporary physical slot creation"

    assert run_single_mode(
        bindir,
        node,
        f"""
INSERT INTO foo VALUES (1);
SELECT pg_logical_slot_get_changes('{SLOT_LOGICAL}', NULL, NULL);
""",
    ), "logical decoding"

    assert run_single_mode(
        bindir,
        node,
        f"SELECT pg_replication_slot_advance('{SLOT_LOGICAL}', pg_current_wal_lsn())",
    ), "logical slot advance"

    assert run_single_mode(
        bindir,
        node,
        f"SELECT pg_replication_slot_advance('{SLOT_PHYSICAL}', pg_current_wal_lsn())",
    ), "physical slot advance"

    assert run_single_mode(
        bindir,
        node,
        f"SELECT pg_copy_logical_replication_slot('{SLOT_LOGICAL}', 'slot_log_copy')",
    ), "logical slot copy"

    assert run_single_mode(
        bindir,
        node,
        f"SELECT pg_copy_physical_replication_slot('{SLOT_PHYSICAL}', 'slot_phy_copy')",
    ), "physical slot copy"

    assert run_single_mode(
        bindir,
        node,
        f"SELECT pg_drop_replication_slot('{SLOT_LOGICAL}')",
    ), "logical slot drop"

    assert run_single_mode(
        bindir,
        node,
        f"SELECT pg_drop_replication_slot('{SLOT_PHYSICAL}')",
    ), "physical slot drop"
