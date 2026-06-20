# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_ctl logrotate and the resulting log file handling."""

import os
import re
import time

from pypg.util import TIMEOUT_DEFAULT, slurp_file


def fetch_file_name(logfiles, fmt):
    """Extract the file name of a *fmt* from the contents of current_logfiles."""
    filename = None
    for line in logfiles.split("\n"):
        m = re.search(rf"{fmt} (.*)$", line)
        if m:
            filename = m.group(1)
    return filename


def check_log_pattern(fmt, logfiles, pattern, node):
    """Check for a pattern in the logs associated to one format."""
    lfname = fetch_file_name(logfiles, fmt)

    max_attempts = 10 * TIMEOUT_DEFAULT

    logcontents = ""
    for _ in range(max_attempts):
        logcontents = slurp_file(os.path.join(node.data_dir, lfname))
        if re.search(pattern, logcontents):
            break
        time.sleep(0.1)

    assert re.search(pattern, logcontents), f"found expected log file content for {fmt}"

    # While we're at it, test pg_current_logfile() function
    assert (
        node.safe_sql(f"SELECT pg_current_logfile('{fmt}')") == lfname
    ), f"pg_current_logfile() gives correct answer with {fmt}"


def test_004_logrotate(create_pg):
    # Set up node with logging collector
    node = create_pg("primary", start=False)
    node.append_conf(
        """
logging_collector = on
log_destination = 'stderr, csvlog, jsonlog'
# these ensure stability of test results:
log_rotation_age = 0
lc_messages = 'C'
"""
    )

    node.start()

    # Verify that log output gets to the file

    node.sql("SELECT 1/0")

    # might need to retry if logging collector process is slow...
    max_attempts = 10 * TIMEOUT_DEFAULT

    current_logfiles = None
    for _ in range(max_attempts):
        try:
            current_logfiles = slurp_file(
                os.path.join(node.data_dir, "current_logfiles")
            )
            break
        except OSError:
            time.sleep(0.1)
    assert current_logfiles is not None

    print(f"# current_logfiles = {current_logfiles}")

    assert re.search(
        r"^stderr log/postgresql-.*log\n"
        r"csvlog log/postgresql-.*csv\n"
        r"jsonlog log/postgresql-.*json$",
        current_logfiles,
    ), "current_logfiles is sane"

    check_log_pattern("stderr", current_logfiles, "division by zero", node)
    check_log_pattern("csvlog", current_logfiles, "division by zero", node)
    check_log_pattern("jsonlog", current_logfiles, "division by zero", node)

    # Sleep 2 seconds and ask for log rotation; this should result in
    # output into a different log file name.
    time.sleep(2)
    node.command_ok(["pg_ctl", "logrotate", "-D", node.data_dir])

    # pg_ctl logrotate doesn't wait for rotation request to be completed.
    # Allow a bit of time for it to happen.
    new_current_logfiles = None
    for _ in range(max_attempts):
        new_current_logfiles = slurp_file(
            os.path.join(node.data_dir, "current_logfiles")
        )
        if new_current_logfiles != current_logfiles:
            break
        time.sleep(0.1)

    print(f"# now current_logfiles = {new_current_logfiles}")

    assert re.search(
        r"^stderr log/postgresql-.*log\n"
        r"csvlog log/postgresql-.*csv\n"
        r"jsonlog log/postgresql-.*json$",
        new_current_logfiles,
    ), "new current_logfiles is sane"

    # Verify that log output gets to this file, too
    node.sql("fee fi fo fum")

    check_log_pattern("stderr", new_current_logfiles, "syntax error", node)
    check_log_pattern("csvlog", new_current_logfiles, "syntax error", node)
    check_log_pattern("jsonlog", new_current_logfiles, "syntax error", node)

    node.stop()
