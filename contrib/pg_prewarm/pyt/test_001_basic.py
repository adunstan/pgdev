# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_prewarm's autoprewarm feature: dump and restore of the buffer pool."""

import re

from libpq import Session
from pypg.util import TIMEOUT_DEFAULT, slurp_file, poll_until


def _wait_for_log(node, pattern, offset=0, timeout=TIMEOUT_DEFAULT):
    """Poll the server log until *pattern* appears at/after *offset*."""
    regex = re.compile(pattern)

    def _found():
        try:
            content = slurp_file(node.logfile, offset)
        except FileNotFoundError:
            return False
        return regex.search(content) is not None

    assert poll_until(_found, timeout=timeout), (
        f"timed out waiting for log pattern {pattern!r}"
    )


def test_pg_prewarm(create_pg):
    node = create_pg("main", start=False)
    node.append_conf(
        "shared_preload_libraries = 'pg_prewarm'\n"
        "pg_prewarm.autoprewarm = true\n"
        "pg_prewarm.autoprewarm_interval = 0"
    )
    node.start()

    # setup
    node.safe_sql(
        "CREATE EXTENSION pg_prewarm;\n"
        "CREATE TABLE test(c1 int);\n"
        "INSERT INTO test SELECT generate_series(1, 100);\n"
        "CREATE INDEX test_idx ON test(c1);\n"
        "CREATE ROLE test_user LOGIN;"
    )

    # test read mode
    result = node.safe_sql("SELECT pg_prewarm('test', 'read');")
    assert re.match(r"^[1-9][0-9]*$", result), "read mode succeeded"

    # test buffer_mode
    result = node.safe_sql("SELECT pg_prewarm('test', 'buffer');")
    assert re.match(r"^[1-9][0-9]*$", result), "buffer mode succeeded"

    # prefetch mode might or might not be available
    res = node.sql("SELECT pg_prewarm('test', 'prefetch');")
    stdout = res.psqlout
    stderr = res.error_message or ""
    assert re.match(r"^[1-9][0-9]*$", stdout) or re.search(
        r"prefetch is not supported by this build", stderr
    ), "prefetch mode succeeded"

    # test_user should be unable to prewarm table/index without privileges
    user_sess = Session(
        connstr=node.connstr() + " user='test_user'", libdir=node.libdir
    )
    res = user_sess.query("SELECT pg_prewarm('test');")
    assert re.search(
        r"permission denied for table test", res.error_message or ""
    ), "pg_prewarm failed as expected"
    res = user_sess.query("SELECT pg_prewarm('test_idx');")
    assert re.search(
        r"permission denied for index test_idx", res.error_message or ""
    ), "pg_prewarm failed as expected"

    # test_user should be able to prewarm table/index with privileges
    node.safe_sql("GRANT SELECT ON test TO test_user;")
    result = user_sess.query_safe("SELECT pg_prewarm('test');")
    assert re.match(r"^[1-9][0-9]*$", result), "pg_prewarm succeeded as expected"
    result = user_sess.query_safe("SELECT pg_prewarm('test_idx');")
    assert re.match(r"^[1-9][0-9]*$", result), "pg_prewarm succeeded as expected"
    user_sess.close()

    # test autoprewarm_dump_now()
    result = node.safe_sql("SELECT autoprewarm_dump_now();")
    assert re.match(r"^[1-9][0-9]*$", result), "autoprewarm_dump_now succeeded"

    # restart, to verify that auto prewarm actually works
    node.restart()

    _wait_for_log(
        node,
        r"autoprewarm successfully prewarmed [1-9][0-9]* of [0-9]+ "
        r"previously-loaded blocks",
    )

    node.stop()

    # control file should indicate normal shut down
    node.command_like(
        ["pg_controldata", node.data_dir],
        re.compile(r"Database cluster state:\s*shut down"),
        "cluster shut down normally",
    )
