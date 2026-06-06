# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for connection behavior prior to authentication.  An injection point
is attached at 'init-pre-auth' so that a new connection hangs during startup,
just before authentication.  While it is stuck, a separate session inspects
pg_stat_activity to confirm the pre-auth backend is recorded, then the
waitpoint is released and the backend is observed reaching the idle state.
"""

import os
import time

import pytest

from libpq import Session


def test_007_pre_auth(create_pg):
    if os.environ.get("enable_injection_points") != "yes":
        pytest.skip("Injection points not supported by this build")

    node = create_pg("primary", start=False)
    node.append_conf("log_connections = 'receipt,authentication'\n")
    node.start()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    if node.safe_sql(
        "SELECT count(*) FROM pg_available_extensions WHERE name = 'injection_points'"
    ) == "0":
        pytest.skip("Extension injection_points not installed")

    node.safe_sql("CREATE EXTENSION injection_points")

    # Connect to the server and inject a waitpoint.
    session = Session(connstr=node.connstr("postgres"), libdir=node.libdir)
    conn = None
    try:
        session.do("SELECT injection_points_attach('init-pre-auth', 'wait')")

        # From this point on, all new connections will hang during startup,
        # just before authentication.  Use the session connection handle for
        # server interaction.
        conn = Session(connstr=node.connstr("postgres"), libdir=node.libdir,
                       wait=False)

        # Wait for the connection to show up in pg_stat_activity, with the
        # wait_event of the injection point.  We need to poll the async
        # connection to drive it forward.
        pid = None
        while True:
            # Drive the async connection forward - it won't progress without
            # polling.
            conn.poll_connect()

            pid = session.query_oneval(
                "SELECT pid FROM pg_stat_activity "
                "WHERE backend_type = 'client backend' "
                "AND state = 'starting' "
                "AND wait_event = 'init-pre-auth';",
                missing_ok=True,
            )
            if pid is not None and pid != "":
                break

            time.sleep(0.1)

        print(f"# backend {pid} is authenticating")
        # authenticating connections are recorded in pg_stat_activity

        # Detach the waitpoint and wait for the connection to complete.
        session.do("SELECT injection_points_wakeup('init-pre-auth')")
        conn.wait_connect()

        # Make sure the pgstat entry is updated eventually.
        while True:
            state = session.query_oneval(
                f"SELECT state FROM pg_stat_activity WHERE pid = {pid}",
                missing_ok=True,
            )
            if state is not None and state == "idle":
                break

            print(
                f"# state for backend {pid} is "
                f"'{state if state is not None else 'undef'}'; "
                "waiting for 'idle'..."
            )
            time.sleep(0.1)

        # authenticated connections reach idle state in pg_stat_activity

        session.do("SELECT injection_points_detach('init-pre-auth')")
    finally:
        if conn is not None:
            conn.close()
        session.close()
