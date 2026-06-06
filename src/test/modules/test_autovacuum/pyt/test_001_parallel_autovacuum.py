# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test parallel autovacuum behavior."""

import os

import pytest


# Before each test we should disable autovacuum for 'test_autovac' table and
# generate some dead tuples in it.
def _prepare_for_next_test(node, test_number):
    node.safe_sql(
        f"""
        ALTER TABLE test_autovac SET (autovacuum_enabled = false);
        UPDATE test_autovac SET col_1 = {test_number};
    """
    )


def test_001_parallel_autovacuum(create_pg):
    node = create_pg("main", start=False)

    # Limit to one autovacuum worker and disable autovacuum logging globally
    # (enabled only on the test table) so that log checks below match only
    # activity on the expected table.
    node.append_conf(
        """
autovacuum_max_workers = 1
autovacuum_worker_slots = 1
autovacuum_max_parallel_workers = 2
max_worker_processes = 10
max_parallel_workers = 10
log_min_messages = debug2
autovacuum_naptime = '1s'
min_parallel_index_scan_size = 0
log_autovacuum_min_duration = -1
"""
    )
    node.start()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    injection_points_available = (
        node.safe_sql(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        != "0"
    )
    if (
        os.environ.get("enable_injection_points", "no") != "yes"
        and not injection_points_available
    ):
        pytest.skip("Injection points not supported by this build")
    if not injection_points_available:
        pytest.skip("Extension injection_points not installed")

    # Create all functions needed for testing
    node.safe_sql(
        """
        CREATE EXTENSION injection_points;
    """
    )

    indexes_num = 3
    initial_rows_num = 10_000
    autovacuum_parallel_workers = 2

    # Create table and fill it with some data
    node.safe_sql(
        f"""
        CREATE TABLE test_autovac (
            id SERIAL PRIMARY KEY,
            col_1 INTEGER,  col_2 INTEGER,  col_3 INTEGER,  col_4 INTEGER
        ) WITH (autovacuum_parallel_workers = {autovacuum_parallel_workers},
                log_autovacuum_min_duration = 0);

        INSERT INTO test_autovac
        SELECT
            g AS col1,
            g + 1 AS col2,
            g + 2 AS col3,
            g + 3 AS col4
        FROM generate_series(1, {initial_rows_num}) AS g;
    """
    )

    # Create specified number of b-tree indexes on the table
    node.safe_sql(
        f"""
        DO $$
        DECLARE
            i INTEGER;
        BEGIN
            FOR i IN 1..{indexes_num} LOOP
                EXECUTE format('CREATE INDEX idx_col_%s ON test_autovac (col_%s);', i, i);
            END LOOP;
        END $$;
    """
    )

    # Test 1 :
    # Our table has enough indexes and appropriate reloptions, so autovacuum must
    # be able to process it in parallel mode. Just check if it can do it.

    _prepare_for_next_test(node, 1)
    log_offset = node.log_position()

    node.safe_sql(
        """
        ALTER TABLE test_autovac SET (autovacuum_enabled = true);
    """
    )

    # Wait for parallel autovacuum to complete; check worker count matches reloptions.
    node.wait_for_log(
        r"parallel workers: index vacuum: 2 planned, 2 launched in total", log_offset
    )
    assert True, "parallel autovacuum on test_autovac table"

    # Test 2:
    # Check whether parallel autovacuum leader can propagate cost-based parameters
    # to the parallel workers.

    _prepare_for_next_test(node, 2)
    log_offset = node.log_position()

    node.safe_sql(
        """
        SELECT injection_points_attach('autovacuum-start-parallel-vacuum', 'wait');

        ALTER TABLE test_autovac SET (autovacuum_parallel_workers = 1, autovacuum_enabled = true);
    """
    )

    # Wait until parallel autovacuum is inited
    node.wait_for_event("autovacuum worker", "autovacuum-start-parallel-vacuum")

    # Update the shared cost-based delay parameters.  ALTER SYSTEM cannot run
    # inside a transaction block, so issue each statement separately (safe_sql
    # sends them as individual commands; libpq's multi-statement exec would wrap
    # them in one implicit transaction).
    node.safe_sql("ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 500")
    node.safe_sql("ALTER SYSTEM SET autovacuum_vacuum_cost_delay = 5")
    node.safe_sql("ALTER SYSTEM SET vacuum_cost_page_miss = 10")
    node.safe_sql("ALTER SYSTEM SET vacuum_cost_page_dirty = 10")
    node.safe_sql("ALTER SYSTEM SET vacuum_cost_page_hit = 10")
    node.safe_sql("SELECT pg_reload_conf()")

    # Resume the leader process to update the shared parameters during heap scan (i.e.
    # vacuum_delay_point() is called) and launch a parallel vacuum worker, but it stops
    # before vacuuming indexes due to the injection point.
    node.safe_sql(
        """
        SELECT injection_points_wakeup('autovacuum-start-parallel-vacuum');
    """
    )

    # Check whether parallel worker successfully updated all parameters during
    # index processing.
    node.wait_for_log(
        r"parallel autovacuum worker updated cost params: cost_limit=500, cost_delay=5, cost_page_miss=10, cost_page_dirty=10, cost_page_hit=10",
        log_offset,
    )

    # Cleanup
    node.safe_sql(
        """
        SELECT injection_points_detach('autovacuum-start-parallel-vacuum');
    """
    )

    assert (
        True
    ), "vacuum delay parameter changes are propagated to parallel vacuum workers"

    node.stop()
