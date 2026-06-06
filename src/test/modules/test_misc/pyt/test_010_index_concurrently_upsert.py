# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test INSERT ON CONFLICT DO UPDATE concurrent with CREATE/REINDEX CONCURRENTLY.

These tests verify the fix for "duplicate key value violates unique
constraint" errors that occurred when infer_arbiter_indexes() only considered
indisvalid indexes, causing different transactions to use different arbiter
indexes.
"""

import time

import pytest

from libpq.constants import CONNECTION_OK

# Default timeout (seconds) for waiting on background operations.
TIMEOUT_DEFAULT = 180


# ---------------------------------------------------------------------------
# Helper functions (named non-test_* so pytest does not collect them).
# ---------------------------------------------------------------------------


def wait_for_injection_point(node, point_name, timeout=None):
    """Wait for a session to hit an injection point.

    Optional *timeout* is in seconds.  Returns True if found, False on
    timeout.  On timeout, logs diagnostic information about all active
    queries.
    """
    if timeout is None:
        timeout = TIMEOUT_DEFAULT / 2

    for _ in range(int(timeout * 10)):
        pid = node.safe_sql(
            f"""
            SELECT pid FROM pg_stat_activity
            WHERE wait_event_type = 'InjectionPoint'
              AND wait_event = '{point_name}'
            LIMIT 1;
        """
        )
        if pid != "":
            return True
        time.sleep(0.1)

    # Timeout - report diagnostic information
    activity = node.safe_sql(
        """
        SELECT format('pid=%s, state=%s, wait_event_type=%s, wait_event=%s, backend_xmin=%s, backend_xid=%s, query=%s',
            pid, state, wait_event_type, wait_event, backend_xmin, backend_xid, left(query, 100))
        FROM pg_stat_activity
        ORDER BY pid;
    """
    )
    print(
        f"wait_for_injection_point timeout waiting for: {point_name}\n"
        f"Current queries in pg_stat_activity:\n{activity}"
    )
    return False


def ok_injection_point(node, injection_point, testname=None):
    """Assert that a wait for the given injection point succeeds."""
    if testname is None:
        testname = f"hit injection point {injection_point}"
    assert wait_for_injection_point(node, injection_point), testname


def wait_for_idle(node, pid, timeout=None):
    """Wait for a specific backend to become idle.

    Returns True if idle, False if waiting for injection point or timeout.
    """
    if timeout is None:
        timeout = TIMEOUT_DEFAULT / 2

    for _ in range(int(timeout * 10)):
        result = node.safe_sql(
            f"""
            SELECT state, wait_event_type FROM pg_stat_activity WHERE pid = {pid};
        """
        )
        parts = result.split("|", 1)
        state = parts[0] if len(parts) > 0 else ""
        wait_event_type = parts[1] if len(parts) > 1 else ""
        if state == "idle":
            return True
        if wait_event_type == "InjectionPoint":
            return False
        time.sleep(0.1)
    return False


def wakeup_injection_point(node, point_name):
    """Detach and wakeup an injection point."""
    node.safe_sql(
        f"""
SELECT injection_points_detach('{point_name}');
SELECT injection_points_wakeup('{point_name}');
"""
    )


def safe_quit(session):
    """Wait for any pending query to complete and close the session.

    Returns empty string on success, error message on failure.
    """
    # Wait for any async queries to complete
    session.wait_for_completion()

    # Check connection status
    status = session.conn_status()

    # Close the session
    session.close()

    # Return empty string if connection was OK, otherwise return error
    return "" if status == CONNECTION_OK else "connection error"


def clean_safe_quit_ok(*sessions):
    """Verify that the given sessions exit cleanly."""
    for i, session in enumerate(sessions, start=1):
        assert safe_quit(session) == "", f"session {i} quit cleanly"


# ---------------------------------------------------------------------------
# The test.
# ---------------------------------------------------------------------------


def test_010_index_concurrently_upsert(create_pg):
    # Node initialization
    node = create_pg("node")

    # Check if the extension injection_points is available
    if (
        node.safe_sql(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        == "0"
    ):
        pytest.skip("Extension injection_points not installed")

    node.safe_sql("CREATE EXTENSION injection_points;")

    node.safe_sql(
        """
CREATE SCHEMA test;
CREATE UNLOGGED TABLE test.tblpk (i int PRIMARY KEY, updated_at timestamp);
ALTER TABLE test.tblpk SET (parallel_workers=0);

CREATE TABLE test.tblparted(i int primary key, updated_at timestamp) PARTITION BY RANGE (i);
CREATE TABLE test.tbl_partition PARTITION OF test.tblparted
    FOR VALUES FROM (0) TO (10000)
    WITH (parallel_workers = 0);

CREATE UNLOGGED TABLE test.tblexpr(i int, updated_at timestamp);
CREATE UNIQUE INDEX tbl_pkey_special ON test.tblexpr(abs(i)) WHERE i < 1000;
ALTER TABLE test.tblexpr SET (parallel_workers=0);

"""
    )

    ##########################################################################
    print("# Test: REINDEX CONCURRENTLY + UPSERT (wakeup at set-dead phase)")

    # Create sessions for concurrent operations
    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    # Setup injection points for each session
    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-set-dead', 'wait');
"""
    )

    # s3 starts REINDEX (will block on reindex-relation-concurrently-before-set-dead)
    s3.do_async("REINDEX INDEX CONCURRENTLY test.tblpk_pkey;")

    # Wait for s3 to hit injection point
    ok_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    # s1 starts UPSERT (will block on check-exclusion-or-unique-constraint-no-conflict)
    s1.do_async(
        "INSERT INTO test.tblpk VALUES (13,now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    # Wait for s1 to hit injection point
    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    # Wakeup s3 to continue (reindex-relation-concurrently-before-set-dead)
    wakeup_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    # s2 starts UPSERT (will block on exec-insert-before-insert-speculative)
    s2.do_async(
        "INSERT INTO test.tblpk VALUES (13,now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    # Wait for s2 to hit injection point
    ok_injection_point(node, "exec-insert-before-insert-speculative")

    # Wakeup s1 (check-exclusion-or-unique-constraint-no-conflict)
    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    # Wakeup s2 (exec-insert-before-insert-speculative)
    wakeup_injection_point(node, "exec-insert-before-insert-speculative")

    clean_safe_quit_ok(s1, s2, s3)

    # Cleanup test 1
    node.safe_sql("TRUNCATE TABLE test.tblpk")

    ##########################################################################
    print("# Test: REINDEX CONCURRENTLY + UPSERT (wakeup at swap phase)")

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-swap', 'wait');
"""
    )

    s3.do_async("REINDEX INDEX CONCURRENTLY test.tblpk_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-swap")

    s1.do_async(
        "INSERT INTO test.tblpk VALUES (13,now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    wakeup_injection_point(node, "reindex-relation-concurrently-before-swap")

    s2.do_async(
        "INSERT INTO test.tblpk VALUES (13,now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    wakeup_injection_point(node, "exec-insert-before-insert-speculative")
    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblpk")

    ##########################################################################
    print("# Test: REINDEX CONCURRENTLY + UPSERT (s1 wakes before reindex)")

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-set-dead', 'wait');
"""
    )

    s3.do_async("REINDEX INDEX CONCURRENTLY test.tblpk_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    s1.do_async(
        "INSERT INTO test.tblpk VALUES (13,now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    # Start s2 BEFORE waking reindex (key difference from permutation 1)
    s2.do_async(
        "INSERT INTO test.tblpk VALUES (13,now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    # Wake s1 first, then reindex, then s2
    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")
    wakeup_injection_point(node, "reindex-relation-concurrently-before-set-dead")
    wakeup_injection_point(node, "exec-insert-before-insert-speculative")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblpk")

    ##########################################################################
    print("# Test: REINDEX + UPSERT ON CONSTRAINT (set-dead phase)")

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-set-dead', 'wait');
"""
    )

    s3.do_async("REINDEX INDEX CONCURRENTLY test.tblpk_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    s1.do_async(
        "INSERT INTO test.tblpk VALUES (13, now()) ON CONFLICT ON CONSTRAINT tblpk_pkey DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    wakeup_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    s2.do_async(
        "INSERT INTO test.tblpk VALUES (13, now()) ON CONFLICT ON CONSTRAINT tblpk_pkey DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")
    wakeup_injection_point(node, "exec-insert-before-insert-speculative")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblpk")

    ##########################################################################
    print("# Test: REINDEX + UPSERT ON CONSTRAINT (swap phase)")

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-swap', 'wait');
"""
    )

    s3.do_async("REINDEX INDEX CONCURRENTLY test.tblpk_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-swap")

    s1.do_async(
        "INSERT INTO test.tblpk VALUES (13, now()) ON CONFLICT ON CONSTRAINT tblpk_pkey DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    wakeup_injection_point(node, "reindex-relation-concurrently-before-swap")

    s2.do_async(
        "INSERT INTO test.tblpk VALUES (13, now()) ON CONFLICT ON CONSTRAINT tblpk_pkey DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    wakeup_injection_point(node, "exec-insert-before-insert-speculative")
    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblpk")

    ##########################################################################
    print("# Test: REINDEX + UPSERT ON CONSTRAINT (s1 wakes before reindex)")

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-set-dead', 'wait');
"""
    )

    s3.do_async("REINDEX INDEX CONCURRENTLY test.tblpk_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    s1.do_async(
        "INSERT INTO test.tblpk VALUES (13, now()) ON CONFLICT ON CONSTRAINT tblpk_pkey DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    # Start s2 BEFORE waking reindex
    s2.do_async(
        "INSERT INTO test.tblpk VALUES (13, now()) ON CONFLICT ON CONSTRAINT tblpk_pkey DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    # Wake s1 first, then reindex, then s2
    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")
    wakeup_injection_point(node, "reindex-relation-concurrently-before-set-dead")
    wakeup_injection_point(node, "exec-insert-before-insert-speculative")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblpk")

    ##########################################################################
    print("# Test: REINDEX on partitioned table (set-dead phase)")

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-set-dead', 'wait');
"""
    )

    s3.do_async("REINDEX INDEX CONCURRENTLY test.tbl_partition_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    s1.do_async(
        "INSERT INTO test.tblparted VALUES (13, now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    wakeup_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    s2.do_async(
        "INSERT INTO test.tblparted VALUES (13, now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")
    wakeup_injection_point(node, "exec-insert-before-insert-speculative")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblparted")

    ##########################################################################
    print("# Test: REINDEX on partitioned table (swap phase)")

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-swap', 'wait');
"""
    )

    s3.do_async("REINDEX INDEX CONCURRENTLY test.tbl_partition_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-swap")

    s1.do_async(
        "INSERT INTO test.tblparted VALUES (13, now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    wakeup_injection_point(node, "reindex-relation-concurrently-before-swap")

    s2.do_async(
        "INSERT INTO test.tblparted VALUES (13, now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    wakeup_injection_point(node, "exec-insert-before-insert-speculative")
    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblparted")

    ##########################################################################
    print("# Test: REINDEX on partitioned table (s1 wakes before reindex)")

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-set-dead', 'wait');
"""
    )

    s3.do_async("REINDEX INDEX CONCURRENTLY test.tbl_partition_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-set-dead")

    s1.do_async(
        "INSERT INTO test.tblparted VALUES (13, now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    # Start s2 BEFORE waking reindex
    s2.do_async(
        "INSERT INTO test.tblparted VALUES (13, now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    # Wake s1 first, then reindex, then s2
    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")
    wakeup_injection_point(node, "reindex-relation-concurrently-before-set-dead")
    wakeup_injection_point(node, "exec-insert-before-insert-speculative")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblparted")

    ##########################################################################
    print(
        "# Test: REINDEX on partitioned table, cache inval between two "
        "get_partition_ancestors"
    )

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-init-partition-after-get-partition-ancestors', 'wait');
"""
    )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('reindex-relation-concurrently-before-swap', 'wait');
"""
    )

    s2.do_async("REINDEX INDEX CONCURRENTLY test.tbl_partition_pkey;")

    ok_injection_point(node, "reindex-relation-concurrently-before-swap")

    s1.do_async(
        "INSERT INTO test.tblparted VALUES (13, now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-init-partition-after-get-partition-ancestors")

    wakeup_injection_point(node, "reindex-relation-concurrently-before-swap")

    wakeup_injection_point(node, "exec-init-partition-after-get-partition-ancestors")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblparted")

    ##########################################################################
    print("# Test: CREATE INDEX CONCURRENTLY + UPSERT")
    # Uses invalidate-catalog-snapshot-end to test catalog invalidation
    # during UPSERT

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    # Get the session's backend PID before attaching injection points
    s1_pid = s1.query_oneval("SELECT pg_backend_pid()")

    # s1 attaches BOTH injection points - the unique constraint check AND catalog snapshot
    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    # In cases of cache clobbering, s1 may hit the injection point during attach.
    # Start attach asynchronously so we can check if it blocks.
    s1.do_async(
        "SELECT injection_points_attach('invalidate-catalog-snapshot-end', 'wait');"
    )

    # Wait for that session to become idle (attach completed), or wake it up if
    # it becomes stuck on injection point.
    if not wait_for_idle(node, s1_pid):
        ok_injection_point(
            node,
            "invalidate-catalog-snapshot-end",
            "s1 hit injection point during attach (cache clobbering mode)",
        )
        node.safe_sql(
            """
            SELECT injection_points_wakeup('invalidate-catalog-snapshot-end');
        """
        )
    # Wait for async command to complete
    s1.wait_for_completion()

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('define-index-before-set-valid', 'wait');
"""
    )

    # s3: Start CREATE INDEX CONCURRENTLY (blocks on define-index-before-set-valid)
    s3.do_async("CREATE UNIQUE INDEX CONCURRENTLY tbl_pkey_duplicate ON test.tblpk(i);")

    ok_injection_point(node, "define-index-before-set-valid")

    # s1: Start UPSERT (blocks on invalidate-catalog-snapshot-end)
    s1.do_async(
        "INSERT INTO test.tblpk VALUES (13,now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "invalidate-catalog-snapshot-end")

    # Wakeup s3 (CREATE INDEX continues, triggers catalog invalidation)
    wakeup_injection_point(node, "define-index-before-set-valid")

    # s2: Start UPSERT (blocks on exec-insert-before-insert-speculative)
    s2.do_async(
        "INSERT INTO test.tblpk VALUES (13,now()) ON CONFLICT (i) DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")

    wakeup_injection_point(node, "invalidate-catalog-snapshot-end")

    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    wakeup_injection_point(node, "exec-insert-before-insert-speculative")

    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblparted")

    ##########################################################################
    print("# Test: CREATE INDEX CONCURRENTLY on partial index + UPSERT")
    # Uses invalidate-catalog-snapshot-end to test catalog invalidation during UPSERT

    s1 = node.connect()
    s2 = node.connect()
    s3 = node.connect()

    s1_pid = s1.query_oneval("SELECT pg_backend_pid()")

    # s1 attaches BOTH injection points - the unique constraint check AND catalog snapshot
    s1.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('check-exclusion-or-unique-constraint-no-conflict', 'wait');
"""
    )

    s1.do("SELECT injection_points_attach('invalidate-catalog-snapshot-end', 'wait');")

    # In cases of cache clobbering, s1 may hit the injection point during attach.
    # Wait for that session to become idle (attach completed), or wake it up if
    # it becomes stuck on injection point.
    if not wait_for_idle(node, s1_pid):
        ok_injection_point(
            node,
            "invalidate-catalog-snapshot-end",
            "s1 hit injection point during attach (cache clobbering mode)",
        )
        node.safe_sql(
            """
            SELECT injection_points_wakeup('invalidate-catalog-snapshot-end');
        """
        )

    s2.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('exec-insert-before-insert-speculative', 'wait');
"""
    )

    s3.do(
        """
SELECT injection_points_set_local();
SELECT injection_points_attach('define-index-before-set-valid', 'wait');
"""
    )

    # s3: Start CREATE INDEX CONCURRENTLY (blocks on define-index-before-set-valid)
    s3.do_async(
        "CREATE UNIQUE INDEX CONCURRENTLY tbl_pkey_special_duplicate ON test.tblexpr(abs(i)) WHERE i < 10000;"
    )

    ok_injection_point(node, "define-index-before-set-valid")

    # s1: Start UPSERT (blocks on invalidate-catalog-snapshot-end)
    s1.do_async(
        "INSERT INTO test.tblexpr VALUES(13,now()) ON CONFLICT (abs(i)) WHERE i < 100 DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "invalidate-catalog-snapshot-end")

    # Wakeup s3 (CREATE INDEX continues, triggers catalog invalidation)
    wakeup_injection_point(node, "define-index-before-set-valid")

    # s2: Start UPSERT (blocks on exec-insert-before-insert-speculative)
    s2.do_async(
        "INSERT INTO test.tblexpr VALUES(13,now()) ON CONFLICT (abs(i)) WHERE i < 100 DO UPDATE SET updated_at = now();"
    )

    ok_injection_point(node, "exec-insert-before-insert-speculative")
    wakeup_injection_point(node, "invalidate-catalog-snapshot-end")
    ok_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")
    wakeup_injection_point(node, "exec-insert-before-insert-speculative")
    wakeup_injection_point(node, "check-exclusion-or-unique-constraint-no-conflict")

    clean_safe_quit_ok(s1, s2, s3)

    node.safe_sql("TRUNCATE TABLE test.tblexpr")
