# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test for the lock statistics and log_lock_waits.

This test creates multiple locking situations when a session (s2) has to
wait on a lock for longer than deadlock_timeout. The first tests each test a
dedicated lock type.
The last one checks that log_lock_waits has no impact on the statistics
counters.

This test also checks that log_lock_waits messages are emitted both when
a wait occurs and when the lock is acquired, and that the "still waiting for"
message is logged exactly once per wait, even if the backend wakes due
to signals.
"""

import re

import pytest

DEADLOCK_TIMEOUT = 10


def setup_sessions(node):
    """Open the two sessions s1 and s2.

    Returns ``(s1, s2)``.  Fresh libpq backends are opened with
    ``node.connect()`` (no psql subprocess); the injection point used to hold
    a backend in the deadlock-timeout path is attached from s2.
    """
    s1 = node.connect()
    s2 = node.connect()

    # Setup injection points for the waiting session
    s2.query_safe(
        "SELECT injection_points_attach('deadlock-timeout-fired', 'wait');")
    return s1, s2


def wait_for_pg_stat_lock(node, lock_type):
    """Wait until pg_stat_lock reflects the expected wait.

    Fetch waits and wait_time from pg_stat_lock for a given lock type until
    they reach expected values: at least one wait and waiting longer than the
    deadlock_timeout.
    """
    assert node.poll_query_until(
        f"""
        SELECT waits > 0 AND wait_time >= {DEADLOCK_TIMEOUT}
        FROM pg_stat_lock
        WHERE locktype = '{lock_type}';
        """
    ), f"Timed out waiting for pg_stat_lock for {lock_type}"


def wait_and_detach(node, point_name):
    """Wait for an injection point, then detach it."""
    node.wait_for_event("client backend", point_name)
    node.safe_sql(
        f"""
SELECT injection_points_detach('{point_name}');
SELECT injection_points_wakeup('{point_name}');
""")


def test_011_lock_stats(create_pg):
    node = create_pg("node", start=False)
    node.append_conf(f"deadlock_timeout = {DEADLOCK_TIMEOUT}ms")
    node.start()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    if node.safe_sql(
        "SELECT count(*) FROM pg_available_extensions "
        "WHERE name = 'injection_points'"
    ) == "0":
        pytest.skip("Injection points not supported by this build")

    node.safe_sql("CREATE EXTENSION injection_points;")

    node.safe_sql("""
CREATE TABLE test_stat_tab(key text not null, value int);
INSERT INTO test_stat_tab(key, value) VALUES('k0', 1);
""")

    ########################################################################

    ####### Relation lock

    s1, s2 = setup_sessions(node)

    log_offset = node.log_position()

    s1.query_safe("""
SELECT pg_stat_reset_shared('lock');
BEGIN;
LOCK TABLE test_stat_tab;
""")

    # s2 setup
    s2.query_safe("""
BEGIN;
SELECT pg_stat_force_next_flush();
""")
    # s2 blocks on LOCK.
    s2.do_async("LOCK TABLE test_stat_tab;")

    wait_and_detach(node, 'deadlock-timeout-fired')

    # Check that log_lock_waits message is emitted during a lock wait.
    node.wait_for_log(r"still waiting for AccessExclusiveLock on relation",
                      log_offset)

    # Wake the backend waiting on the lock and confirm it woke by calling
    # pg_log_backend_memory_contexts() and checking for the logged memory
    # contexts. This is necessary to test later that the "still waiting for"
    # message is logged exactly once per wait, even if the backend wakes
    # during the wait.
    node.safe_sql("""SELECT pg_log_backend_memory_contexts(pid)
    FROM pg_locks WHERE locktype = 'relation' AND
    relation = 'test_stat_tab'::regclass AND NOT granted;""")
    node.wait_for_log(r"logging memory contexts", log_offset)

    # deadlock_timeout fired, now commit in s1 and s2
    s1.query_safe("COMMIT")
    s2.wait_for_completion()
    s2.query_safe("COMMIT")

    # check that pg_stat_lock has been updated
    wait_for_pg_stat_lock(node, 'relation')

    # Check that log_lock_waits message is emitted when the lock is acquired
    # after waiting.
    node.wait_for_log(r"acquired AccessExclusiveLock on relation", log_offset)

    # Check that the "still waiting for" message is logged exactly once per
    # wait, even if the backend wakes during the wait.
    log_contents = node.log_content()[log_offset:]
    still_waiting = re.findall(r"still waiting for", log_contents)
    assert len(still_waiting) == 1, (
        "still waiting logged exactly once despite wakeups from "
        "pg_log_backend_memory_contexts()")

    # close sessions
    s1.close()
    s2.close()

    ####### transaction lock

    s1, s2 = setup_sessions(node)

    log_offset = node.log_position()

    # The INSERT must autocommit before the explicit transaction is opened, so
    # that session s2 can see rows k1/k2/k3 and block on s1's row lock.  Send
    # it separately from the BEGIN block: a single multi-statement query
    # containing BEGIN would run the INSERT inside the still-open transaction,
    # leaving the rows invisible to s2 (so its UPDATE would match nothing and
    # never wait).
    s1.query_safe("""
SELECT pg_stat_reset_shared('lock');
INSERT INTO test_stat_tab(key, value) VALUES('k1', 1), ('k2', 1), ('k3', 1);
""")
    s1.query_safe("""
BEGIN;
UPDATE test_stat_tab SET value = value + 1 WHERE key = 'k1';
""")

    # s2 setup
    s2.query_safe("""
SET log_lock_waits = on;
BEGIN;
SELECT pg_stat_force_next_flush();
""")
    # s2 blocks here on UPDATE
    s2.do_async("UPDATE test_stat_tab SET value = value + 1 WHERE key = 'k1';")

    wait_and_detach(node, 'deadlock-timeout-fired')

    # Check that log_lock_waits message is emitted during a lock wait.
    node.wait_for_log(r"still waiting for ShareLock on transaction",
                      log_offset)

    # deadlock_timeout fired, now commit in s1 and s2
    s1.query_safe("COMMIT")
    s2.wait_for_completion()
    s2.query_safe("COMMIT")

    # check that pg_stat_lock has been updated
    wait_for_pg_stat_lock(node, 'transactionid')

    # Check that log_lock_waits message is emitted when the lock is acquired
    # after waiting.
    node.wait_for_log(r"acquired ShareLock on transaction", log_offset)

    # Close sessions
    s1.close()
    s2.close()

    ####### advisory lock

    s1, s2 = setup_sessions(node)

    log_offset = node.log_position()

    s1.query_safe("""
SELECT pg_stat_reset_shared('lock');
SELECT pg_advisory_lock(1);
""")

    # s2 setup
    s2.query_safe("""
SET log_lock_waits = on;
BEGIN;
SELECT pg_stat_force_next_flush();
""")
    # s2 blocks on the advisory lock.
    s2.do_async("SELECT pg_advisory_lock(1);")

    wait_and_detach(node, 'deadlock-timeout-fired')

    # Check that log_lock_waits message is emitted during a lock wait.
    node.wait_for_log(r"still waiting for ExclusiveLock on advisory lock",
                      log_offset)

    # deadlock_timeout fired, now unlock and commit s2
    s1.query_safe("SELECT pg_advisory_unlock(1)")
    s2.wait_for_completion()
    s2.query_safe("""
SELECT pg_advisory_unlock(1);
COMMIT;
""")

    # check that pg_stat_lock has been updated
    wait_for_pg_stat_lock(node, 'advisory')

    # Check that log_lock_waits message is emitted when the lock is acquired
    # after waiting.
    node.wait_for_log(r"acquired ExclusiveLock on advisory lock", log_offset)

    # Close sessions
    s1.close()
    s2.close()

    ####### Ensure log_lock_waits has no impact

    s1, s2 = setup_sessions(node)

    log_offset = node.log_position()

    s1.query_safe("""
SELECT pg_stat_reset_shared('lock');
BEGIN;
LOCK TABLE test_stat_tab;
""")

    # s2 setup
    s2.query_safe("""
SET log_lock_waits = off;
BEGIN;
SELECT pg_stat_force_next_flush();
""")
    # s2 blocks on LOCK.
    s2.do_async("LOCK TABLE test_stat_tab;")

    wait_and_detach(node, 'deadlock-timeout-fired')

    # deadlock_timeout fired, now commit in s1 and s2
    s1.query_safe("COMMIT")
    s2.wait_for_completion()
    s2.query_safe("COMMIT")

    # check that pg_stat_lock has been updated
    wait_for_pg_stat_lock(node, 'relation')

    # Check that no log_lock_waits messages are emitted
    assert not node.log_contains(
        "still waiting for AccessExclusiveLock on relation", log_offset), \
        "check that no log_lock_waits message is emitted during a lock wait"
    assert not node.log_contains(
        "acquired AccessExclusiveLock on relation", log_offset), \
        ("check that no log_lock_waits message is emitted when the lock is "
         "acquired after waiting")

    # close sessions
    s1.close()
    s2.close()

    # cleanup
    node.safe_sql("DROP TABLE test_stat_tab;")
