# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Exercise the read_stream / AIO machinery.

Drive the AIO code paths via the SQL functions provided by the test_aio
extension, across each supported io_method.
"""

import os
import re
import subprocess

import pytest

# -- AIO test helpers --------------------------------------------------------


def configure(node):
    """Prepare a cluster for AIO tests."""
    node.append_conf(
        "\n".join(
            [
                "shared_preload_libraries=test_aio",
                "log_min_messages = 'DEBUG3'",
                "log_statement=all",
                "log_error_verbosity=default",
                "restart_after_crash=false",
                "temp_buffers=100",
                "",
            ]
        )
    )


def have_io_uring(bindir, libdir):
    """Detect whether io_uring is a supported io_method.

    Detect io_uring support by inspecting the list of valid io_method values
    reported when assigning an invalid value to the enum GUC.  ``-C`` is used
    so the superuser check is skipped.
    """
    env = dict(os.environ)
    if libdir:
        env["LD_LIBRARY_PATH"] = libdir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    postgres = os.path.join(bindir, "postgres")
    proc = subprocess.run(
        [postgres, "-C", "invalid", "-c", "io_method=invalid"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    out = proc.stdout

    m = re.search(r"Available values: ([^\.]+)\.", out)
    if not m:
        raise RuntimeError("can't determine supported io_method values")
    return "io_uring" in m.group(1)


def supported_io_methods(bindir, libdir):
    """Return the list of io_method values supported by this build."""
    methods = ["worker"]
    if have_io_uring(bindir, libdir):
        methods.append("io_uring")
    # Return sync last, as it will least commonly fail.
    methods.append("sync")
    return methods


# -- per-io_method test bodies ----------------------------------------------


def do_setup(node):
    """Create the extension and a large-ish table."""
    node.safe_sql(
        "CREATE EXTENSION test_aio;\n"
        "\n"
        "CREATE TABLE largeish(k int not null) WITH (FILLFACTOR=10);\n"
        "INSERT INTO largeish(k) SELECT generate_series(1, 10000);\n"
    )


def do_repeated_blocks(io_method, node):
    """Test repeated reads of the same blocks via the read stream."""
    psql = node.connect()
    try:
        # Preventing larger reads makes testing easier.
        psql.query_safe("SET io_combine_limit = 1")

        # test miss of the same block twice in a row
        psql.query_safe("SELECT evict_rel('largeish');")

        # block 0 grows the distance enough that the stream will look ahead and
        # try to start a pending read for block 2 (and later block 4) twice
        # before returning any buffers.
        psql.query_safe(
            "SELECT * FROM read_stream_for_blocks('largeish', ARRAY[0, 2, 2, 4, 4]);"
        )

        psql.query_safe(
            "SELECT * FROM read_stream_for_blocks('largeish', ARRAY[0, 2, 2, 4, 4]);"
        )

        # test hit of the same block twice in a row
        psql.query_safe("SELECT evict_rel('largeish');")
        psql.query_safe(
            "SELECT * FROM read_stream_for_blocks('largeish', "
            "ARRAY[0, 1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1, 0]);"
        )

        # Test repeated blocks with a temp table, using invalidate_rel_block()
        # to evict individual local buffers.
        psql.query_safe(
            "CREATE TEMP TABLE largeish_temp(k int not null) "
            "WITH (FILLFACTOR=10);\n"
            "INSERT INTO largeish_temp(k) SELECT generate_series(1, 200);\n"
        )

        # Evict the specific blocks we'll request to force misses.
        psql.query_safe("SELECT invalidate_rel_block('largeish_temp', 0);")
        psql.query_safe("SELECT invalidate_rel_block('largeish_temp', 2);")
        psql.query_safe("SELECT invalidate_rel_block('largeish_temp', 4);")

        psql.query_safe(
            "SELECT * FROM read_stream_for_blocks('largeish_temp', "
            "ARRAY[0, 2, 2, 4, 4]);"
        )

        # Now the blocks are cached, so repeated access should be hits.
        psql.query_safe(
            "SELECT * FROM read_stream_for_blocks('largeish_temp', "
            "ARRAY[0, 2, 2, 4, 4]);"
        )
    finally:
        psql.close()


def do_inject_foreign(io_method, node):
    """Test a read stream encountering buffers undergoing IO in another backend."""
    psql_a = node.connect()
    psql_b = node.connect()
    try:
        pid_a = psql_a.query_oneval("SELECT pg_backend_pid();")

        #
        # Test read stream encountering buffers undergoing IO in another
        # backend, with the other backend's reads succeeding.
        #
        psql_a.query_safe("SELECT evict_rel('largeish');")

        psql_b.query_safe(
            "SELECT inj_io_completion_wait(pid=>pg_backend_pid(), "
            "relfilenode=>pg_relation_filenode('largeish'));"
        )

        psql_b.do_async("SELECT read_rel_block_ll('largeish', blockno=>5, nblocks=>1);")

        assert node.poll_query_until(
            "SELECT wait_event FROM pg_stat_activity "
            "WHERE wait_event = 'completion_wait';",
            "completion_wait",
        )

        # Block 5 is undergoing IO in session b, so session a will move on to
        # start a new IO for block 7.
        psql_a.do_async(
            "SELECT array_agg(blocknum) FROM "
            "read_stream_for_blocks('largeish', ARRAY[0, 2, 5, 7]);"
        )

        assert node.poll_query_until(
            f"SELECT wait_event FROM pg_stat_activity WHERE pid = {pid_a}",
            "AioIoCompletion",
        )

        node.safe_sql("SELECT inj_io_completion_continue()")

        assert re.search(
            r"\{0,2,5,7\}", psql_a.get_async_result().psqlout
        ), f"{io_method}: read stream encounters succeeding IO by another backend"

        # Drain session b's now-completed low-level read before reusing it.
        psql_b.wait_for_completion()

        #
        # Test read stream encountering buffers undergoing IO in another
        # backend, with the other backend's reads failing.
        #
        psql_a.query_safe("SELECT evict_rel('largeish');")

        psql_b.query_safe(
            "SELECT inj_io_completion_wait(pid=>pg_backend_pid(), "
            "relfilenode=>pg_relation_filenode('largeish'));"
        )

        psql_b.query_safe(
            "SELECT inj_io_short_read_attach(-errno_from_string('EIO'), "
            "pid=>pg_backend_pid(), "
            "relfilenode=>pg_relation_filenode('largeish'));"
        )

        psql_b.do_async("SELECT read_rel_block_ll('largeish', blockno=>5, nblocks=>1);")

        assert node.poll_query_until(
            "SELECT wait_event FROM pg_stat_activity "
            "WHERE wait_event = 'completion_wait';",
            "completion_wait",
        )

        psql_a.do_async(
            "SELECT array_agg(blocknum) FROM "
            "read_stream_for_blocks('largeish', ARRAY[0, 2, 5, 7]);"
        )

        assert node.poll_query_until(
            f"SELECT wait_event FROM pg_stat_activity WHERE pid = {pid_a}",
            "AioIoCompletion",
        )

        node.safe_sql("SELECT inj_io_completion_continue()")

        assert re.search(
            r"\{0,2,5,7\}", psql_a.get_async_result().psqlout
        ), f"{io_method}: read stream encounters failing IO by another backend"

        # Session b's low-level read hits the injected error.
        res_b = psql_b.get_async_result()

        assert res_b.error_message is not None and re.search(
            r"ERROR.*could not read blocks 5\.\.5", res_b.error_message
        ), f"{io_method}: injected error occurred (got {res_b.error_message!r})"
        psql_b.clear_stderr()
        psql_b.query_safe("SELECT inj_io_short_read_detach();")

        #
        # Test read stream encountering two buffers that are undergoing the
        # same IO, started by another backend.
        #
        psql_a.query_safe("SELECT evict_rel('largeish');")

        psql_b.query_safe(
            "SELECT inj_io_completion_wait(pid=>pg_backend_pid(), "
            "relfilenode=>pg_relation_filenode('largeish'));"
        )

        psql_b.do_async("SELECT read_rel_block_ll('largeish', blockno=>2, nblocks=>3);")

        assert node.poll_query_until(
            "SELECT wait_event FROM pg_stat_activity "
            "WHERE wait_event = 'completion_wait';",
            "completion_wait",
        )

        # Blocks 2 and 4 are undergoing IO initiated by session b.
        psql_a.do_async(
            "SELECT array_agg(blocknum) FROM "
            "read_stream_for_blocks('largeish', ARRAY[0, 2, 4]);"
        )

        assert node.poll_query_until(
            f"SELECT wait_event FROM pg_stat_activity WHERE pid = {pid_a}",
            "AioIoCompletion",
        )

        node.safe_sql("SELECT inj_io_completion_continue()")

        assert re.search(
            r"\{0,2,4\}", psql_a.get_async_result().psqlout
        ), f"{io_method}: read stream encounters two buffer read in one IO"

        # Drain session b's now-completed low-level read.
        psql_b.wait_for_completion()
    finally:
        psql_a.close()
        psql_b.close()


def run_io_method(io_method, node, injection_points_available):
    """Run all sub-tests for a node configured with a given io_method."""
    assert (
        node.safe_sql("SHOW io_method") == io_method
    ), f"{io_method}: io_method set correctly"

    do_repeated_blocks(io_method, node)

    if not injection_points_available:
        return
    do_inject_foreign(io_method, node)


# -- entrypoint --------------------------------------------------------------


def test_004_read_stream(create_pg, bindir, libdir):
    node = create_pg("test", start=False)

    configure(node)

    node.append_conf("\n".join(["max_connections=8", "io_method=worker", ""]))

    node.start()

    # Skip faithfully if the test_aio module is not installed in this install.
    if (
        node.safe_sql(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'test_aio'"
        )
        == "0"
    ):
        node.stop()
        pytest.skip("Extension test_aio not installed")

    do_setup(node)

    # The foreign-injection sub-test is gated on the enable_injection_points
    # build flag.  An injection-points build installs the injection_points extension,
    # which the in-tree install always provides; treat its availability as the
    # signal that injection points are usable.
    injection_points_available = (
        node.safe_sql(
            "SELECT count(*) > 0 FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        == "t"
    )
    if (
        os.environ.get("enable_injection_points", "no") != "yes"
        and not injection_points_available
    ):
        injection_points_available = False

    node.stop()

    for method in supported_io_methods(bindir, libdir):
        # adjust_conf(io_method): later values win, so re-appending overrides
        # the io_method=worker set above.
        node.append_conf(f"io_method={method}\n")
        node.start()
        run_io_method(method, node, injection_points_available)
        node.stop()
