# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests restarts of postgres due to crashes of a subprocess."""

# Two longer-running libpq sessions are used: One to kill a backend,
# triggering a crash-restart cycle, one to detect when postmaster
# noticed the backend died.  The second backend is necessary because
# it's otherwise hard to determine if postmaster is still accepting new
# sessions (because it hasn't noticed that the backend died), or because
# it's already restarted.
#

import os
import re
import signal

from libpq import ConnStatusType

# Patterns matching how psql/libpq reports that the connection to the
# backend was lost.  The first WARNING variant only appears on SIGQUIT,
# where the backend's signal handlers get a chance to run.
_SIGQUIT_DIED = re.compile(
    r"terminating connection because of unexpected SIGQUIT signal"
    r"|server closed the connection unexpectedly"
    r"|connection to server was lost"
    r"|could not send data to server"
    r"|no connection to the server"
)

_CRASH_DIED = re.compile(
    r"terminating connection because of crash of another server process"
    r"|server closed the connection unexpectedly"
    r"|connection to server was lost"
    r"|could not send data to server"
    r"|no connection to the server"
)


def _async_died(session, pattern):
    """Send a long sleep async and confirm the session dies due to the crash.

    Sends 'SELECT 1' / 'SELECT pg_sleep(3600)' on a psql session and waits
    for the connection-loss / crash message on stderr.
    """
    session.do_async("SELECT pg_sleep(3600)")
    res = session.get_async_result()
    if res is None:
        # Connection dropped with no error result; confirm it is no longer OK.
        assert session.conn_status() != ConnStatusType.CONNECTION_OK, \
            "session should have died after crash"
        return
    msg = (res.error_message or "") + (res.psqlout or "")
    assert pattern.search(msg), \
        f"session died successfully after crash; got: {msg!r}"


def test_013_crash_restart(create_pg):
    node = create_pg("primary", start=False, allows_streaming=True)

    # Enable pg_stat_statements to test restart of shared_preload_libraries.
    node.append_conf(
        "shared_preload_libraries = 'pg_stat_statements'\n"
        "pg_stat_statements.max = 50000\n"
        "compute_query_id = 'regress'\n"
    )

    node.start()

    # by default the framework doesn't restart after a crash.  ALTER SYSTEM
    # cannot run inside a transaction block, so issue each statement
    # separately.
    node.safe_sql("ALTER SYSTEM SET restart_after_crash = 1")
    node.safe_sql("ALTER SYSTEM SET log_connections = receipt")
    node.safe_sql("SELECT pg_reload_conf()")

    # Remember the time that pg_stat_statements was reset.  We'll use it later
    # to verify that it gets re-initialized after crash.
    node.safe_sql("CREATE EXTENSION pg_stat_statements")
    stats_reset = node.safe_sql(
        "SELECT stats_reset FROM pg_stat_statements_info")

    # libpq session, keeping it alive, so we have an alive backend to kill.
    killme = node.connect("postgres")
    # Need a second session to check if crash-restart happened.
    monitor = node.connect("postgres")

    try:
        # create table, insert row that should survive
        res = killme.query(
            "CREATE TABLE alive(status text);\n"
            "INSERT INTO alive VALUES($$committed-before-sigquit$$);\n"
            "SELECT pg_backend_pid();")
        assert res.error_message is None, res.error_message
        pid = int(res.psqlout.strip().splitlines()[-1])

        # insert a row that should *not* survive, due to in-progress xact
        res = killme.query(
            "BEGIN;\n"
            "INSERT INTO alive VALUES($$in-progress-before-sigquit$$)"
            " RETURNING status;")
        assert res.error_message is None, res.error_message
        assert re.search("in-progress-before-sigquit", res.psqlout), \
            res.psqlout

        # Start longrunning query in second session; its failure will signal
        # that crash-restart has occurred.  The initial trivial select is to
        # be sure that the session successfully connected to the backend.
        marker = monitor.query_oneval("SELECT $$psql-connected$$")
        assert marker == "psql-connected", marker

        # kill once with QUIT - we expect the backend to exit, while emitting
        # an error message first.
        os.kill(pid, signal.SIGQUIT)

        # Check that the killme session sees the killed backend as having been
        # terminated.
        _async_died(killme, _SIGQUIT_DIED)
        killme.close()

        # Wait till server restarts - we should get the WARNING here, but
        # sometimes the server is unable to send that, if interrupted while
        # sending.
        _async_died(monitor, _CRASH_DIED)
        monitor.close()
    finally:
        try:
            killme.close()
        except Exception:
            pass
        try:
            monitor.close()
        except Exception:
            pass

    # Wait till server restarts
    assert node.poll_query_until("SELECT 1", expected="1"), \
        "reconnected after SIGQUIT"

    # restart sessions, now that the crash cycle finished
    killme = node.connect("postgres")
    monitor = node.connect("postgres")

    try:
        # Verify that pg_stat_statements, loaded via shared_preload_libraries,
        # was re-initialized at the crash.
        stats_reset_after = node.safe_sql(
            "SELECT stats_reset FROM pg_stat_statements_info")
        assert stats_reset != stats_reset_after, \
            "pg_stat_statements was reset by restart"

        # Acquire pid of new backend
        pid = int(killme.query_oneval("SELECT pg_backend_pid()"))

        # Insert test rows.  The committed row must land in its own
        # transaction (it must be committed before the BEGIN);
        # the in-process Session wraps a multi-statement query in a single
        # implicit transaction, so issue the committed INSERT separately or it
        # would be rolled back together with the in-progress one at SIGKILL.
        res = killme.query(
            "INSERT INTO alive VALUES($$committed-before-sigkill$$)"
            " RETURNING status;")
        assert res.error_message is None, res.error_message
        res = killme.query(
            "BEGIN;\n"
            "INSERT INTO alive VALUES($$in-progress-before-sigkill$$)"
            " RETURNING status;")
        assert res.error_message is None, res.error_message
        assert re.search("in-progress-before-sigkill", res.psqlout), \
            res.psqlout

        # Re-start longrunning query in second session; its failure will
        # signal that crash-restart has occurred.
        marker = monitor.query_oneval("SELECT $$psql-connected$$")
        assert marker == "psql-connected", marker

        # kill with SIGKILL this time - we expect the backend to exit, without
        # being able to emit an error message.
        os.kill(pid, signal.SIGKILL)

        # Check that the killme session sees the server as being terminated.
        # No WARNING, because signal handlers aren't being run on SIGKILL.
        _async_died(killme, _SIGQUIT_DIED)
        killme.close()

        # Wait till server restarts.
        _async_died(monitor, _CRASH_DIED)
        monitor.close()
    finally:
        try:
            killme.close()
        except Exception:
            pass
        try:
            monitor.close()
        except Exception:
            pass

    # Wait till server restarts
    assert node.poll_query_until("SELECT 1", expected="1"), \
        "reconnected after SIGKILL"

    # Make sure the committed rows survived, in-progress ones not
    assert node.safe_sql("SELECT * FROM alive") == \
        "committed-before-sigquit\ncommitted-before-sigkill", \
        "data survived"

    assert node.safe_sql(
        "INSERT INTO alive VALUES($$before-orderly-restart$$)"
        " RETURNING status") == "before-orderly-restart", \
        "can still write after crash restart"

    # Confirm that the logical replication launcher, a background worker
    # without the never-restart flag, has also restarted successfully.
    assert node.poll_query_until(
        "SELECT count(*) = 1 FROM pg_stat_activity"
        " WHERE backend_type = 'logical replication launcher'"), \
        "logical replication launcher restarted after crash"

    # Just to be sure, check that an orderly restart now still works
    node.restart()

    assert node.safe_sql("SELECT * FROM alive") == \
        "committed-before-sigquit\ncommitted-before-sigkill\n" \
        "before-orderly-restart", \
        "data survived"

    assert node.safe_sql(
        "INSERT INTO alive VALUES($$after-orderly-restart$$)"
        " RETURNING status") == "after-orderly-restart", \
        "can still write after orderly restart"

    node.stop()
