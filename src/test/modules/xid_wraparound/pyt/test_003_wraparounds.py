# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Consume a lot of XIDs, wrapping around a few times."""

import os

import pytest

if "xid_wraparound" not in os.environ.get("PG_TEST_EXTRA", ""):
    pytest.skip(
        "test xid_wraparound not enabled in PG_TEST_EXTRA",
        allow_module_level=True,
    )


def test_003_wraparounds(create_pg):
    # Initialize node
    node = create_pg("wraparound", start=False)

    node.append_conf(
        """
autovacuum_naptime = 1s
# so it's easier to verify the order of operations
autovacuum_max_workers = 1
log_autovacuum_min_duration = 0
"""
    )
    node.start()
    node.safe_sql("CREATE EXTENSION xid_wraparound")

    # Create a test table. We disable autovacuum on the table to run
    # it only to prevent wraparound.
    node.safe_sql(
        """
CREATE TABLE wraparoundtest(t text) WITH (autovacuum_enabled = off);
INSERT INTO wraparoundtest VALUES ('beginning');
"""
    )

    # Burn through 10 billion transactions in total, in batches of 100 million.
    for i in range(1, 101):
        node.safe_sql("SELECT consume_xids(100000000)")
        node.safe_sql(f"INSERT INTO wraparoundtest VALUES ('after {i} batches')")

    ret = node.safe_sql("SELECT COUNT(*) FROM wraparoundtest")
    assert ret == "101"

    node.stop()
