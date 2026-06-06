# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Logical decoding on a standby: tests logical decoding, recovery conflict and
standby promotion.  Logical slots are created on a standby; when the primary
does something that conflicts with standby decoding (row removal / VACUUM,
incorrect wal_level, DROP DATABASE, ...) the slots get invalidated with the
right conflict reason.  Injection points are used to keep xl_running_xacts from
advancing an active slot's catalog_xmin.
"""

import os
import subprocess

import pytest

from libpq import Session
from pypg.util import TIMEOUT_DEFAULT, poll_until

DEFAULT_TIMEOUT = TIMEOUT_DEFAULT

# Name for the physical slot on primary
PRIMARY_SLOTNAME = "primary_physical"
STANDBY_PHYSICAL_SLOTNAME = "standby_physical"


# ---------------------------------------------------------------------------
# A backgrounded pg_recvlogical --start session, returned by
# make_slot_active().
# ---------------------------------------------------------------------------
class RecvLogicalHandle:
    """A long-running ``pg_recvlogical --start --no-loop`` subprocess.

    Background reader threads drain stdout and stderr into buffers as the data
    arrives.  pg_recvlogical writes each decoded record to its output fd with a
    raw write() (no stdio buffering), so the threads see complete lines
    promptly; this also avoids mixing select() with a buffered readline(), which
    can strand data in Python's TextIOWrapper buffer where select() never
    reports it as readable.
    """

    def __init__(self, argv):
        import threading

        print("# Running (background): " + " ".join(argv))
        # Lifetime is managed by finish()/kill(), not a with block.
        self._proc = subprocess.Popen(  # pylint: disable=consider-using-with
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.stdout = ""
        self.stderr = ""
        self._finished = False
        self._lock = threading.Lock()

        def _drain(stream, attr):
            while True:
                line = stream.readline()
                if line == "":
                    break
                with self._lock:
                    setattr(self, attr, getattr(self, attr) + line)

        self._out_thread = threading.Thread(
            target=_drain, args=(self._proc.stdout, "stdout"), daemon=True
        )
        self._err_thread = threading.Thread(
            target=_drain, args=(self._proc.stderr, "stderr"), daemon=True
        )
        self._out_thread.start()
        self._err_thread.start()

    def finish(self):
        """Wait for the process to exit, capturing its output.

        Returns the exit status (the value of $? equivalent, i.e. returncode).
        """
        if not self._finished:
            self._proc.wait(timeout=DEFAULT_TIMEOUT)
            self._out_thread.join(timeout=DEFAULT_TIMEOUT)
            self._err_thread.join(timeout=DEFAULT_TIMEOUT)
            self._finished = True
        return self._proc.returncode

    def pump_until(self, pattern, timeout=DEFAULT_TIMEOUT):
        """Wait until *pattern* matches the accumulated stdout.

        The reader thread keeps appending to self.stdout, so we just poll the
        buffer until the pattern matches or we time out / the process exits.
        """
        import re
        import time

        regex = re.compile(pattern, re.S)
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            with self._lock:
                buf = self.stdout
            if regex.search(buf):
                return True
            if self._proc.poll() is not None and not self._out_thread.is_alive():
                # Process exited and all output has been drained.
                with self._lock:
                    return bool(regex.search(self.stdout))
            if deadline is not None and time.monotonic() > deadline:
                with self._lock:
                    return bool(regex.search(self.stdout))
            time.sleep(0.05)

    def kill(self):
        if not self._finished:
            try:
                self._proc.kill()
            except Exception:  # pylint: disable=broad-exception-caught
                pass


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def wait_for_xmins(node, slotname, check_expr):
    """Wait until *check_expr* holds for the named slot's xmin/catalog_xmin."""
    assert node.poll_query_until(
        f"SELECT {check_expr} "
        "FROM pg_catalog.pg_replication_slots "
        f"WHERE slot_name = '{slotname}'"
    ), "Timed out waiting for slot xmins to advance"


def create_logical_slots(node, primary, slot_prefix):
    """Create the inactive/active logical slots on the standby."""
    active_slot = slot_prefix + "activeslot"
    inactive_slot = slot_prefix + "inactiveslot"
    node.create_logical_slot_on_standby(primary, inactive_slot, "testdb")
    node.create_logical_slot_on_standby(primary, active_slot, "testdb")


def drop_logical_slots(standby, slot_prefix):
    """Drop the inactive/active logical slots on the standby."""
    active_slot = slot_prefix + "activeslot"
    inactive_slot = slot_prefix + "inactiveslot"
    standby.sql(f"SELECT pg_drop_replication_slot('{inactive_slot}')")
    standby.sql(f"SELECT pg_drop_replication_slot('{active_slot}')")


def make_slot_active(node, slot_prefix, wait):
    """Launch pg_recvlogical against the 'activeslot' slot in the background.

    When *wait* is true, wait until the slot shows an active_pid (a known-good
    scenario); otherwise we are testing a known failure scenario.
    """
    active_slot = slot_prefix + "activeslot"
    argv = [
        os.path.join(node.bindir, "pg_recvlogical"),
        "--dbname",
        node.connstr("testdb"),
        "--slot",
        active_slot,
        "--option",
        "include-xids=0",
        "--option",
        "skip-empty-xacts=1",
        "--file",
        "-",
        "--no-loop",
        "--start",
    ]
    handle = RecvLogicalHandle(argv)
    if wait:
        # make sure activeslot is in use
        assert node.poll_query_until(
            "SELECT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = "
            f"'{active_slot}' AND active_pid IS NOT NULL)"
        ), "slot never became active"
    return handle


def check_pg_recvlogical_stderr(handle, check_stderr):
    """Assert pg_recvlogical terminated in response to the walsender error."""
    import re

    ret = handle.finish()
    assert ret != 0, "pg_recvlogical exited non-zero"
    assert re.search(
        check_stderr, handle.stderr
    ), f"slot has been invalidated (stderr={handle.stderr!r})"


def check_slots_dropped(standby, slot_prefix, handle):
    """Assert both slots have been dropped on the standby."""
    assert (
        standby.slot(slot_prefix + "inactiveslot")["slot_type"] == ""
    ), "inactiveslot on standby dropped"
    assert (
        standby.slot(slot_prefix + "activeslot")["slot_type"] == ""
    ), "activeslot on standby dropped"
    check_pg_recvlogical_stderr(handle, "conflict with recovery")


def change_hot_standby_feedback_and_wait_for_xmins(standby, primary, hsf, invalidated):
    """Set hot_standby_feedback on the standby and wait for primary xmins."""
    standby.append_conf(f"\nhot_standby_feedback = {hsf}\n")
    standby.reload()

    if hsf and invalidated:
        # With hot_standby_feedback on, xmin should advance, but catalog_xmin
        # should still remain NULL since there is no logical slot.
        wait_for_xmins(
            primary, PRIMARY_SLOTNAME, "xmin IS NOT NULL AND catalog_xmin IS NULL"
        )
    elif hsf:
        # With hot_standby_feedback on, xmin and catalog_xmin should advance.
        wait_for_xmins(
            primary, PRIMARY_SLOTNAME, "xmin IS NOT NULL AND catalog_xmin IS NOT NULL"
        )
    else:
        # Both should be NULL since hs_feedback is off
        wait_for_xmins(
            primary, PRIMARY_SLOTNAME, "xmin IS NULL AND catalog_xmin IS NULL"
        )


def check_slots_conflict_reason(standby, slot_prefix, reason):
    """Assert both slots are conflicting with the expected invalidation reason."""
    active_slot = slot_prefix + "activeslot"
    inactive_slot = slot_prefix + "inactiveslot"

    res = standby.safe_sql(
        "select invalidation_reason from pg_replication_slots where slot_name = "
        f"'{active_slot}' and conflicting;"
    )
    assert res == reason, f"{active_slot} reason for conflict is {reason}"

    res = standby.safe_sql(
        "select invalidation_reason from pg_replication_slots where slot_name = "
        f"'{inactive_slot}' and conflicting;"
    )
    assert res == reason, f"{inactive_slot} reason for conflict is {reason}"


def reactive_slots_change_hfs_and_wait_for_xmins(
    standby, primary, previous_slot_prefix, slot_prefix, hsf, invalidated
):
    """Recreate the slots, change hot_standby_feedback, and wait for xmins.

    Returns the new active-slot handle.
    """
    drop_logical_slots(standby, previous_slot_prefix)
    create_logical_slots(standby, primary, slot_prefix)
    change_hot_standby_feedback_and_wait_for_xmins(standby, primary, hsf, invalidated)
    handle = make_slot_active(standby, slot_prefix, True)
    # reset stat: easier to check for confl_active_logicalslot in
    # pg_stat_database_conflicts
    standby.sql("select pg_stat_reset();", dbname="testdb")
    return handle


def check_for_invalidation(standby, slot_prefix, log_start, test_name):
    """Assert both slots were invalidated and the conflict stat was updated."""
    active_slot = slot_prefix + "activeslot"
    inactive_slot = slot_prefix + "inactiveslot"

    # message should be issued.  Use wait_for_log rather than an immediate
    # log_contains check: the invalidation is logged during the standby's replay
    # of the conflicting record, which may land slightly after
    # wait_for_replay_catchup returns.
    standby.wait_for_log(
        f'invalidating obsolete replication slot "{inactive_slot}"', log_start
    )  # inactiveslot slot invalidation is logged

    standby.wait_for_log(
        f'invalidating obsolete replication slot "{active_slot}"', log_start
    )  # activeslot slot invalidation is logged

    # Verify that pg_stat_database_conflicts.confl_active_logicalslot has been
    # updated
    assert standby.poll_query_until(
        "select (confl_active_logicalslot = 1) from pg_stat_database_conflicts "
        "where datname = 'testdb'"
    ), "Timed out waiting confl_active_logicalslot to be updated"


def wait_until_vacuum_can_remove(primary, standby, vac_option, sql, to_vac):
    """Run *sql* then VACUUM so dead rows can be removed past the slot horizon.

    The injection_point avoids seeing a xl_running_xacts that could advance an
    active replication slot's catalog_xmin.
    """
    # From this point the checkpointer and bgwriter will skip writing
    # xl_running_xacts record.
    primary.safe_sql(
        "SELECT injection_points_attach('skip-log-running-xacts', 'error');",
        dbname="testdb",
    )

    # Get the current xid horizon.
    xid_horizon = primary.safe_sql(
        "select pg_snapshot_xmin(pg_current_snapshot());", dbname="testdb"
    )

    # Launch our sql.  Run each statement as its own transaction (autocommit).
    # Sending the whole thing as one multi-command string would wrap it in a
    # single implicit transaction, which changes the xids stamped on the catalog
    # tuples and prevents the row-removal conflict from firing.
    for stmt in (s.strip() for s in sql.split(";")):
        if stmt:
            primary.safe_sql(stmt, dbname="testdb")

    # Wait until we get a newer horizon.
    assert primary.poll_query_until(
        "SELECT (select pg_snapshot_xmin(pg_current_snapshot())::text::int - "
        f"{xid_horizon}) > 0",
        dbname="testdb",
    ), "new snapshot does not have a newer horizon"

    # Launch the vacuum command and insert into flush_wal (see CREATE TABLE
    # flush_wal for the reason).  VACUUM cannot run inside a transaction block,
    # so the two statements are sent separately rather than as one multi-command
    # string.
    primary.safe_sql(f"VACUUM {vac_option} verbose {to_vac};", dbname="testdb")
    primary.safe_sql("INSERT INTO flush_wal DEFAULT VALUES;", dbname="testdb")

    primary.wait_for_replay_catchup(standby)

    # Resume generating the xl_running_xacts record
    primary.safe_sql(
        "SELECT injection_points_detach('skip-log-running-xacts');",
        dbname="testdb",
    )


# ---------------------------------------------------------------------------
# The test.
# ---------------------------------------------------------------------------
def test_035_standby_logical_decoding(create_pg):
    # Skip unless built with injection points.
    if os.environ.get("enable_injection_points", "no") != "yes":
        pytest.skip("Injection points not supported by this build")

    ########################
    # Initialize primary node
    ########################
    node_primary = create_pg(
        "primary", start=False, allows_streaming="logical", has_archiving=True
    )
    node_primary.append_conf(
        "\n".join(
            [
                "wal_level = 'logical'",
                "max_replication_slots = 4",
                "max_wal_senders = 4",
                "autovacuum = off",
                "",
            ]
        )
    )
    node_primary.start()

    # Check if the extension injection_points is available.
    if (
        node_primary.safe_sql(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'injection_points'"
        )
        == "0"
    ):
        pytest.skip("Extension injection_points not installed")

    node_primary.sql("CREATE DATABASE testdb")

    node_primary.safe_sql(
        f"SELECT * FROM pg_create_physical_replication_slot('{PRIMARY_SLOTNAME}');",
        dbname="testdb",
    )

    # Check conflicting is NULL for physical slot
    res = node_primary.safe_sql(
        "SELECT conflicting is null FROM pg_replication_slots where slot_name = "
        f"'{PRIMARY_SLOTNAME}';"
    )
    assert res == "t", "Physical slot reports conflicting as NULL"

    backup_name = "b1"
    node_primary.backup(backup_name)

    # Some tests need to wait for VACUUM to be replayed.  But vacuum does not
    # flush WAL.  An insert into flush_wal outside transaction does guarantee a
    # flush.
    node_primary.sql("CREATE TABLE flush_wal();", dbname="testdb")

    #######################
    # Initialize standby node
    #######################
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(
        node_primary, backup_name, has_streaming=True, has_restoring=True
    )
    node_standby.append_conf(
        f"primary_slot_name = '{PRIMARY_SLOTNAME}'\nmax_replication_slots = 5\n"
    )
    node_standby.start()
    node_primary.wait_for_replay_catchup(node_standby)

    #######################
    # Initialize subscriber node
    #######################
    node_subscriber = create_pg("subscriber", start=False)
    node_subscriber.start()

    ##################################################
    # Test that the standby requires hot_standby to be enabled for pre-existing
    # logical slots.
    ##################################################
    node_standby.create_logical_slot_on_standby(node_primary, "restart_test")
    node_standby.stop()
    node_standby.append_conf("hot_standby = off\n")

    # Use pg_ctl directly because this test expects the server to fail during
    # startup.
    subprocess.run(
        [
            os.path.join(node_standby.bindir, "pg_ctl"),
            "--pgdata",
            node_standby.data_dir,
            "--log",
            node_standby.logfile,
            "start",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    # wait for postgres to terminate
    poll_until(
        lambda: not os.path.exists(node_standby.pidfile),
        timeout=10 * DEFAULT_TIMEOUT,
    )

    # Confirm that the server startup fails with an expected error
    import re

    logfile = node_standby.log_content()
    assert re.search(
        r'FATAL: .* logical replication slot ".*" exists on the standby, but '
        r'"hot_standby" = "off"',
        logfile,
    ), "the standby ends with an error during startup because hot_standby was disabled"

    # adjust_conf(hot_standby => on): later value wins.
    node_standby.append_conf("hot_standby = on\n")
    node_standby.start()
    node_standby.safe_sql("SELECT pg_drop_replication_slot('restart_test')")

    ##################################################
    # Test that logical decoding on the standby behaves correctly.
    ##################################################
    create_logical_slots(node_standby, node_primary, "behaves_ok_")

    node_primary.safe_sql(
        "CREATE TABLE decoding_test(x integer, y text);", dbname="testdb"
    )
    node_primary.safe_sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(1,10) s;",
        dbname="testdb",
    )

    node_primary.wait_for_replay_catchup(node_standby)

    result = node_standby.safe_sql(
        "SELECT pg_logical_slot_get_changes('behaves_ok_activeslot', NULL, NULL);",
        dbname="testdb",
    )

    # test if basic decoding works (2 BEGIN/COMMIT and 10 rows = 14 lines)
    assert (
        len(result.splitlines()) == 14
    ), "Decoding produced 14 rows (2 BEGIN/COMMIT and 10 rows)"

    # Insert some rows and verify that we get the same results from
    # pg_recvlogical and the SQL interface.
    node_primary.safe_sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(1,4) s;",
        dbname="testdb",
    )

    expected = (
        "BEGIN\n"
        "table public.decoding_test: INSERT: x[integer]:1 y[text]:'1'\n"
        "table public.decoding_test: INSERT: x[integer]:2 y[text]:'2'\n"
        "table public.decoding_test: INSERT: x[integer]:3 y[text]:'3'\n"
        "table public.decoding_test: INSERT: x[integer]:4 y[text]:'4'\n"
        "COMMIT"
    )

    node_primary.wait_for_replay_catchup(node_standby)

    stdout_sql = node_standby.safe_sql(
        "SELECT data FROM pg_logical_slot_peek_changes('behaves_ok_activeslot', "
        "NULL, NULL, 'include-xids', '0', 'skip-empty-xacts', '1');",
        dbname="testdb",
    )
    assert stdout_sql == expected, "got expected output from SQL decoding session"

    endpos = node_standby.safe_sql(
        "SELECT lsn FROM pg_logical_slot_peek_changes('behaves_ok_activeslot', "
        "NULL, NULL) ORDER BY lsn DESC LIMIT 1;",
        dbname="testdb",
    )

    # Insert some rows after $endpos, which we won't read.
    node_primary.safe_sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(5,50) s;",
        dbname="testdb",
    )

    node_primary.wait_for_replay_catchup(node_standby)

    recv = node_standby.pg_recvlogical_upto(
        "testdb",
        "behaves_ok_activeslot",
        endpos,
        DEFAULT_TIMEOUT,
        **{"include-xids": "0", "skip-empty-xacts": "1"},
    )
    assert (
        recv.stdout.rstrip("\n") == expected
    ), "got same expected output from pg_recvlogical decoding session"

    assert node_standby.poll_query_until(
        "SELECT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = "
        "'behaves_ok_activeslot' AND active_pid IS NULL)",
        dbname="testdb",
    ), "slot never became inactive"

    recv = node_standby.pg_recvlogical_upto(
        "testdb",
        "behaves_ok_activeslot",
        endpos,
        DEFAULT_TIMEOUT,
        **{"include-xids": "0", "skip-empty-xacts": "1"},
    )
    assert recv.stdout.rstrip("\n") == "", "pg_recvlogical acknowledged changes"

    node_primary.safe_sql("CREATE DATABASE otherdb")

    # Wait for catchup to ensure that the new database is visible to other
    # sessions on the standby.
    node_primary.wait_for_replay_catchup(node_standby)

    res = node_standby.sql(
        "SELECT lsn FROM pg_logical_slot_peek_changes('behaves_ok_activeslot', "
        "NULL, NULL) ORDER BY lsn DESC LIMIT 1;",
        dbname="otherdb",
    )
    assert res.error_message is not None and re.search(
        r'replication slot "behaves_ok_activeslot" was not created in this database',
        res.error_message,
    ), "replaying logical slot from another database fails"

    ##################################################
    # Test that we can subscribe on the standby with the publication created on
    # the primary.
    ##################################################
    # Create a table on the primary
    node_primary.safe_sql("CREATE TABLE tab_rep (a int primary key)")
    # Create a table (same structure) on the subscriber node
    node_subscriber.safe_sql("CREATE TABLE tab_rep (a int primary key)")
    # Create a publication on the primary
    node_primary.safe_sql("CREATE PUBLICATION tap_pub for table tab_rep")

    node_primary.wait_for_replay_catchup(node_standby)

    # Subscribe on the standby
    standby_connstr = (
        f"host={node_standby.host} port={node_standby.port} dbname=postgres"
    )

    # Use an async session here: a synchronous CREATE SUBSCRIPTION would wait for
    # activity on the primary and we wouldn't be able to run
    # pg_log_standby_snapshot() on the primary while waiting.
    sub_sess = node_subscriber.connect()
    try:
        assert sub_sess.do_async(
            "CREATE SUBSCRIPTION tap_sub "
            f"CONNECTION '{standby_connstr}' "
            "PUBLICATION tap_pub "
            "WITH (copy_data = off);"
        )

        # Log the standby snapshot to speed up the subscription creation
        node_primary.log_standby_snapshot(node_standby, "tap_sub")

        # Collect the CREATE SUBSCRIPTION result.
        assert sub_sess.get_async_result().error_message is None
    finally:
        sub_sess.close()

    node_subscriber.wait_for_subscription_sync(node_standby, "tap_sub")

    # Insert some rows on the primary
    node_primary.safe_sql("INSERT INTO tab_rep select generate_series(1,10);")

    node_primary.wait_for_replay_catchup(node_standby)
    node_standby.wait_for_catchup("tap_sub")

    # Check that the subscriber can see the rows inserted in the primary
    result = node_subscriber.safe_sql("SELECT count(*) FROM tab_rep")
    assert result == "10", "check replicated inserts after subscription on standby"

    # We do not need the subscription and the subscriber anymore
    node_subscriber.safe_sql("DROP SUBSCRIPTION tap_sub")
    node_subscriber.stop()

    # Create the injection_points extension
    node_primary.safe_sql("CREATE EXTENSION injection_points;", dbname="testdb")

    ##################################################
    # Recovery conflict scenario 1: hot_standby_feedback off and vacuum FULL.
    ##################################################
    handle = reactive_slots_change_hfs_and_wait_for_xmins(
        node_standby, node_primary, "behaves_ok_", "vacuum_full_", 0, 1
    )

    # Ensure that replication slot stats are not empty before the conflict.
    node_primary.safe_sql(
        "INSERT INTO decoding_test(x,y) SELECT 100,'100';", dbname="testdb"
    )

    assert node_standby.poll_query_until(
        "SELECT total_txns > 0 FROM pg_stat_replication_slots WHERE slot_name = "
        "'vacuum_full_activeslot'",
        dbname="testdb",
    ), "replication slot stats of vacuum_full_activeslot not updated"

    # This should trigger the conflict
    wait_until_vacuum_can_remove(
        node_primary,
        node_standby,
        "full",
        "CREATE TABLE conflict_test(x integer, y text); DROP TABLE conflict_test;",
        "pg_class",
    )

    # Check invalidation in the logfile and in pg_stat_database_conflicts
    check_for_invalidation(
        node_standby, "vacuum_full_", 1, "with vacuum FULL on pg_class"
    )

    # Verify reason for conflict is 'rows_removed'
    check_slots_conflict_reason(node_standby, "vacuum_full_", "rows_removed")

    # Attempting to alter an invalidated slot should result in an error
    rep_sess = Session(
        connstr=node_standby.connstr("postgres") + " replication=database",
        libdir=node_standby.libdir,
    )
    try:
        alter_res = rep_sess.query(
            "ALTER_REPLICATION_SLOT vacuum_full_inactiveslot (failover);"
        )
        assert (
            alter_res.error_message is not None
            and re.search(
                r'can no longer access replication slot "vacuum_full_inactiveslot"',
                alter_res.error_message,
            )
            and re.search(
                r'This replication slot has been invalidated due to "rows_removed"\.',
                alter_res.error_message,
            )
        ), "invalidated slot cannot be altered"

        # Ensure that replication slot stats are not removed after invalidation.
        assert (
            node_standby.safe_sql(
                "SELECT total_txns > 0 FROM pg_stat_replication_slots WHERE slot_name = "
                "'vacuum_full_activeslot'",
                dbname="testdb",
            )
            == "t"
        ), "replication slot stats not removed after invalidation"

        handle = make_slot_active(node_standby, "vacuum_full_", False)
        # We are not able to read from the slot as it has been invalidated
        check_pg_recvlogical_stderr(
            handle,
            'can no longer access replication slot "vacuum_full_activeslot"',
        )

        # Attempt to copy an invalidated logical replication slot
        copy_res = rep_sess.query(
            "select pg_copy_logical_replication_slot('vacuum_full_inactiveslot', "
            "'vacuum_full_inactiveslot_copy');"
        )
        assert copy_res.error_message is not None and re.search(
            r'cannot copy invalidated replication slot "vacuum_full_inactiveslot"',
            copy_res.error_message,
        ), "invalidated slot cannot be copied"
    finally:
        rep_sess.close()

    # Set hot_standby_feedback to on
    change_hot_standby_feedback_and_wait_for_xmins(node_standby, node_primary, 1, 1)

    ##################################################
    # Verify that invalidated logical slots stay invalidated across a restart.
    ##################################################
    node_standby.restart()

    # Verify reason for conflict is retained across a restart.
    check_slots_conflict_reason(node_standby, "vacuum_full_", "rows_removed")

    ##################################################
    # Verify that invalidated logical slots do not lead to retaining WAL.
    ##################################################
    restart_lsn = node_standby.safe_sql(
        "SELECT restart_lsn FROM pg_replication_slots "
        "WHERE slot_name = 'vacuum_full_activeslot' AND conflicting;"
    )

    # As pg_walfile_name() can not be executed on the standby, get the WAL file
    # name associated to this lsn from the primary.
    walfile_name = node_primary.safe_sql(f"SELECT pg_walfile_name('{restart_lsn}')")

    # Generate some activity and switch WAL file on the primary
    node_primary.advance_wal(1)
    node_primary.safe_sql("checkpoint;")

    # Wait for the standby to catch up
    node_primary.wait_for_replay_catchup(node_standby)

    # Request a checkpoint on the standby to trigger the WAL file(s) removal
    node_standby.safe_sql("checkpoint;")

    # Verify that the WAL file has not been retained on the standby
    standby_walfile = os.path.join(node_standby.data_dir, "pg_wal", walfile_name)
    assert not os.path.isfile(
        standby_walfile
    ), "invalidated logical slots do not lead to retaining WAL"

    ##################################################
    # Recovery conflict scenario 2: conflict due to row removal with
    # hot_standby_feedback off.
    ##################################################
    logstart = node_standby.log_position()

    handle = reactive_slots_change_hfs_and_wait_for_xmins(
        node_standby, node_primary, "vacuum_full_", "row_removal_", 0, 1
    )

    # This should trigger the conflict
    wait_until_vacuum_can_remove(
        node_primary,
        node_standby,
        "",
        "CREATE TABLE conflict_test(x integer, y text); DROP TABLE conflict_test;",
        "pg_class",
    )

    check_for_invalidation(
        node_standby, "row_removal_", logstart, "with vacuum on pg_class"
    )
    check_slots_conflict_reason(node_standby, "row_removal_", "rows_removed")

    handle = make_slot_active(node_standby, "row_removal_", False)
    check_pg_recvlogical_stderr(
        handle, 'can no longer access replication slot "row_removal_activeslot"'
    )

    ##################################################
    # Recovery conflict scenario 3: same as 2 but on a shared catalog table.
    ##################################################
    logstart = node_standby.log_position()

    handle = reactive_slots_change_hfs_and_wait_for_xmins(
        node_standby, node_primary, "row_removal_", "shared_row_removal_", 0, 1
    )

    # Trigger the conflict (create/drop a role and vacuum pg_authid)
    wait_until_vacuum_can_remove(
        node_primary,
        node_standby,
        "",
        "CREATE ROLE create_trash; DROP ROLE create_trash;",
        "pg_authid",
    )

    check_for_invalidation(
        node_standby, "shared_row_removal_", logstart, "with vacuum on pg_authid"
    )
    check_slots_conflict_reason(node_standby, "shared_row_removal_", "rows_removed")

    handle = make_slot_active(node_standby, "shared_row_removal_", False)
    check_pg_recvlogical_stderr(
        handle,
        'can no longer access replication slot "shared_row_removal_activeslot"',
    )

    ##################################################
    # Recovery conflict scenario 4: same as 2 but on a non catalog table.
    # No conflict expected.
    ##################################################
    logstart = node_standby.log_position()

    handle = reactive_slots_change_hfs_and_wait_for_xmins(
        node_standby, node_primary, "shared_row_removal_", "no_conflict_", 0, 1
    )

    # This should not trigger a conflict
    wait_until_vacuum_can_remove(
        node_primary,
        node_standby,
        "",
        "CREATE TABLE conflict_test(x integer, y text); "
        "INSERT INTO conflict_test(x,y) SELECT s, s::text FROM generate_series(1,4) s; "
        "UPDATE conflict_test set x=1, y=1;",
        "conflict_test",
    )

    # message should not be issued
    assert not node_standby.log_contains(
        'invalidating obsolete replication slot "no_conflict_inactiveslot"', logstart
    ), "inactiveslot slot invalidation is not logged with vacuum on conflict_test"

    assert not node_standby.log_contains(
        'invalidating obsolete replication slot "no_conflict_activeslot"', logstart
    ), "activeslot slot invalidation is not logged with vacuum on conflict_test"

    # Verify that confl_active_logicalslot has not been updated
    assert node_standby.poll_query_until(
        "select (confl_active_logicalslot = 0) from pg_stat_database_conflicts "
        "where datname = 'testdb'"
    ), "Timed out waiting confl_active_logicalslot to be updated"

    # Verify slots are reported as non conflicting in pg_replication_slots
    assert (
        node_standby.safe_sql(
            "select bool_or(conflicting) from "
            "(select conflicting from pg_replication_slots where slot_type = 'logical')"
        )
        == "f"
    ), "Logical slots are reported as non conflicting"

    # Turn hot_standby_feedback back on
    change_hot_standby_feedback_and_wait_for_xmins(node_standby, node_primary, 1, 0)

    # Restart the standby node to ensure no slots are still active
    node_standby.restart()

    ##################################################
    # Recovery conflict scenario 5: conflict due to on-access pruning.
    ##################################################
    logstart = node_standby.log_position()

    handle = reactive_slots_change_hfs_and_wait_for_xmins(
        node_standby, node_primary, "no_conflict_", "pruning_", 0, 0
    )

    # Injection point avoids seeing a xl_running_xacts.
    node_primary.safe_sql(
        "SELECT injection_points_attach('skip-log-running-xacts', 'error');",
        dbname="testdb",
    )

    # This should trigger the conflict
    node_primary.safe_sql(
        "CREATE TABLE prun(id integer, s char(2000)) "
        "WITH (fillfactor = 75, user_catalog_table = true);",
        dbname="testdb",
    )
    node_primary.safe_sql("INSERT INTO prun VALUES (1, 'A');", dbname="testdb")
    node_primary.safe_sql("UPDATE prun SET s = 'B';", dbname="testdb")
    node_primary.safe_sql("UPDATE prun SET s = 'C';", dbname="testdb")
    node_primary.safe_sql("UPDATE prun SET s = 'D';", dbname="testdb")
    node_primary.safe_sql("UPDATE prun SET s = 'E';", dbname="testdb")

    node_primary.wait_for_replay_catchup(node_standby)

    # Resume generating the xl_running_xacts record
    node_primary.safe_sql(
        "SELECT injection_points_detach('skip-log-running-xacts');",
        dbname="testdb",
    )

    check_for_invalidation(node_standby, "pruning_", logstart, "with on-access pruning")
    check_slots_conflict_reason(node_standby, "pruning_", "rows_removed")

    handle = make_slot_active(node_standby, "pruning_", False)
    check_pg_recvlogical_stderr(
        handle, 'can no longer access replication slot "pruning_activeslot"'
    )

    # Turn hot_standby_feedback back on
    change_hot_standby_feedback_and_wait_for_xmins(node_standby, node_primary, 1, 1)

    ##################################################
    # Recovery conflict scenario 6: incorrect wal_level on primary.
    ##################################################
    logstart = node_standby.log_position()

    drop_logical_slots(node_standby, "pruning_")
    create_logical_slots(node_standby, node_primary, "wal_level_")

    handle = make_slot_active(node_standby, "wal_level_", True)

    # reset stat
    node_standby.sql("select pg_stat_reset();", dbname="testdb")

    # Make primary wal_level replica. This will trigger slot conflict.
    node_primary.append_conf("\nwal_level = 'replica'\n")
    node_primary.restart()

    node_primary.wait_for_replay_catchup(node_standby)

    check_for_invalidation(node_standby, "wal_level_", logstart, "due to wal_level")
    check_slots_conflict_reason(node_standby, "wal_level_", "wal_level_insufficient")

    handle = make_slot_active(node_standby, "wal_level_", False)
    # We are not able to read from the slot as it requires
    # effective_wal_level >= logical on the primary server
    check_pg_recvlogical_stderr(
        handle,
        'logical decoding on standby requires "effective_wal_level" >= '
        '"logical" on the primary',
    )

    # Restore primary wal_level
    node_primary.append_conf("\nwal_level = 'logical'\n")
    node_primary.restart()
    node_primary.wait_for_replay_catchup(node_standby)

    handle = make_slot_active(node_standby, "wal_level_", False)
    # as the slot has been invalidated we should not be able to read
    check_pg_recvlogical_stderr(
        handle, 'can no longer access replication slot "wal_level_activeslot"'
    )

    ##################################################
    # DROP DATABASE should drop its slots, including active slots.
    ##################################################
    drop_logical_slots(node_standby, "wal_level_")
    create_logical_slots(node_standby, node_primary, "drop_db_")

    handle = make_slot_active(node_standby, "drop_db_", True)

    # Create a slot on a database that would not be dropped.  This slot should
    # not get dropped.
    node_standby.create_logical_slot_on_standby(node_primary, "otherslot", "postgres")

    # dropdb on the primary to verify slots are dropped on standby
    node_primary.safe_sql("DROP DATABASE testdb")

    node_primary.wait_for_replay_catchup(node_standby)

    assert (
        node_standby.safe_sql(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = 'testdb')"
        )
        == "f"
    ), "database dropped on standby"

    check_slots_dropped(node_standby, "drop_db_", handle)

    assert (
        node_standby.slot("otherslot")["slot_type"] == "logical"
    ), "otherslot on standby not dropped"

    # Cleanup: manually drop the slot that was not dropped.
    node_standby.sql("SELECT pg_drop_replication_slot('otherslot')")

    ##################################################
    # Test standby promotion and logical decoding behavior after the standby
    # gets promoted.
    ##################################################
    node_standby.reload()

    node_primary.sql("CREATE DATABASE testdb")
    node_primary.safe_sql(
        "CREATE TABLE decoding_test(x integer, y text);", dbname="testdb"
    )

    # Wait for the standby to catchup before initializing the cascading standby
    node_primary.wait_for_replay_catchup(node_standby)

    # The standby's cached testdb session was connected to the testdb that has
    # just been dropped and recreated; its backend was terminated by the DROP
    # DATABASE replay.  Discard it so the next query reconnects to the new
    # database.
    node_standby.session("testdb").close()

    # Create a physical replication slot on the standby.
    node_standby.safe_sql(
        "SELECT * FROM pg_create_physical_replication_slot("
        f"'{STANDBY_PHYSICAL_SLOTNAME}');",
        dbname="testdb",
    )

    # Initialize cascading standby node
    node_cascading_standby = create_pg("cascading_standby", start=False)
    node_standby.backup(backup_name)
    node_cascading_standby.init_from_backup(
        node_standby, backup_name, has_streaming=True, has_restoring=True
    )
    node_cascading_standby.append_conf(
        f"primary_slot_name = '{STANDBY_PHYSICAL_SLOTNAME}'\n"
        "hot_standby_feedback = on\n"
    )
    node_cascading_standby.start()

    # create the logical slots
    create_logical_slots(node_standby, node_primary, "promotion_")

    # Wait for the cascading standby to catchup before creating the slots
    node_standby.wait_for_replay_catchup(node_cascading_standby, node_primary)

    # create the logical slots on the cascading standby too
    create_logical_slots(node_cascading_standby, node_primary, "promotion_")

    # Make slots active
    handle = make_slot_active(node_standby, "promotion_", True)
    cascading_handle = make_slot_active(node_cascading_standby, "promotion_", True)

    try:
        # Insert some rows before the promotion
        node_primary.safe_sql(
            "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM "
            "generate_series(1,4) s;",
            dbname="testdb",
        )

        # Wait for both standbys to catchup
        node_primary.wait_for_replay_catchup(node_standby)
        node_standby.wait_for_replay_catchup(node_cascading_standby, node_primary)

        # promote
        node_standby.promote()

        # insert some rows on promoted standby
        node_standby.safe_sql(
            "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM "
            "generate_series(5,7) s;",
            dbname="testdb",
        )

        # Wait for the cascading standby to catchup
        node_standby.wait_for_replay_catchup(node_cascading_standby)

        expected = (
            "BEGIN\n"
            "table public.decoding_test: INSERT: x[integer]:1 y[text]:'1'\n"
            "table public.decoding_test: INSERT: x[integer]:2 y[text]:'2'\n"
            "table public.decoding_test: INSERT: x[integer]:3 y[text]:'3'\n"
            "table public.decoding_test: INSERT: x[integer]:4 y[text]:'4'\n"
            "COMMIT\n"
            "BEGIN\n"
            "table public.decoding_test: INSERT: x[integer]:5 y[text]:'5'\n"
            "table public.decoding_test: INSERT: x[integer]:6 y[text]:'6'\n"
            "table public.decoding_test: INSERT: x[integer]:7 y[text]:'7'\n"
            "COMMIT"
        )

        # check that we are decoding pre and post promotion inserted rows
        stdout_sql = node_standby.safe_sql(
            "SELECT data FROM pg_logical_slot_peek_changes('promotion_inactiveslot', "
            "NULL, NULL, 'include-xids', '0', 'skip-empty-xacts', '1');",
            dbname="testdb",
        )
        assert (
            stdout_sql == expected
        ), "got expected output from SQL decoding session on promoted standby"

        # check that we are decoding pre and post promotion inserted rows with
        # pg_recvlogical that has started before the promotion
        assert handle.pump_until(
            r"^.*COMMIT.*COMMIT$"
        ), "got 2 COMMIT from pg_recvlogical output"
        assert (
            handle.stdout.rstrip("\n") == expected
        ), "got same expected output from pg_recvlogical decoding session"

        # check that we are decoding pre and post promotion inserted rows on the
        # cascading standby
        stdout_sql = node_cascading_standby.safe_sql(
            "SELECT data FROM pg_logical_slot_peek_changes('promotion_inactiveslot', "
            "NULL, NULL, 'include-xids', '0', 'skip-empty-xacts', '1');",
            dbname="testdb",
        )
        assert (
            stdout_sql == expected
        ), "got expected output from SQL decoding session on cascading standby"

        # check that we are decoding pre and post promotion inserted rows with
        # pg_recvlogical that has started before the promotion on the cascading
        # standby
        assert cascading_handle.pump_until(
            r"^.*COMMIT.*COMMIT$"
        ), "got 2 COMMIT from pg_recvlogical output"
        assert cascading_handle.stdout.rstrip("\n") == expected, (
            "got same expected output from pg_recvlogical decoding session on "
            "cascading standby"
        )
    finally:
        handle.kill()
        cascading_handle.kill()
