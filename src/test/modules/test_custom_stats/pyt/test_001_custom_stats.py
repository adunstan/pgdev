# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test custom pgstats functionality.

This script includes tests for both variable and fixed-sized custom
pgstats:
- Creation, updates, and reporting.
- Persistence across restarts.
- Loss after crash recovery.
- Resets for fixed-sized stats.
"""


def _safe_sql_oneshot(node, query):
    """Run *query* on a fresh connection and close it.

    safe_sql spawns a separate psql process (and hence a
    separate backend) for every statement.  Custom stats are accumulated in
    backend-local pending memory and flushed to shared memory on backend exit,
    so each statement's effect becomes visible to subsequent connections.  The
    in-process cached Session would instead keep one long-lived backend whose
    pending stats never flush between calls, so use a short-lived connection
    here to faithfully reproduce the per-statement flush semantics.
    """
    sess = node.connect()
    try:
        sess.query_safe(query)
    finally:
        sess.close()


def test_001_custom_stats(create_pg):
    node = create_pg("main", start=False)
    node.append_conf(
        "shared_preload_libraries = 'test_custom_var_stats, test_custom_fixed_stats'"
    )
    node.start()

    _safe_sql_oneshot(node, "CREATE EXTENSION test_custom_var_stats")
    _safe_sql_oneshot(node, "CREATE EXTENSION test_custom_fixed_stats")

    # Create entries for variable-sized stats.
    _safe_sql_oneshot(
        node, "select test_custom_stats_var_create('entry1', 'Test entry 1')"
    )
    _safe_sql_oneshot(
        node, "select test_custom_stats_var_create('entry2', 'Test entry 2')"
    )
    _safe_sql_oneshot(
        node, "select test_custom_stats_var_create('entry3', 'Test entry 3')"
    )
    _safe_sql_oneshot(
        node, "select test_custom_stats_var_create('entry4', 'Test entry 4')"
    )

    # Update counters: entry1=2, entry2=3, entry3=2, entry4=3, fixed=3
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry1')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry1')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry2')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry2')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry2')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry3')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry3')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry4')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry4')")
    _safe_sql_oneshot(node, "select test_custom_stats_var_update('entry4')")
    _safe_sql_oneshot(node, "select test_custom_stats_fixed_update()")
    _safe_sql_oneshot(node, "select test_custom_stats_fixed_update()")
    _safe_sql_oneshot(node, "select test_custom_stats_fixed_update()")

    # Test data reports.
    result = node.safe_sql("select * from test_custom_stats_var_report('entry1')")
    assert result == "entry1|2|Test entry 1", "report for variable-sized data of entry1"

    result = node.safe_sql("select * from test_custom_stats_var_report('entry2')")
    assert result == "entry2|3|Test entry 2", "report for variable-sized data of entry2"

    result = node.safe_sql("select * from test_custom_stats_var_report('entry3')")
    assert result == "entry3|2|Test entry 3", "report for variable-sized data of entry3"

    result = node.safe_sql("select * from test_custom_stats_var_report('entry4')")
    assert result == "entry4|3|Test entry 4", "report for variable-sized data of entry4"

    result = node.safe_sql("select * from test_custom_stats_fixed_report()")
    assert result == "3|", "report for fixed-sized stats"

    # Test drop of variable-sized stats.
    _safe_sql_oneshot(node, "select * from test_custom_stats_var_drop('entry3')")
    result = node.safe_sql("select * from test_custom_stats_var_report('entry3')")
    assert result == "", "entry3 not found after drop"
    _safe_sql_oneshot(node, "select * from test_custom_stats_var_drop('entry4')")
    result = node.safe_sql("select * from test_custom_stats_var_report('entry4')")
    assert result == "", "entry4 not found after drop"

    # Test persistence across clean restart.
    node.stop()
    node.start()

    result = node.safe_sql("select * from test_custom_stats_var_report('entry1')")
    assert (
        result == "entry1|2|Test entry 1"
    ), "variable-sized stats persist after clean restart"

    result = node.safe_sql("select * from test_custom_stats_var_report('entry2')")
    assert (
        result == "entry2|3|Test entry 2"
    ), "variable-sized stats persist after clean restart"

    result = node.safe_sql("select * from test_custom_stats_fixed_report()")
    assert result == "3|", "fixed-sized stats persist after clean restart"

    # Test persistence after crash recovery.
    node.stop("immediate")
    node.start()

    result = node.safe_sql("select * from test_custom_stats_var_report('entry1')")
    assert result == "", "variable-sized stats of entry1 lost after crash recovery"
    result = node.safe_sql("select * from test_custom_stats_var_report('entry2')")
    assert result == "", "variable-sized stats of entry2 lost after crash recovery"

    # Crash recovery sets the reset timestamp.
    result = node.safe_sql(
        "select numcalls from test_custom_stats_fixed_report() "
        "where stats_reset is not null"
    )
    assert result == "0", "fixed-sized stats are reset after crash recovery"

    # Test reset of fixed-sized stats.
    _safe_sql_oneshot(node, "select test_custom_stats_fixed_update()")
    _safe_sql_oneshot(node, "select test_custom_stats_fixed_update()")
    _safe_sql_oneshot(node, "select test_custom_stats_fixed_update()")

    result = node.safe_sql("select numcalls from test_custom_stats_fixed_report()")
    assert result == "3", "report of fixed-sized before manual reset"

    _safe_sql_oneshot(node, "select test_custom_stats_fixed_reset()")

    result = node.safe_sql(
        "select numcalls from test_custom_stats_fixed_report() "
        "where stats_reset is not null"
    )
    assert result == "0", "report of fixed-sized after manual reset"
