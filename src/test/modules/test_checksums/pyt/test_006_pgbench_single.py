# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test enabling data checksums in an online cluster under concurrent pgbench load.

A pgbench workload runs in the background while the
main thread repeatedly toggles data checksums on and off, randomly restarts and
power-cycles the node, and verifies that data pages can always be read back and
that no checksum verification failures appear in the server log.

The installed ``pgbench`` binary is run as a background subprocess (via the
node's ``pg_bin`` environment).  Any SQL the test needs runs in-process through
the libpq Session (``node.safe_sql``/``node.poll_query_until``).
"""

import os
import random
import re
import subprocess

import pytest

from pypg.util import TIMEOUT_DEFAULT

# Regex matching a checksum verification failure in the server log.
_PAGE_VERIFICATION_FAILED = re.compile(r"page verification failed,.+\d$", re.M)


def _check_no_checksum_errors(node, offset, label):
    """Assert no page-verification failures in the log at/after *offset*.

    Returns the new log size to use as the next offset.
    """
    log = node.log_content()[offset:]
    assert not _PAGE_VERIFICATION_FAILED.search(
        log
    ), f"no checksum validation errors in primary log ({label})"
    return node.log_position()


def _background_rw_pgbench(node, extended, checksums, current):
    """Start a pgbench run in the background against *node*.

    If a previous pgbench is still running it is shut down first.  Returns the
    new Popen handle.
    """
    # If a previous pgbench is still running, start by shutting it down.
    if current is not None:
        if current.poll() is None:
            current.terminate()
        current.wait()

    clients = 1
    runtime = 2
    if extended:
        # Randomize the number of pgbench clients a bit (range 1-16)
        clients = 1 + int(random.random() * 15)
        runtime = 600

    cmd = ["pgbench", "-p", str(node.port), "-T", str(runtime), "-c", str(clients)]
    # Randomize whether we spawn connections or not
    if extended and checksums.cointoss():
        cmd.append("-C")
    # Finally add the database name to use
    cmd.append("postgres")

    pg_bin = node.pg_bin
    argv = [pg_bin.resolve(cmd[0]), *cmd[1:]]
    print("# Running (background): " + " ".join(argv))
    devnull = subprocess.DEVNULL
    return subprocess.Popen(
        argv,
        env=pg_bin.command_env(None),
        stdin=devnull,
        stdout=devnull,
        stderr=devnull,
    )


def _flip_data_checksums(node, state, extended, checksums):
    """Invert the data checksum state of the cluster, validating before/after.

    Returns the new state string.
    """
    temptablewait = False

    # First, make sure the cluster is in the state we expect it to be
    checksums.test_checksum_state(node, state)

    if state == "off":
        # Coin-toss to see if we are injecting a retry due to a temptable
        if checksums.cointoss():
            node.safe_sql(
                "SELECT injection_points_attach("
                "'datachecksumsworker-fake-temptable-wait', 'notice');"
            )
            temptablewait = True

        # log LSN right before we start changing checksums
        result = node.safe_sql("SELECT pg_current_wal_lsn()")
        print(f"# LSN before enabling: {result}")

        # Ensure that the primary switches to "inprogress-on"
        checksums.enable_data_checksums(node, wait="inprogress-on")

        if extended:
            checksums.random_sleep()

        # Wait for checksums enabled on the primary
        checksums.wait_for_checksum_state(node, "on")

        # log LSN right after the primary flips checksums to "on"
        result = node.safe_sql("SELECT pg_current_wal_lsn()")
        print(f"# LSN after enabling: {result}")

        if extended:
            checksums.random_sleep()

        if temptablewait:
            node.safe_sql(
                "SELECT injection_points_detach("
                "'datachecksumsworker-fake-temptable-wait');"
            )
        return "on"

    if state == "on":
        if extended:
            checksums.random_sleep()

        # log LSN right before we start changing checksums
        result = node.safe_sql("SELECT pg_current_wal_lsn()")
        print(f"# LSN before disabling: {result}")

        checksums.disable_data_checksums(node)

        # Wait for checksums disabled on the primary
        checksums.wait_for_checksum_state(node, "off")

        # log LSN right after the primary flips checksums to "off"
        result = node.safe_sql("SELECT pg_current_wal_lsn()")
        print(f"# LSN after disabling: {result}")

        if extended:
            checksums.random_sleep()
        return "off"

    # This should only happen due to programmer error when hacking on the test
    # code, but since that might pass subtly we error out.
    raise AssertionError(f"data_checksum_state variable has invalid state: {state}")


def test_006_pgbench_single(create_pg, pg_bin, checksums):
    # This test suite is expensive, or very expensive, to execute.  There are
    # two PG_TEST_EXTRA options for running it, "checksum" for a pared-down
    # suite and "checksum_extended" for the full suite.  The full suite can run
    # for hours on slow or constrained systems.
    pg_test_extra = os.environ.get("PG_TEST_EXTRA", "")
    extended = bool(re.search(r"\bchecksum_extended\b", pg_test_extra))
    if not re.search(r"\bchecksum(_extended)?\b", pg_test_extra):
        pytest.skip("Expensive data checksums test disabled")

    if os.environ.get("enable_injection_points", "no") != "yes":
        pytest.skip("Injection points not supported by this build")

    # The number of full test iterations which will be performed.  The exact
    # number of tests performed and the wall time taken is non-deterministic as
    # the test performs a lot of randomized actions, but 10 iterations will be
    # a long test run regardless.
    test_iterations = 10 if extended else 1

    # Variables which record the current state of the cluster
    data_checksum_state = "off"
    pgbench = None
    node_loglocation = 0

    # Create and start a cluster with one node
    node = create_pg(
        "pgbench_single_main",
        start=False,
        allows_streaming=True,
        initdb_extra=["--no-data-checksums"],
    )
    # max_connections need to be bumped in order to accommodate for pgbench
    # clients and log_statement is dialled down since it otherwise will
    # generate enormous amounts of logging.  Page verification failures are
    # still logged.
    node.append_conf("max_connections = 100\nlog_statement = none\n")
    node.start()

    try:
        node.safe_sql("CREATE EXTENSION test_checksums;")
        node.safe_sql("CREATE EXTENSION injection_points;")
        # Create some content to have un-checksummed data in the cluster
        node.safe_sql("CREATE TABLE t AS SELECT generate_series(1, 100000) AS a;")

        # Initialize pgbench
        scalefactor = 10 if extended else 1
        node.pg_bin.command_ok(
            [
                "pgbench",
                "-p",
                str(node.port),
                "-i",
                "-s",
                str(scalefactor),
                "-q",
                "postgres",
            ],
            "pgbench initialization",
        )

        # Start the test suite with pgbench running.
        pgbench = _background_rw_pgbench(node, extended, checksums, pgbench)

        # Main test suite.  This loop will start a pgbench run on the cluster
        # and while that's running flip the state of data checksums
        # concurrently.  It will then randomly restart the cluster and then
        # check for the desired state.  The idea behind doing things randomly is
        # to stress out any timing related issues by subjecting the cluster to
        # varied workloads.
        for i in range(test_iterations):
            print(f"# iteration {i + 1} of {test_iterations}")

            if not node.postmaster_alive():
                # Start, to do recovery, and stop
                node.start()
                node.stop("fast")

                # Since the log isn't being written to now, parse the log and
                # check for instances of checksum verification failures.
                node_loglocation = _check_no_checksum_errors(
                    node,
                    node_loglocation,
                    "during WAL recovery",
                )

                # Randomize the WAL size, to trigger checkpoints less/more often
                sb = 64 + int(random.random() * 1024)
                node.append_conf(f"max_wal_size = {sb}\n")
                print(f"# changing max_wal_size to {sb}")

                node.start()

                # Start a pgbench in the background against the primary
                pgbench = _background_rw_pgbench(node, extended, checksums, pgbench)

            node.safe_sql("UPDATE t SET a = a + 1;")

            data_checksum_state = _flip_data_checksums(
                node, data_checksum_state, extended, checksums
            )
            if extended:
                checksums.random_sleep()
            result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1")
            assert result == "100000", "ensure data pages can be read back on primary"

            if extended:
                checksums.random_sleep()

            # Potentially powercycle the node
            if checksums.cointoss():
                node.stop(checksums.stopmode())

                node.pg_bin.command_ok(
                    ["pg_controldata", node.data_dir],
                    "pg_controldata",
                )

                node_loglocation = _check_no_checksum_errors(
                    node,
                    node_loglocation,
                    "outside WAL recovery",
                )

            if extended:
                checksums.random_sleep()

        # Make sure the node is running
        if not node.postmaster_alive():
            node.start()

        # Testrun is over, ensure that data reads back as expected and perform a
        # final verification of the data checksum state.
        result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1")
        assert result == "100000", "ensure data pages can be read back on primary"
        checksums.test_checksum_state(node, data_checksum_state)

        # Perform one final pass over the logs and hunt for unexpected errors
        node_loglocation = _check_no_checksum_errors(node, node_loglocation, "final")
    finally:
        if pgbench is not None:
            if pgbench.poll() is None:
                pgbench.terminate()
            try:
                pgbench.wait(timeout=TIMEOUT_DEFAULT)
            except subprocess.TimeoutExpired:
                pgbench.kill()
                pgbench.wait()
