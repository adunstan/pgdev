# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test that background workers can be terminated by db commands."""

import os

import pytest


def launch_bgworker(node, database, testcase, interruptible):
    """Ensure the worker_spi dynamic worker is launched on the specified
    database.  Returns the PID of the worker launched."""

    # Launch a background worker on the given database.
    pid = node.safe_sql(
        f"SELECT worker_spi_launch({testcase}, '{database}'::regdatabase, 0, "
        f"'{{}}', {interruptible});"
    )

    # Check that the bgworker is initialized and napping.
    result = node.poll_query_until(
        f"SELECT wait_event FROM pg_stat_activity WHERE pid = {pid};", "WorkerSpiMain"
    )
    assert result, f"dynamic bgworker {testcase} launched"

    return pid


def run_bgworker_interruptible_test(node, command, testname, pid):
    """Run query and verify that the bgworker with the specified PID has been
    terminated."""
    offset = node.log_position()

    node.safe_sql(command)

    node.wait_for_log(
        r'terminating background worker "worker_spi dynamic" '
        r"due to administrator command",
        offset,
    )

    # Postmaster entry reporting the worker as exiting.
    node.wait_for_log(
        r'LOG: .*background worker "worker_spi dynamic" '
        rf"\(PID {pid}\) exited with exit code",
        offset,
    )

    result = node.safe_sql(
        f"SELECT count(*) = 0 FROM pg_stat_activity WHERE pid = {pid};"
    )
    assert result == "t", f"dynamic bgworker stopped for {testname}"


def test_002_worker_terminate(create_pg, tmp_path):
    node = create_pg("mynode", start=False)
    # The naptime is large enough to give some room on slow machines, so as
    # the spawned workers have the time to process the interrupt requests sent
    # by the database commands.
    node.append_conf(
        """
autovacuum = off
debug_parallel_query = off
log_min_messages = debug1
worker_spi.naptime = 600
"""
    )
    node.start()

    # This test depends on injection points to detect whether background
    # workers remain.  Check if the extension injection_points is available, as
    # it may be possible that this script is run with installcheck, where the
    # module would not be installed by default.
    if (
        node.safe_sql(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        == "0"
    ):
        pytest.skip("Extension injection_points not installed")

    node.safe_sql("CREATE EXTENSION worker_spi;")

    # Launch a background worker without BGWORKER_INTERRUPTIBLE.
    pid = launch_bgworker(node, "postgres", 0, "false")

    # Ensure CREATE DATABASE WITH TEMPLATE fails because a non-interruptible
    # bgworker exists.

    # The injection point 'procarray-reduce-count' reduces the number of
    # backend retries, allowing for shorter test runs. See
    # CountOtherDBBackends().
    node.safe_sql("CREATE EXTENSION injection_points;")
    node.safe_sql("SELECT injection_points_attach('procarray-reduce-count', 'error');")

    sess = node.connect()
    try:
        sess.query("CREATE DATABASE testdb WITH TEMPLATE postgres")
        stderr = sess.get_stderr()
    finally:
        sess.close()
    assert (
        'source database "postgres" is being accessed by other users' in stderr
    ), "background worker blocked the database creation"

    # Confirm that the non-interruptible bgworker is still running.
    result = node.safe_sql(
        "SELECT count(1) FROM pg_stat_activity\n"
        "        WHERE backend_type = 'worker_spi dynamic';"
    )

    assert (
        result == "1"
    ), "background worker is still running after CREATE DATABASE WITH TEMPLATE"

    # Terminate the non-interruptible worker for the next tests.
    node.safe_sql(
        "SELECT pg_terminate_backend(pid)\n"
        "        FROM pg_stat_activity "
        "WHERE backend_type = 'worker_spi dynamic';"
    )

    # The injection point is not used anymore, release it.
    node.safe_sql("SELECT injection_points_detach('procarray-reduce-count');")

    # Check that BGWORKER_INTERRUPTIBLE allows background workers to be
    # terminated with database-related commands.

    # Test case 1: CREATE DATABASE WITH TEMPLATE
    pid = launch_bgworker(node, "postgres", 1, "true")
    run_bgworker_interruptible_test(
        node,
        "CREATE DATABASE testdb WITH TEMPLATE postgres",
        "CREATE DATABASE WITH TEMPLATE",
        pid,
    )

    # Test case 2: ALTER DATABASE RENAME
    pid = launch_bgworker(node, "testdb", 2, "true")
    run_bgworker_interruptible_test(
        node, "ALTER DATABASE testdb RENAME TO renameddb", "ALTER DATABASE RENAME", pid
    )

    # Preparation for the next test, create a tablespace.
    tablespace = str(tmp_path / "test_tablespace")
    os.makedirs(tablespace, exist_ok=True)
    node.safe_sql(f"CREATE TABLESPACE test_tablespace LOCATION '{tablespace}'")

    # Test case 3: ALTER DATABASE SET TABLESPACE
    pid = launch_bgworker(node, "renameddb", 3, "true")
    run_bgworker_interruptible_test(
        node,
        "ALTER DATABASE renameddb SET TABLESPACE test_tablespace",
        "ALTER DATABASE SET TABLESPACE",
        pid,
    )

    # Test case 4: DROP DATABASE
    pid = launch_bgworker(node, "renameddb", 4, "true")
    run_bgworker_interruptible_test(
        node, "DROP DATABASE renameddb", "DROP DATABASE", pid
    )
