# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test race condition when a restart point is running during a promotion,
checking that WAL segments are correctly removed in the restart point
while the promotion finishes.

This test relies on an injection point that causes the checkpointer to
wait in the middle of a restart point on a standby.  The checkpointer
is awaken to finish its restart point only once the promotion of the
standby is completed, and the node should be able to restart properly.
"""

import os
import time

import pytest

from libpq import ConnStatusType
from pypg.util import TIMEOUT_DEFAULT, poll_until


def test_041_checkpoint_at_promote(create_pg):
    # This test is gated on the enable_injection_points build flag.  When that
    # variable is unset, fall back to the actual capability: an
    # injection-points build installs the injection_points extension, which
    # check_extension below independently confirms.  Either signal being
    # present means injection points are usable.
    node_primary = create_pg("master", allows_streaming=True)
    node_primary.append_conf("""
log_checkpoints = on
restart_after_crash = on
""")
    node_primary.restart()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    injection_points_available = node_primary.safe_sql(
        "SELECT count(*) FROM pg_available_extensions "
        "WHERE name = 'injection_points'"
    ) != "0"
    if os.environ.get("enable_injection_points", "no") != "yes" \
            and not injection_points_available:
        pytest.skip("Injection points not supported by this build")
    if not injection_points_available:
        pytest.skip("Extension injection_points not installed")

    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Setup a standby.
    node_standby = create_pg("standby1", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.start()

    # Dummy table for the upcoming tests.
    node_primary.safe_sql("checkpoint")
    node_primary.safe_sql("CREATE TABLE prim_tab (a int);")

    # Register an injection point on the standby so as the follow-up
    # restart point will wait on it.
    node_primary.safe_sql("CREATE EXTENSION injection_points;")
    # Wait until the extension has been created on the standby
    node_primary.wait_for_replay_catchup(node_standby)

    # Note that from this point the checkpointer will wait in the middle of
    # a restart point on the standby.
    node_standby.safe_sql(
        "SELECT injection_points_attach('create-restart-point', 'wait');")

    # Execute a restart point on the standby, that we will now be waiting on.
    # This needs to be in the background.
    logstart = node_standby.log_position()
    psql_session = node_standby.connect("postgres")
    assert psql_session.do_async("CHECKPOINT;"), "failed to send CHECKPOINT"

    # Switch one WAL segment to make the previous restart point remove the
    # segment once the restart point completes.
    node_primary.safe_sql("INSERT INTO prim_tab VALUES (1);")
    node_primary.safe_sql("SELECT pg_switch_wal();")
    node_primary.wait_for_replay_catchup(node_standby)

    # Wait until the checkpointer is in the middle of the restart point
    # processing.
    node_standby.wait_for_event("checkpointer", "create-restart-point")

    # Check the logs that the restart point has started on standby.  This is
    # optional, but let's be sure.
    assert node_standby.log_contains(
        "restartpoint starting: fast wait", logstart
    ), "restartpoint has started"

    # Trigger promotion during the restart point.
    node_primary.stop()
    node_standby.promote()

    # Update the start position before waking up the checkpointer!
    logstart = node_standby.log_position()

    # Now wake up the checkpointer.
    node_standby.safe_sql(
        "SELECT injection_points_wakeup('create-restart-point');")

    # Wait until the previous restart point completes on the newly-promoted
    # standby, checking the logs for that.
    checkpoint_complete = False
    for _ in range(10 * TIMEOUT_DEFAULT):
        if node_standby.log_contains("restartpoint complete", logstart):
            checkpoint_complete = True
            break
        time.sleep(0.1)
    assert checkpoint_complete, "restart point has completed"

    # Done with the async CHECKPOINT session.
    psql_session.close()

    # Kill with SIGKILL, forcing all the backends to restart.
    killme = node_standby.connect("postgres")
    try:
        pid = int(killme.query_oneval("SELECT pg_backend_pid()"))

        node_standby.signal_backend(pid, "KILL")

        # Wait until the server restarts, finishing consuming output: the
        # backend we are connected to is terminated by the crash, so the
        # connection is lost.
        killme.do_async("SELECT 1;")
        res = killme.get_async_result()
        if res is not None:
            msg = (res.error_message or "") + (res.psqlout or "")
            assert (
                killme.conn_status() != ConnStatusType.CONNECTION_OK
                or msg
            ), "psql query died successfully after SIGKILL"
        else:
            assert killme.conn_status() != ConnStatusType.CONNECTION_OK, \
                "psql query died successfully after SIGKILL"
    finally:
        killme.close()

    # Wait till server finishes restarting.
    assert poll_until(
        lambda: node_standby.poll_query_until("SELECT 1", expected="1")
    ), "server never finished restarting"

    # After recovery, the server should be able to start.
    assert node_standby.safe_sql("select 1") == "1", "psql select 1"
