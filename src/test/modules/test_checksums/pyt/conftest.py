# Copyright (c) 2026, PostgreSQL Global Development Group

"""Shared helpers for the data-checksums test suite.

Provides a small :class:`DataChecksums` helper, exposed through the
``checksums`` fixture, whose methods enable/disable data checksums on a running
cluster, poll the data_checksums GUC, and add randomized sleeps and stop modes
to shake out race conditions.

All queries run in-process through the libpq Session (node.safe_sql /
poll_query_until).
"""

import random

import pytest

from pypg.util import TIMEOUT_DEFAULT


class DataChecksums:
    """Utility methods for testing data checksums in a running cluster."""

    def test_checksum_state(self, node, state):
        """Assert the data_checksums GUC at *node* equals *state*.

        Returns True if it matches, else False.
        """
        result = node.safe_sql(
            "SELECT setting FROM pg_catalog.pg_settings "
            "WHERE name = 'data_checksums';"
        )
        assert result == state, f"ensure checksums are set to {state} on {node.name}"
        return result == state

    def wait_for_checksum_state(self, node, state, timeout=TIMEOUT_DEFAULT):
        """Poll the data_checksums GUC until it equals *state* or times out."""
        res = node.poll_query_until(
            "SELECT setting FROM pg_catalog.pg_settings "
            "WHERE name = 'data_checksums';",
            state,
            timeout=timeout,
        )
        assert res, f"ensure data checksums are transitioned to {state} on {node.name}"
        return res

    def _wait_for_launcher_exit(self, node):
        node.poll_query_until(
            "SELECT count(*) = 0 FROM pg_catalog.pg_stat_activity "
            "WHERE backend_type = 'datachecksums launcher';"
        )

    def enable_data_checksums(self, node, cost_delay=0, cost_limit=100, wait=None):
        """Enable data checksums in the cluster running at *node*.

        *cost_delay*/*cost_limit* are passed to pg_enable_data_checksums().  If
        *wait* is given, wait for that state (and, for 'on'/'off', for the
        launcher to exit) before returning.
        """
        node.safe_sql(f"SELECT pg_enable_data_checksums({cost_delay}, {cost_limit});")
        if wait is not None:
            self.wait_for_checksum_state(node, wait)
            if wait in ("on", "off"):
                self._wait_for_launcher_exit(node)

    def disable_data_checksums(self, node, wait=None):
        """Disable data checksums in the cluster running at *node*.

        If *wait* is given (its value is ignored, unlike enable), wait for the
        state to become 'off' and for the launcher to exit before returning.
        """
        node.safe_sql("SELECT pg_disable_data_checksums();")
        if wait is not None:
            self.wait_for_checksum_state(node, "off")
            self._wait_for_launcher_exit(node)

    def cointoss(self):
        """Return 0 or 1 with even probability."""
        return int(random.random() < 0.5)

    def random_sleep(self, max_seconds=3):
        """Sleep a random (0, max_seconds) interval, sometimes.

        Injects unpredictable sleeps to avoid timing patterns that mask race
        conditions.  A *max_seconds* of 0 disables the sleep entirely.
        """
        import time

        if max_seconds == 0:
            return
        if self.cointoss():
            time.sleep(random.randint(0, max_seconds - 1) if max_seconds > 1 else 0)

    def stopmode(self):
        """Randomly select a valid stop mode."""
        return "immediate" if self.cointoss() else "fast"


@pytest.fixture
def checksums():
    """Yield a :class:`DataChecksums` helper for the data-checksums tests."""
    return DataChecksums()
