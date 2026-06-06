# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test remove of temporary files after a crash."""

import os
import re
import signal

from pypg.util import poll_until


def _kill_backend(pid):
    """SIGKILL a specific backend, mirroring 'pg_ctl kill KILL <pid>'."""
    os.kill(pid, signal.SIGKILL)


def _wait_backend_blocked_on_lock(session, pid):
    """Run the server-side loop that waits until backend *pid* is stuck on a
    not-granted lock, then returns the 'insert-tuple-lock-waiting' marker.

    Sends the DO block + SELECT on the 2nd session.
    """
    res = session.query(
        "DO $c$\n"
        "DECLARE\n"
        "  c INT;\n"
        "BEGIN\n"
        "  LOOP\n"
        "    SELECT COUNT(*) INTO c FROM pg_locks WHERE pid = " + str(pid)
        + " AND NOT granted;\n"
        "    IF c > 0 THEN\n"
        "      EXIT;\n"
        "    END IF;\n"
        "  END LOOP;\n"
        "END; $c$;\n"
        "SELECT $$insert-tuple-lock-waiting$$;"
    )
    assert res.error_message is None, res.error_message
    assert re.search("insert-tuple-lock-waiting", res.psqlout), res.psqlout


def _detect_session_died(session):
    """Send a long sleep async and confirm the session dies due to the crash.

    Sends 'SELECT pg_sleep(<timeout_default>)' on the 2nd session and waits
    for the connection-loss / crash WARNING on stderr.
    """
    # Send a sleep that would block; the postmaster's crash handling will
    # terminate this backend instead, so the async result reports failure.
    session.do_async("SELECT pg_sleep(180)")
    res = session.get_async_result()
    # On a crash the backend is terminated: either an error result comes back
    # (FATAL: terminating connection because of crash of another server
    # process) or the connection is simply lost (result is None / error set).
    pattern = re.compile(
        r"terminating connection because of crash of another server process"
        r"|server closed the connection unexpectedly"
        r"|connection to server was lost"
        r"|could not send data to server"
        r"|terminating connection due to"
        r"|no connection to the server"
    )
    if res is None:
        # Connection dropped with no error result; confirm it is no longer OK.
        from libpq import ConnStatusType

        assert session.conn_status() != ConnStatusType.CONNECTION_OK, \
            "second session should have died after SIGKILL"
        return
    msg = (res.error_message or "") + (res.psqlout or "")
    assert pattern.search(msg), \
        f"second session died successfully after SIGKILL; got: {msg!r}"


def _ls_tmp_count(node):
    """Return COUNT(1) of files in base/pgsql_tmp as an int."""
    return int(
        node.safe_sql("SELECT COUNT(1) FROM pg_ls_dir($$base/pgsql_tmp$$)")
    )


def _crash_cycle(node, remove_temp_files):
    """Run one full open-sessions / block / SIGKILL / restart cycle.

    Returns once the server has finished restarting and is reachable again.
    The two concurrent backends are opened with node.connect() so the crash
    drops them; they are explicitly closed afterwards.
    """
    # Session to be killed: keep it alive so we have a backend to SIGKILL.
    killme = node.connect("postgres")
    # 2nd session that blocks the 1st via the UNIQUE constraint, preventing
    # removal of the temp file created by the 1st session.
    killme2 = node.connect("postgres")
    try:
        # Get backend pid of the session to be killed.
        pid = int(killme.query_oneval("SELECT pg_backend_pid()"))

        # Insert one tuple and leave the transaction open on the 2nd session.
        res = killme2.query(
            "BEGIN;\n"
            "INSERT INTO tab_crash (a) VALUES(1);\n"
            "SELECT $$insert-tuple-to-lock-next-insert$$;"
        )
        assert res.error_message is None, res.error_message
        assert re.search("insert-tuple-to-lock-next-insert", res.psqlout), \
            res.psqlout

        # On the 1st session, open a transaction and fire the INSERT that
        # generates a temp file.  It will block on the UNIQUE lock held by the
        # 2nd session, so it must be sent asynchronously: it does not return
        # before the backend is killed.
        assert killme.do("BEGIN") is not None
        marker = killme.query_oneval("SELECT $$in-progress-before-sigkill$$")
        assert marker == "in-progress-before-sigkill", marker
        assert killme.do_async(
            "INSERT INTO tab_crash (a) "
            "SELECT i FROM generate_series(1, 5000) s(i)"
        ), "failed to send blocking insert"

        # Wait until that batch insert gets stuck on the lock.
        _wait_backend_blocked_on_lock(killme2, pid)

        # Kill the 1st backend with SIGKILL.
        _kill_backend(pid)

        # The 1st psql session is now dead; close it.
        killme.close()

        # Wait till the other session reports failure, ensuring the postmaster
        # has noticed its dead child and begun a restart cycle.
        _detect_session_died(killme2)
        killme2.close()
    finally:
        # In case of an assertion failure mid-cycle, make sure we do not leak
        # the connections (they may already be closed).
        try:
            killme.close()
        except Exception:
            pass
        try:
            killme2.close()
        except Exception:
            pass

    # Wait till the server finishes restarting and is reachable again.
    assert poll_until(
        lambda: node.poll_query_until("SELECT 1", expected="1")
    ), "server never finished restarting"


def test_022_crash_temp_files(create_pg):
    node = create_pg("node_crash")

    # By default the server doesn't restart after crash; turn that on.  Reduce
    # work_mem to generate a temporary file with a small number of rows.
    # ALTER SYSTEM cannot run inside a transaction block, and the in-process
    # Session wraps a multi-statement query in one implicit transaction, so
    # each statement is issued separately.
    node.safe_sql("ALTER SYSTEM SET remove_temp_files_after_crash = on")
    node.safe_sql("ALTER SYSTEM SET log_connections = receipt")
    node.safe_sql("ALTER SYSTEM SET work_mem = '64kB'")
    node.safe_sql("ALTER SYSTEM SET restart_after_crash = on")
    node.safe_sql("SELECT pg_reload_conf()")

    # create table, insert rows
    node.safe_sql("CREATE TABLE tab_crash (a integer UNIQUE);")

    # First cycle: remove_temp_files_after_crash = on.
    _crash_cycle(node, remove_temp_files=True)

    # Check for temporary files -- should be gone.
    assert _ls_tmp_count(node) == 0, "no temporary files"

    #
    # Test old behavior (don't remove temporary files after crash)
    #
    node.safe_sql("ALTER SYSTEM SET remove_temp_files_after_crash = off")
    node.safe_sql("SELECT pg_reload_conf()")

    # Second cycle: remove_temp_files_after_crash = off.
    _crash_cycle(node, remove_temp_files=False)

    # Check for temporary files -- should be there.
    assert _ls_tmp_count(node) == 1, "one temporary file"

    # Restart should remove the temporary files.
    node.restart()

    # Check the temporary files -- should be gone.
    assert _ls_tmp_count(node) == 0, "temporary file was removed"

    node.stop()
