# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test dynamic management of AIO io worker processes.

Verify that io worker backends start, stop, and respawn correctly as the
io_workers GUC is changed and when individual workers are terminated.
"""

import random


def check_io_worker_count(node, worker_count):
    """Poll until the number of 'io worker' backends equals worker_count."""
    assert node.poll_query_until(
        "SELECT COUNT(*) FROM pg_stat_activity WHERE backend_type = 'io worker'",
        str(worker_count),
    ), f"io worker count is {worker_count}"


def terminate_io_worker(node, worker_count):
    """Terminate a random io worker with SIGINT and check it exits."""
    # Select a random io worker
    pid = node.safe_sql(
        "SELECT pid FROM pg_stat_activity WHERE "
        "backend_type = 'io worker' ORDER BY RANDOM() LIMIT 1"
    )

    # terminate IO worker with SIGINT
    node.pg_bin.command_ok(
        ["pg_ctl", "kill", "INT", pid],
        "random io worker process signalled with INT",
    )

    # Check that worker exits
    assert node.poll_query_until(
        f"SELECT COUNT(*) FROM pg_stat_activity WHERE pid = {pid}", "0"
    ), "random io worker process exited after signal"


def change_number_of_io_workers(node, worker_count, prev_worker_count, expect_failure):
    """Change io_min_workers and verify the resulting state.

    Returns the new effective worker count.
    """
    res = node.session().query(f"ALTER SYSTEM SET io_min_workers = {worker_count}")
    node.safe_sql("SELECT pg_reload_conf()")

    if expect_failure:
        stderr = res.error_message or ""
        assert (
            f"{worker_count} is outside the valid range for parameter "
            f'"io_min_workers"' in stderr
        ), f"updating io_min_workers to {worker_count} failed, as expected"
        return prev_worker_count

    assert node.poll_query_until("SHOW io_min_workers", str(worker_count)), (
        f"updating number of io_min_workers from {prev_worker_count} "
        f"to {worker_count}"
    )

    check_io_worker_count(node, worker_count)
    terminate_io_worker(node, worker_count)
    check_io_worker_count(node, worker_count)

    return worker_count


def number_of_io_workers_dynamic(node):
    prev_worker_count = node.safe_sql("SHOW io_min_workers")

    # Verify that worker count can't be set to 0
    change_number_of_io_workers(node, 0, prev_worker_count, 1)

    # Verify that worker count can't be set to 33 (above the max)
    change_number_of_io_workers(node, 33, prev_worker_count, 1)

    # Try changing IO workers to a random value and verify that the worker
    # count ends up as expected. Always test the min/max of workers.
    #
    # Valid range for io_workers is [1, 32]. 8 tests in total seems
    # reasonable.
    io_workers_range = list(range(1, 33))
    random.shuffle(io_workers_range)
    for worker_count in (1, 32, io_workers_range[0], io_workers_range[6]):
        prev_worker_count = change_number_of_io_workers(
            node, worker_count, prev_worker_count, 0
        )


def test_002_io_workers(create_pg):
    node = create_pg("worker", start=False)
    node.append_conf(
        """
io_method=worker
io_worker_idle_timeout=0ms
io_worker_launch_interval=0ms
io_max_workers=32
"""
    )
    node.start()

    # Test changing the number of I/O worker processes while also evaluating
    # the handling of their termination.
    number_of_io_workers_dynamic(node)

    node.stop()
