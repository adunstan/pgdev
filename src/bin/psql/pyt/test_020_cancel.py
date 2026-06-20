# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test query canceling by sending SIGINT to a running psql.  A long-running
query is started in a background psql; once the server reports the backend
is executing it (observed in-process via pg_stat_activity), SIGINT is sent to
the psql process group and the resulting cancel error is checked.
"""

import os
import signal
import subprocess
import sys

import pytest

from pypg.util import TIMEOUT_DEFAULT


# Sending SIGINT on Windows terminates the test itself, so skip there.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="sending SIGINT on Windows terminates the test itself",
)


def test_020_cancel(pg):
    node = pg

    with subprocess.Popen(
        [
            os.path.join(node.bindir, "psql"),
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "-h",
            node.host,
            "-p",
            str(node.port),
            "postgres",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Run in its own session/process group so we can deliver SIGINT to the
        # psql process without it also hitting the test process.
        start_new_session=True,
    ) as psql:
        try:
            # Send sleep command and wait until the server has registered it.
            psql.stdin.write(f"select pg_sleep({TIMEOUT_DEFAULT});\n")
            psql.stdin.flush()

            assert node.poll_query_until(
                "SELECT (SELECT count(*) FROM pg_stat_activity "
                "WHERE query ~ '^select pg_sleep') > 0;"
            ), "timed out waiting for the backend to start the sleep query"

            # Send cancel request (SIGINT to psql's process group).
            psql.send_signal(signal.SIGINT)

            _stdout, stderr = psql.communicate(timeout=TIMEOUT_DEFAULT)
        finally:
            if psql.poll() is None:
                psql.kill()
                psql.communicate()

    # The query failed as expected (ON_ERROR_STOP=1 -> nonzero exit).
    assert psql.returncode != 0, "query failed as expected"
    assert (
        "canceling statement due to user request" in stderr
    ), f"query was canceled\nstderr:\n{stderr}"
