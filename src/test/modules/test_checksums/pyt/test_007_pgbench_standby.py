# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test enabling data checksums on a primary/standby pair under pgbench load.

Test suite for enabling data checksums in an online cluster comprising a
primary and a replicated standby, with concurrent activity via pgbench runs.

The installed ``pgbench`` binary is run as a background subprocess.  All other
SQL goes through the in-process libpq Session (safe_sql / poll_query_until).

This suite is expensive.  It skips entirely unless PG_TEST_EXTRA contains
"checksum" (pared-down, one iteration) or "checksum_extended" (full suite,
five iterations and long-running pgbench).  It also requires an
injection-points build.
"""

import os
import random
import re
import subprocess

import pytest

# The number of full test iterations which will be performed.  The exact
# number of tests performed and the wall time taken is non-deterministic as the
# test performs a lot of randomized actions, but 5 iterations will be a long
# test run regardless.
TEST_ITERATIONS_DEFAULT = 1
TEST_ITERATIONS_EXTENDED = 5

# Physical replication slot used by the standby.
NODE_PRIMARY_SLOT = "physical_slot"

# Regex matching a checksum verification failure in a server log.
PAGE_VERIFICATION_FAILED = re.compile(r"page verification failed,.+\d$", re.M)


class _PgbenchRunner:
    """Manages a single background pgbench process against one node.

    Terminates any currently-running pgbench before launching a new one.
    """

    def __init__(self, bindir, extended):
        self._pgbench = os.path.join(bindir, "pgbench")
        if not os.path.exists(self._pgbench):
            self._pgbench = "pgbench"
        self._extended = extended
        self._proc = None
        # Held open for the runner's lifetime; closed in close().
        self._devnull = open(os.devnull, "r+b")  # pylint: disable=consider-using-with

    def start(self, node, standby):
        # Terminate any currently running pgbench process before continuing.
        self.finish()

        clients = 1
        runtime = 5
        if self._extended:
            # Randomize the number of pgbench clients a bit (range 1-16).
            clients = 1 + random.randint(0, 14)
            runtime = 600

        cmd = [
            self._pgbench,
            "-h",
            node.host,
            "-p",
            str(node.port),
            "-T",
            str(runtime),
            "-c",
            str(clients),
        ]
        # Randomize whether we spawn connections or not.
        if self._extended and random.random() < 0.5:
            cmd.append("-C")
        # If we run on a standby it needs to be a read-only benchmark.
        if standby:
            cmd.append("-S")
            cmd.append("-n")
        # Finally add the database name to use.
        cmd.append("postgres")

        # Background process; reaped later by finish().
        self._proc = subprocess.Popen(  # pylint: disable=consider-using-with
            cmd,
            stdin=self._devnull,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def finish(self):
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self._proc = None

    def close(self):
        self.finish()
        self._devnull.close()


def _flip_data_checksums(primary, standby, checksums, state, extended):
    """Invert the state of data checksums in the cluster.

    If data checksums are on then disable them and vice versa.  Validates the
    before and after state on both nodes.  Returns the new state.
    """
    temptablewait = False

    # First, make sure the cluster is in the state we expect it to be.
    checksums.test_checksum_state(primary, state)
    checksums.test_checksum_state(standby, state)

    if state == "off":
        # Coin-toss to see if we are injecting a retry due to a temptable.
        if checksums.cointoss():
            primary.safe_sql(
                "SELECT injection_points_attach("
                "'datachecksumsworker-fake-temptable-wait', 'notice');"
            )
            temptablewait = True

        # Log LSN right before we start changing checksums.
        result = primary.safe_sql("SELECT pg_current_wal_lsn()")
        print(f"# LSN before enabling: {result}")

        # Ensure that the primary switches to "inprogress-on".
        checksums.enable_data_checksums(primary, wait="inprogress-on")

        if extended:
            checksums.random_sleep()

        # Wait for checksum enable to be replayed.  The standby connects with
        # application_name=standby, which is how wait_for_catchup locates it.
        primary.wait_for_catchup("standby", "replay")

        # Ensure that the standby has switched to "inprogress-on" or "on".
        # Normally it would be "inprogress-on", but it is theoretically
        # possible for the primary to complete the checksum enabling *and* have
        # the standby replay that record before we reach the check below.
        res = standby.poll_query_until(
            "SELECT setting = 'off' FROM pg_catalog.pg_settings "
            "WHERE name = 'data_checksums';",
            "f",
        )
        assert res, "ensure standby has absorbed the inprogress-on barrier"
        result = standby.safe_sql(
            "SELECT setting FROM pg_catalog.pg_settings "
            "WHERE name = 'data_checksums';"
        )
        assert result in ("inprogress-on", "on"), (
            "ensure checksums are on, or in progress, on standby_1, got: " + result
        )

        # Wait for checksums enabled on the primary and standby.
        checksums.wait_for_checksum_state(primary, "on")

        # Log LSN right after the primary flips checksums to "on".
        result = primary.safe_sql("SELECT pg_current_wal_lsn()")
        print(f"# LSN after enabling: {result}")

        if extended:
            checksums.random_sleep()
        checksums.wait_for_checksum_state(standby, "on")

        if temptablewait:
            primary.safe_sql(
                "SELECT injection_points_detach("
                "'datachecksumsworker-fake-temptable-wait');"
            )
        return "on"

    if state == "on":
        if extended:
            checksums.random_sleep()

        # Log LSN right before we start changing checksums.
        result = primary.safe_sql("SELECT pg_current_wal_lsn()")
        print(f"# LSN before disabling: {result}")

        checksums.disable_data_checksums(primary)
        primary.wait_for_catchup("standby", "replay")

        # Wait for checksums disabled on the primary and standby.
        if extended:
            checksums.random_sleep()
        checksums.wait_for_checksum_state(primary, "off")
        checksums.wait_for_checksum_state(standby, "off")

        # Log LSN right after the primary flips checksums to "off".
        result = primary.safe_sql("SELECT pg_current_wal_lsn()")
        print(f"# LSN after disabling: {result}")

        return "off"

    # This should only happen due to programmer error when hacking on the test
    # code, but since that might pass subtly we error out.
    raise AssertionError(f"data_checksum_state variable has invalid state: {state}")


def _check_no_verification_failures(node, offset, msg):
    """Assert the node's log has no page-verification failures past *offset*.

    Returns the new offset (current log size).
    """
    log = node.log_content()[offset:]
    assert not PAGE_VERIFICATION_FAILED.search(log), msg
    return node.log_position()


def test_007_pgbench_standby(create_pg, pg_bin, checksums, bindir):
    # This test suite is expensive, or very expensive, to execute.  There are
    # two PG_TEST_EXTRA options for running it, "checksum" for a pared-down
    # test suite and "checksum_extended" for the full suite.
    pg_test_extra = os.environ.get("PG_TEST_EXTRA", "")
    extended = bool(re.search(r"\bchecksum_extended\b", pg_test_extra))
    if not re.search(r"\bchecksum(_extended)?\b", pg_test_extra):
        pytest.skip("Expensive data checksums test disabled")

    if os.environ.get("enable_injection_points", "no") != "yes":
        pytest.skip("Injection points not supported by this build")

    test_iterations = TEST_ITERATIONS_EXTENDED if extended else TEST_ITERATIONS_DEFAULT

    # Variables which record the current state of the cluster.
    data_checksum_state = "off"
    primary_loglocation = 0
    standby_loglocation = 0

    # -----------------------------------------------------------------------
    # Create and start a cluster with one primary and one standby node, and
    # ensure they are caught up and in sync.
    node_primary = create_pg(
        "pgbench_standby_main",
        start=False,
        allows_streaming=True,
        initdb_extra=["--no-data-checksums"],
    )
    # max_connections needs to be bumped in order to accommodate the pgbench
    # clients and log_statement is dialled down since it otherwise will
    # generate enormous amounts of logging.  Page verification failures are
    # still logged.
    node_primary.append_conf(
        "\n"
        "max_connections = 30\n"
        "log_statement = none\n"
        "hot_standby_feedback = on\n"
    )
    node_primary.start()
    node_primary.safe_sql("CREATE EXTENSION test_checksums;")
    node_primary.safe_sql("CREATE EXTENSION injection_points;")
    # Create some content to have un-checksummed data in the cluster.
    node_primary.safe_sql("CREATE TABLE t AS SELECT generate_series(1, 100000) AS a;")
    node_primary.safe_sql(
        f"SELECT pg_create_physical_replication_slot('{NODE_PRIMARY_SLOT}');"
    )
    node_primary.backup("primary_backup")

    node_standby = create_pg("pgbench_standby_standby", start=False)
    node_standby.init_from_backup(node_primary, "primary_backup")

    # Build primary_conninfo without nested single quotes (the value is itself
    # single-quoted in postgresql.conf).  application_name=standby lets
    # wait_for_catchup locate this node in pg_stat_replication.
    connstr_primary = (
        f"host={node_primary.host} port={node_primary.port} "
        "dbname=postgres application_name=standby"
    )
    node_standby.append_conf(
        f"\nprimary_conninfo='{connstr_primary}'\n"
        f"primary_slot_name = '{NODE_PRIMARY_SLOT}'\n"
    )
    node_standby.set_standby_mode()
    node_standby.start()

    pgbench_primary = _PgbenchRunner(bindir, extended)
    pgbench_standby = _PgbenchRunner(bindir, extended)

    try:
        # Initialize pgbench and wait for the objects to be created on the
        # standby.
        scalefactor = 10 if extended else 1
        pg_bin.command_ok(
            [
                "pgbench",
                "-h",
                node_primary.host,
                "-p",
                str(node_primary.port),
                "-i",
                "-s",
                str(scalefactor),
                "-q",
                "postgres",
            ],
            "pgbench initialization",
        )
        node_primary.wait_for_catchup("standby", "replay")

        # Start the test suite with pgbench running on all nodes.
        pgbench_standby.start(node_standby, standby=True)
        pgbench_primary.start(node_primary, standby=False)

        # Main test suite.  This loop will start a pgbench run on the cluster
        # and while that's running flip the state of data checksums
        # concurrently.  It will then randomly restart the cluster and check
        # for the desired state.  The idea behind doing things randomly is to
        # stress out any timing related issues by subjecting the cluster to
        # varied workloads.
        for i in range(test_iterations):
            print(f"# iteration {i + 1} of {test_iterations}")

            if not node_primary.postmaster_alive():
                # Start, to do recovery, and stop.
                node_primary.start()
                node_primary.stop("fast")

                # Since the log isn't being written to now, parse the log and
                # check for instances of checksum verification failures.
                primary_loglocation = _check_no_verification_failures(
                    node_primary,
                    primary_loglocation,
                    "no checksum validation errors in primary log "
                    "(during WAL recovery)",
                )

                # Randomize the WAL size, to trigger checkpoints less/more
                # often.
                sb = 32 + random.randint(0, 959)
                node_primary.append_conf(f"\nmax_wal_size = {sb}\n")
                print(f"# changing primary max_wal_size to {sb}")
                node_primary.start()

                # Start a pgbench in the background against the primary.
                pgbench_primary.start(node_primary, standby=False)

            if not node_standby.postmaster_alive():
                node_standby.start()
                node_standby.stop("fast")

                # Since the log isn't being written to now, parse the log and
                # check for instances of checksum verification failures.
                standby_loglocation = _check_no_verification_failures(
                    node_standby,
                    standby_loglocation,
                    "no checksum validation errors in standby_1 log "
                    "(during WAL recovery)",
                )

                # Randomize the WAL size, to trigger checkpoints less/more
                # often.
                sb = 32 + random.randint(0, 959)
                node_standby.append_conf(f"\nmax_wal_size = {sb}\n")
                print(f"# changing standby max_wal_size to {sb}")
                node_standby.start()

                # Start a read-only pgbench in the background on the standby.
                pgbench_standby.start(node_standby, standby=True)

            node_primary.safe_sql("UPDATE t SET a = a + 1;")
            node_primary.wait_for_catchup("standby", "write")

            data_checksum_state = _flip_data_checksums(
                node_primary,
                node_standby,
                checksums,
                data_checksum_state,
                extended,
            )
            if extended:
                checksums.random_sleep()
            result = node_primary.safe_sql("SELECT count(*) FROM t WHERE a > 1")
            assert result == "100000", "ensure data pages can be read back on primary"
            checksums.random_sleep()

            # Potentially powercycle the cluster (the nodes independently).
            if extended and checksums.cointoss():
                node_primary.stop(checksums.stopmode())

                # Slurp the file after shutdown, so that it doesn't interfere
                # with the recovery.
                primary_loglocation = _check_no_verification_failures(
                    node_primary,
                    primary_loglocation,
                    "no checksum validation errors in primary log "
                    "(outside WAL recovery)",
                )

            if extended:
                checksums.random_sleep()

            if extended and checksums.cointoss():
                node_standby.stop(checksums.stopmode())

                # Slurp the file after shutdown, so that it doesn't interfere
                # with the recovery.
                standby_loglocation = _check_no_verification_failures(
                    node_standby,
                    standby_loglocation,
                    "no checksum validation errors in standby_1 log "
                    "(outside WAL recovery)",
                )

        # Make sure the nodes are running.
        if not node_primary.postmaster_alive():
            node_primary.start()
        if not node_standby.postmaster_alive():
            node_standby.start()

        # Stop the background pgbench runs before final verification so they
        # don't keep mutating the data being checked.
        pgbench_primary.finish()
        pgbench_standby.finish()

        # Testrun is over, ensure that data reads back as expected and perform
        # a final verification of the data checksum state.
        result = node_primary.safe_sql("SELECT count(*) FROM t WHERE a > 1")
        assert result == "100000", "ensure data pages can be read back on primary"
        checksums.test_checksum_state(node_primary, data_checksum_state)
        checksums.test_checksum_state(node_standby, data_checksum_state)

        # Perform one final pass over the logs and hunt for unexpected errors.
        primary_loglocation = _check_no_verification_failures(
            node_primary,
            primary_loglocation,
            "no checksum validation errors in primary log",
        )
        standby_loglocation = _check_no_verification_failures(
            node_standby,
            standby_loglocation,
            "no checksum validation errors in standby_1 log",
        )
    finally:
        pgbench_primary.close()
        pgbench_standby.close()
