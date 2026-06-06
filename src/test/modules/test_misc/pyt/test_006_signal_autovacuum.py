# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test signaling autovacuum worker with pg_signal_autovacuum_worker.

Only roles with privileges of pg_signal_autovacuum_worker are allowed to
signal autovacuum workers.  This test uses an injection point located
at the beginning of the autovacuum worker startup.
"""

import os
import re

import pytest


def test_006_signal_autovacuum(create_pg):
    node = create_pg("node", start=False)

    # This ensures a quick worker spawn.
    node.append_conf("autovacuum_naptime = 1\n")
    node.start()

    # The whole test is gated on the enable_injection_points build flag.
    # An injection-points build installs the injection_points extension; check
    # that it is available, as it may be possible that this script is run with
    # installcheck, where the module would not be installed by default.
    if os.environ.get("enable_injection_points", "no") != "yes":
        pytest.skip("Injection points not supported by this build")
    if node.safe_sql(
        "SELECT count(*) FROM pg_available_extensions "
        "WHERE name = 'injection_points'"
    ) == "0":
        pytest.skip("Extension injection_points not installed")

    node.safe_sql("CREATE EXTENSION injection_points;")

    node.safe_sql(
        "CREATE ROLE regress_regular_role;\n"
        "CREATE ROLE regress_worker_role;\n"
        "GRANT pg_signal_autovacuum_worker TO regress_worker_role;\n"
    )

    # From this point, autovacuum worker will wait at startup.
    node.safe_sql(
        "SELECT injection_points_attach('autovacuum-worker-start', 'wait');"
    )

    # Accelerate worker creation in case we reach this point before the naptime
    # ends.
    node.reload()

    # Wait until an autovacuum worker starts.
    node.wait_for_event("autovacuum worker", "autovacuum-worker-start")

    # And grab one of them.
    av_pid = node.safe_sql(
        "SELECT pid FROM pg_stat_activity "
        "WHERE backend_type = 'autovacuum worker' "
        "AND wait_event = 'autovacuum-worker-start' LIMIT 1;"
    )

    # Regular role cannot terminate autovacuum worker.
    sess = node.connect()
    try:
        sess.do("SET ROLE regress_regular_role")
        sess.query(f"SELECT pg_terminate_backend('{av_pid}')")
        psql_err = sess.get_stderr()
    finally:
        sess.close()

    assert re.search(
        r"ERROR:  permission denied to terminate process\n"
        r'DETAIL:  Only roles with privileges of the '
        r'"pg_signal_autovacuum_worker" role may terminate autovacuum '
        r"workers\.",
        psql_err,
    ), "autovacuum worker not signaled with regular role"

    offset = node.log_position()

    # Role with pg_signal_autovacuum_worker can terminate autovacuum worker.
    sess = node.connect()
    try:
        sess.do("SET ROLE regress_worker_role")
        sess.query(f"SELECT pg_terminate_backend('{av_pid}')")
    finally:
        sess.close()

    # Wait for the autovacuum worker to exit before scanning the logs.
    assert node.poll_query_until(
        f"SELECT count(*) = 0 FROM pg_stat_activity "
        f"WHERE pid = '{av_pid}' AND backend_type = 'autovacuum worker';"
    )

    # Check that the primary server logs a FATAL indicating that autovacuum
    # is terminated.
    assert node.log_contains(
        r"FATAL: .*terminating autovacuum process due to administrator command",
        offset,
    ), "autovacuum worker signaled with pg_signal_autovacuum_worker granted"

    # Release injection point.
    node.safe_sql(
        "SELECT injection_points_detach('autovacuum-worker-start');"
    )
