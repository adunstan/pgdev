# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test enabling data checksums in an online cluster with injection points.

Injects failures into checksum processing via injection points.
"""

import os
import re

import pytest


def test_005_injection(create_pg, checksums):
    # Skip unless the build supports injection points, signalled via
    # the enable_injection_points env var.  An injection-points build installs
    # the injection_points extension; treat its availability as a fallback
    # signal that injection points are usable.
    if os.environ.get("enable_injection_points", "no") != "yes":
        pytest.skip("Injection points not supported by this build")

    # -----------------------------------------------------------------------
    # Test cluster setup

    node = create_pg("injection_node", start=False, initdb_extra=["--no-data-checksums"])
    node.start()

    # Set up test environment
    node.safe_sql("CREATE EXTENSION test_checksums;")
    node.safe_sql("CREATE EXTENSION injection_points;")

    # -----------------------------------------------------------------------
    # Inducing failures and crashes in processing

    # Force enabling checksums to fail by marking one of the databases as
    # having failed in processing.
    checksums.disable_data_checksums(node, wait=1)
    node.safe_sql(
        "SELECT injection_points_attach("
        "'datachecksumsworker-fail-db-result','notice');"
    )
    checksums.enable_data_checksums(node, wait="off")
    node.safe_sql(
        "SELECT injection_points_detach('datachecksumsworker-fail-db-result');"
    )

    # Make sure that disabling after a failure works
    checksums.disable_data_checksums(node)
    checksums.test_checksum_state(node, "off")

    # -----------------------------------------------------------------------
    # Timing and retry related tests

    pg_test_extra = os.environ.get("PG_TEST_EXTRA", "")
    if pg_test_extra and re.search(r"\bchecksum_extended\b", pg_test_extra):
        # Inject a delay in the barrier for enabling checksums
        checksums.disable_data_checksums(node, wait=1)
        node.safe_sql("SELECT dcw_inject_delay_barrier();")
        checksums.enable_data_checksums(node, wait="on")

        # Fake the existence of a temporary table at the start of processing,
        # which will force the processing to wait and retry in order to wait
        # for it to disappear.
        checksums.disable_data_checksums(node, wait=1)
        node.safe_sql(
            "SELECT injection_points_attach("
            "'datachecksumsworker-fake-temptable-wait', 'notice');"
        )
        checksums.enable_data_checksums(node, wait="on")

    node.stop()
