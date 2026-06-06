# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test enabling data checksums in an online cluster across restarts.

Exercises that a restart which breaks checksum processing leaves the cluster
in a sane state, and that checksum enablement can complete and be disabled
afterwards.
"""

import os
import time


# The temporary-table barrier portion is only exercised under the extended
# suite; the surrounding basic checks always run.
def test_002_restarts(create_pg, checksums):
    # Initialize node with checksums disabled.
    node = create_pg("restarts_node", start=False,
                     initdb_extra=["--no-data-checksums"])
    node.start()

    # Create some content to have un-checksummed data in the cluster
    node.safe_sql("CREATE TABLE t AS SELECT generate_series(1,10000) AS a;")

    # Ensure that checksums are disabled
    checksums.test_checksum_state(node, "off")

    extra = os.environ.get("PG_TEST_EXTRA", "")
    run_extended = "checksum_extended" in extra.split()

    # The temporary-table barrier and restart-breaks-processing path only runs
    # under the extended suite.  We guard it with a conditional so the basic
    # enable/readback/disable checks below always execute.
    if run_extended:
        # Create a barrier for checksum enablement to block on, in this case a
        # pre-existing temporary table which is kept open while processing is
        # started.  We keep a dedicated session open holding the temporary
        # table as we enable checksums through the node's regular session in
        # another connection.
        #
        # This is a similar test to the synthetic variant in test_005_injection
        # which fakes this scenario.
        bsession = node.connect()
        bsession.do("CREATE TEMPORARY TABLE tt (a integer);")

        # In another session, make sure we can see the blocking temp table but
        # start processing anyways and check that we are blocked with a proper
        # wait event.
        result = node.safe_sql(
            "SELECT relpersistence FROM pg_catalog.pg_class "
            "WHERE relname = 'tt';"
        )
        assert result == "t", "ensure we can see the temporary table"

        # Enabling data checksums shouldn't work as the process is blocked on
        # the temporary table held open by bsession. Ensure that we reach
        # inprogress-on before we do more tests.
        checksums.enable_data_checksums(node, wait="inprogress-on")

        # Wait for processing to finish and the worker waiting for leftover
        # temp relations to be able to actually finish
        assert node.poll_query_until(
            "SELECT wait_event FROM pg_catalog.pg_stat_activity "
            "WHERE backend_type = 'datachecksums worker';",
            "ChecksumEnableTemptableWait",
        )

        # The datachecksumsworker waits for temporary tables to disappear for 3
        # seconds before retrying, so sleep for 4 seconds to be guaranteed to
        # see a retry cycle
        time.sleep(4)

        # Re-check the wait event to ensure we are blocked on the right thing.
        result = node.safe_sql(
            "SELECT wait_event FROM pg_catalog.pg_stat_activity "
            "WHERE backend_type = 'datachecksums worker';"
        )
        assert result == "ChecksumEnableTemptableWait", \
            "ensure the correct wait condition is set"
        checksums.test_checksum_state(node, "inprogress-on")

        # Stop the cluster while bsession is still attached.  We can't close
        # the session first since the brief period between closing and stopping
        # might be enough for checksums to get enabled.
        node.stop()
        bsession.close()
        node.start()

        # Ensure the checksums aren't enabled across the restart.  This leaves
        # the cluster in the same state as before we entered the block.
        checksums.test_checksum_state(node, "off")

    checksums.enable_data_checksums(node, wait="on")

    result = node.safe_sql("SELECT count(*) FROM t WHERE a > 1")
    assert result == "9999", "ensure checksummed pages can be read back"

    assert node.poll_query_until(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE backend_type LIKE 'datachecksums%';",
        "0",
    ), "await datachecksums worker/launcher termination"

    checksums.disable_data_checksums(node, wait=1)

    node.stop()
