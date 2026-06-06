# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Exercise the AIO subsystem extensively.

Load the test_aio extension and drive the AIO subsystem across each supported
io_method (worker, io_uring if supported, sync): the IO handle API, batchmode
API, hard error handling, partial reads, zero buffers, checksum failures,
concurrency, StartReadBuffers(), and injection points.

Helper subs are reproduced as module functions (never named ``test_*``) and the
per-io_method loop is realized as a pytest parametrization.
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
        env=env,
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


# -- generic test helpers ----------------------------------------------------


def psql_like(io_method, session, name, sql, expected_stdout, expected_stderr):
    """Run *sql* and assert its stdout and stderr match expectations.

    Run *sql* on *session*, assert the stdout matches *expected_stdout* and the
    captured stderr (notices + last error) matches *expected_stderr*, then
    clear stderr.  Returns the stdout.
    """
    res = session.query(sql)
    output = res.psqlout

    assert re.search(
        expected_stdout, output
    ), f"{io_method}: {name}: expected stdout /{expected_stdout}/, got {output!r}"
    stderr = session.get_stderr()
    assert re.search(
        expected_stderr, stderr
    ), f"{io_method}: {name}: expected stderr /{expected_stderr}/, got {stderr!r}"
    session.clear_stderr()

    return output


def query_wait_block(
    io_method, node, session, name, sql, waitfor, wait_current_session
):
    """Issue a query asynchronously and wait for a wait event.

    Issue *sql* asynchronously, then poll until *waitfor* wait event is
    observed (in this session's pid, or any session).
    """
    pid = session.backend_pid()

    session.do_async(sql)

    if wait_current_session:
        waitquery = f"SELECT wait_event FROM pg_stat_activity WHERE pid = {pid}"
    else:
        waitquery = (
            "SELECT wait_event FROM pg_stat_activity " f"WHERE wait_event = '{waitfor}'"
        )

    assert node.poll_query_until(
        waitquery, waitfor
    ), f"{io_method}: {name}: observed {waitfor} wait event"


def checksum_failures(session, datname=None):
    """Return checksum failure stats for a database.

    Return (checksum_failures, checksum_last_failure) for *datname*, or for
    shared relations (NULL datname) when *datname* is None.
    """
    if datname is not None:
        cond = f"datname = '{datname}'"
    else:
        cond = "datname IS NULL"

    count = session.query_safe(
        f"SELECT checksum_failures FROM pg_stat_database WHERE {cond};"
    )
    last_failure = session.query_safe(
        f"SELECT checksum_last_failure FROM pg_stat_database WHERE {cond};"
    )
    return count, last_failure


# -- sub-tests ---------------------------------------------------------------


def sub_test_handle(io_method, node):
    """Sanity checks for the IO handle API."""
    psql = node.connect()
    try:
        # leak warning: implicit xact
        psql_like(
            io_method,
            psql,
            "handle_get() leak in implicit xact",
            "SELECT handle_get()",
            r"^$",
            r"leaked AIO handle",
        )

        # leak warning: explicit xact
        psql_like(
            io_method,
            psql,
            "handle_get() leak in explicit xact",
            "BEGIN; SELECT handle_get(); COMMIT",
            r"^$",
            r"leaked AIO handle",
        )

        # leak warning: explicit xact, rollback
        psql_like(
            io_method,
            psql,
            "handle_get() leak in explicit xact, rollback",
            "BEGIN; SELECT handle_get(); ROLLBACK;",
            r"^$",
            r"leaked AIO handle",
        )

        # leak warning: subtrans
        psql_like(
            io_method,
            psql,
            "handle_get() leak in subxact",
            "BEGIN; SAVEPOINT foo; SELECT handle_get(); COMMIT;",
            r"^$",
            r"leaked AIO handle",
        )

        # leak warning + error: released in different command (thus resowner)
        psql_like(
            io_method,
            psql,
            "handle_release() in different command",
            "BEGIN; SELECT handle_get(); SELECT handle_release_last(); COMMIT;",
            r"^$",
            r"(?s)leaked AIO handle.*release in unexpected state",
        )

        # no leak, release in same command
        psql_like(
            io_method,
            psql,
            "handle_release() in same command",
            "BEGIN; SELECT handle_get() UNION ALL SELECT handle_release_last(); COMMIT;",
            r"^$",
            r"^$",
        )

        # normal handle use
        psql_like(
            io_method,
            psql,
            "handle_get_release()",
            "SELECT handle_get_release()",
            r"^$",
            r"^$",
        )

        # should error out, API violation
        psql_like(
            io_method,
            psql,
            "handle_get_twice()",
            "SELECT handle_get_twice()",
            r"^$",
            r"ERROR:  API violation: Only one IO can be handed out$",
        )

        # recover after error in implicit xact
        psql_like(
            io_method,
            psql,
            "handle error recovery in implicit xact",
            "SELECT handle_get_and_error(); SELECT 'ok', handle_get_release()",
            r"^|ok$",
            r"ERROR.*as you command",
        )

        # recover after error in explicit xact
        psql_like(
            io_method,
            psql,
            "handle error recovery in explicit xact",
            "BEGIN; SELECT handle_get_and_error(); SELECT handle_get_release(), 'ok'; COMMIT;",
            r"^|ok$",
            r"ERROR.*as you command",
        )

        # recover after error in subtrans
        psql_like(
            io_method,
            psql,
            "handle error recovery in explicit subxact",
            "BEGIN; SAVEPOINT foo; SELECT handle_get_and_error(); ROLLBACK TO SAVEPOINT foo; SELECT handle_get_release(); ROLLBACK;",
            r"^|ok$",
            r"ERROR.*as you command",
        )
    finally:
        psql.close()


def sub_test_batchmode(io_method, node):
    """Sanity checks for the batchmode API."""
    psql = node.connect()
    try:
        # In a build with RELCACHE_FORCE_RELEASE and CATCACHE_FORCE_RELEASE,
        # just using SELECT batch_start() causes spurious test failures,
        # because the lookup of the type information when printing the result
        # tuple also starts a batch. The easiest way around is to not print a
        # result tuple.
        batch_start_sql = "SELECT WHERE batch_start() IS NULL"

        # leak warning & recovery: implicit xact
        psql_like(
            io_method,
            psql,
            "batch_start() leak & cleanup in implicit xact",
            batch_start_sql,
            r"^$",
            r"open AIO batch at end",
        )

        # leak warning & recovery: explicit xact
        psql_like(
            io_method,
            psql,
            "batch_start() leak & cleanup in explicit xact",
            f"BEGIN; {batch_start_sql}; COMMIT;",
            r"^$",
            r"open AIO batch at end",
        )

        # leak warning & recovery: explicit xact, rollback
        #
        # XXX: This doesn't fail right now, due to not getting a chance to do
        # something at transaction command commit. That's not a correctness
        # issue, it just means it's a bit harder to find buggy code.
        # (left commented out intentionally)

        # no warning, batch closed in same command
        psql_like(
            io_method,
            psql,
            "batch_start(), batch_end() works",
            f"{batch_start_sql} UNION ALL SELECT WHERE batch_end() IS NULL",
            r"^$",
            r"^$",
        )
    finally:
        psql.close()


def sub_test_io_error(io_method, node):
    """Check that simple cases of invalid pages are reported."""
    psql = node.connect()
    try:
        psql.query_safe(
            "CREATE TEMPORARY TABLE tmp_corr(data int not null);\n"
            "INSERT INTO tmp_corr SELECT generate_series(1, 10000);\n"
            "SELECT modify_rel_block('tmp_corr', 1, corrupt_header=>true);\n"
        )

        for tblname in ("tbl_corr", "tmp_corr"):
            if tblname == "tbl_corr":
                invalid_page_re = r'invalid page in block 1 of relation "base/\d+/\d+'
            else:
                invalid_page_re = (
                    r'invalid page in block 1 of relation "base/\d+/t\d+_\d+'
                )

            # verify the error is reported in custom C code
            psql_like(
                io_method,
                psql,
                f"read_rel_block_ll() of {tblname} page",
                f"SELECT read_rel_block_ll('{tblname}', 1)",
                r"^$",
                invalid_page_re,
            )

            # verify the error is reported for bufmgr reads, seq scan
            psql_like(
                io_method,
                psql,
                f"sequential scan of {tblname} block fails",
                f"SELECT count(*) FROM {tblname}",
                r"^$",
                invalid_page_re,
            )

            # verify the error is reported for bufmgr reads, tid scan
            psql_like(
                io_method,
                psql,
                f"tid scan of {tblname} block fails",
                f"SELECT count(*) FROM {tblname} WHERE ctid = '(1, 1)'",
                r"^$",
                invalid_page_re,
            )
    finally:
        psql.close()


def sub_test_startwait_io(io_method, node):
    """Exercise the interplay of StartBufferIO/TerminateBufferIO."""
    psql_a = node.connect()
    psql_b = node.connect()
    try:
        # --- Verify behavior for normal tables ---

        # create a buffer we can play around with
        buf_id = psql_like(
            io_method,
            psql_a,
            "creation of toy buffer succeeds",
            "SELECT buffer_create_toy('tbl_ok', 1)",
            r"^\d+$",
            r"^$",
        )

        # check that one backend can perform StartBufferIO
        psql_like(
            io_method,
            psql_a,
            "first StartBufferIO",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>true);",
            r"^t$",
            r"^$",
        )

        # but not twice on the same buffer (non-waiting)
        psql_like(
            io_method,
            psql_a,
            "second StartBufferIO fails, same session",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>false);",
            r"^f$",
            r"^$",
        )
        psql_like(
            io_method,
            psql_b,
            "second StartBufferIO fails, other session",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>false);",
            r"^f$",
            r"^$",
        )

        # start io in a different session, will block
        query_wait_block(
            io_method,
            node,
            psql_b,
            "blocking start buffer io",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>true);",
            "BufferIo",
            True,
        )

        # Terminate the IO, without marking it as success, this should trigger
        # the waiting session to be able to start the io
        psql_like(
            io_method,
            psql_a,
            "blocking start buffer io, terminating io, not valid",
            f"SELECT buffer_call_terminate_io({buf_id}, for_input=>true, succeed=>false, io_error=>false, release_aio=>false)",
            r"^$",
            r"^$",
        )

        # Because the IO was terminated, but not marked as valid, second
        # session should get the right to start io
        out = psql_b.wait_for_async_pattern(r"t")
        assert re.search(
            r"t", out
        ), f"{io_method}: blocking start buffer io, can start io"

        # terminate the IO again
        psql_b.query_safe(
            f"SELECT buffer_call_terminate_io({buf_id}, for_input=>true, succeed=>false, io_error=>false, release_aio=>false);"
        )

        # same as the above scenario, but mark IO as having succeeded
        psql_like(
            io_method,
            psql_a,
            "blocking buffer io w/ success: first start buffer io",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>true);",
            r"^t$",
            r"^$",
        )

        # start io in a different session, will block
        query_wait_block(
            io_method,
            node,
            psql_b,
            "blocking start buffer io",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>true);",
            "BufferIo",
            True,
        )

        # Terminate the IO, marking it as success
        psql_like(
            io_method,
            psql_a,
            "blocking start buffer io, terminating io, valid",
            f"SELECT buffer_call_terminate_io({buf_id}, for_input=>true, succeed=>true, io_error=>false, release_aio=>false)",
            r"^$",
            r"^$",
        )

        # Because the IO was terminated, and marked as valid, second session
        # should complete but not need io
        out = psql_b.wait_for_async_pattern(r"f")
        assert re.search(
            r"f", out
        ), f"{io_method}: blocking start buffer io, no need to start io"

        # buffer is valid now, make it invalid again
        psql_a.query_safe("SELECT buffer_create_toy('tbl_ok', 1);")

        # --- Verify behavior for temporary tables ---

        # create a buffer we can play around with
        psql_a.query_safe(
            "CREATE TEMPORARY TABLE tmp_ok(data int not null);\n"
            "INSERT INTO tmp_ok SELECT generate_series(1, 10000);\n"
        )
        buf_id = psql_a.query_safe("SELECT buffer_create_toy('tmp_ok', 3);")

        # check that one backend can perform StartLocalBufferIO
        psql_like(
            io_method,
            psql_a,
            "first StartLocalBufferIO",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>false);",
            r"^t$",
            r"^$",
        )

        # Because local buffers don't use IO_IN_PROGRESS, a second
        # StartLocalBufferIO succeeds as well.
        psql_like(
            io_method,
            psql_a,
            "second StartLocalBufferIO succeeds, same session",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>false);",
            r"^t$",
            r"^$",
        )

        # Terminate the IO again, without marking it as a success
        psql_a.query_safe(
            f"SELECT buffer_call_terminate_io({buf_id}, for_input=>true, succeed=>false, io_error=>false, release_aio=>false);"
        )
        psql_like(
            io_method,
            psql_a,
            "StartLocalBufferIO after not marking valid succeeds, same session",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>false);",
            r"^t$",
            r"^$",
        )

        # Terminate the IO again, marking it as a success
        psql_a.query_safe(
            f"SELECT buffer_call_terminate_io({buf_id}, for_input=>true, succeed=>true, io_error=>false, release_aio=>false);"
        )

        # Now another StartLocalBufferIO should fail, this time because the
        # buffer is already valid.
        psql_like(
            io_method,
            psql_a,
            "StartLocalBufferIO after marking valid fails",
            f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>true);",
            r"^f$",
            r"^$",
        )
    finally:
        psql_a.close()
        psql_b.close()


def sub_test_complete_foreign(io_method, node):
    """Test completion of an IO by a backend other than its initiator.

    If the backend issuing a read doesn't wait for the IO's completion,
    another backend can complete the IO.
    """
    psql_a = node.connect()
    psql_b = node.connect()
    try:
        # Issue IO without waiting for completion, then sleep
        psql_a.query_safe(
            "SELECT read_rel_block_ll('tbl_ok', 1, wait_complete=>false);"
        )

        # Check that another backend can read the relevant block
        psql_like(
            io_method,
            psql_b,
            "completing read started by sleeping backend",
            "SELECT count(*) FROM tbl_ok WHERE ctid = '(1,1)' LIMIT 1",
            r"^1$",
            r"^$",
        )

        # Issue IO without waiting for completion, then exit.
        psql_a.query_safe(
            "SELECT read_rel_block_ll('tbl_ok', 1, wait_complete=>false);"
        )
        psql_a.reconnect_and_clear()

        # Check that another backend can read the relevant block. This verifies
        # that the exiting backend left the AIO in a sane state.
        psql_like(
            io_method,
            psql_b,
            "read buffer started by exited backend",
            "SELECT count(*) FROM tbl_ok WHERE ctid = '(1,1)' LIMIT 1",
            r"^1$",
            r"^$",
        )

        # Read a tbl_corr block, then sleep. The other session will retry the
        # IO and also fail.
        log_location = node.log_position()
        psql_a.query_safe(
            "SELECT read_rel_block_ll('tbl_corr', 1, wait_complete=>false);"
        )

        psql_like(
            io_method,
            psql_b,
            "completing read of tbl_corr block started by other backend",
            "SELECT count(*) FROM tbl_corr WHERE ctid = '(1,1)' LIMIT 1",
            r"^$",
            r"invalid page in block",
        )

        # The log message issued for the read_rel_block_ll() should be a LOG
        node.wait_for_log(r"LOG[^\n]+invalid page in", log_location)

        # But for the SELECT, it should be an ERROR
        node.wait_for_log(r"ERROR[^\n]+invalid page in", log_location)
    finally:
        psql_a.close()
        psql_b.close()


def sub_test_close_fd(io_method, node):
    """Test FDs closed while IO is in progress."""
    psql = node.connect()
    try:
        psql_like(
            io_method,
            psql,
            "close all FDs after read, waiting for results",
            "\n\t\t\t\tSELECT read_rel_block_ll('tbl_ok', 1,\n"
            "\t\t\t\t\twait_complete=>true,\n"
            "\t\t\t\t\tbatchmode_enter=>true,\n"
            "\t\t\t\t\tsmgrreleaseall=>true,\n"
            "\t\t\t\t\tbatchmode_exit=>true\n"
            "\t\t\t\t);",
            r"^$",
            r"^$",
        )

        psql_like(
            io_method,
            psql,
            "close all FDs after read, no waiting",
            "\n\t\t\t\tSELECT read_rel_block_ll('tbl_ok', 1,\n"
            "\t\t\t\t\twait_complete=>false,\n"
            "\t\t\t\t\tbatchmode_enter=>true,\n"
            "\t\t\t\t\tsmgrreleaseall=>true,\n"
            "\t\t\t\t\tbatchmode_exit=>true\n"
            "\t\t\t\t);",
            r"^$",
            r"^$",
        )

        # Check that another backend can read the relevant block
        psql_like(
            io_method,
            psql,
            "close all FDs after read, no waiting, query works",
            "SELECT count(*) FROM tbl_ok WHERE ctid = '(1,1)' LIMIT 1",
            r"^1$",
            r"^$",
        )
    finally:
        psql.close()


def sub_test_inject(io_method, node):
    """Exercise hard IO errors via injection points."""
    psql = node.connect()
    try:
        # injected what we'd expect
        psql.query_safe("SELECT inj_io_short_read_attach(8192);")
        psql.query_safe("SELECT invalidate_rel_block('tbl_ok', 2);")
        psql_like(
            io_method,
            psql,
            "injection point not triggering failure",
            "SELECT count(*) FROM tbl_ok WHERE ctid = '(2, 1)'",
            r"^1$",
            r"^$",
        )

        # injected a read shorter than a single block, expecting error
        psql.query_safe("SELECT inj_io_short_read_attach(17);")
        psql.query_safe("SELECT invalidate_rel_block('tbl_ok', 2);")
        psql_like(
            io_method,
            psql,
            "single block short read fails",
            "SELECT count(*) FROM tbl_ok WHERE ctid = '(2, 1)'",
            r"^$",
            r'ERROR:.*could not read blocks 2\.\.2 in file "base/.*": read only 0 of 8192 bytes',
        )

        # shorten multi-block read to a single block, should retry
        inval_query = (
            "SELECT invalidate_rel_block('tbl_ok', 0);\n"
            "SELECT invalidate_rel_block('tbl_ok', 1);\n"
            "SELECT invalidate_rel_block('tbl_ok', 2);\n"
            "SELECT invalidate_rel_block('tbl_ok', 3);\n"
            "/* gap */\n"
            "SELECT invalidate_rel_block('tbl_ok', 5);\n"
            "SELECT invalidate_rel_block('tbl_ok', 6);\n"
            "SELECT invalidate_rel_block('tbl_ok', 7);\n"
            "SELECT invalidate_rel_block('tbl_ok', 8);"
        )

        psql.query_safe(inval_query)
        psql.query_safe("SELECT inj_io_short_read_attach(8192);")
        psql_like(
            io_method,
            psql,
            "multi block short read (1 block) is retried",
            "SELECT count(*) FROM tbl_ok",
            r"^10000$",
            r"^$",
        )

        # shorten multi-block read to two blocks, should retry
        psql.query_safe(inval_query)
        psql.query_safe("SELECT inj_io_short_read_attach(8192*2);")
        psql_like(
            io_method,
            psql,
            "multi block short read (2 blocks) is retried",
            "SELECT count(*) FROM tbl_ok",
            r"^10000$",
            r"^$",
        )

        # verify that page verification errors are detected even as part of a
        # shortened multi-block read (tbl_corr, block 1 is corrupted)
        psql.query_safe(
            "SELECT invalidate_rel_block('tbl_corr', 0);\n"
            "SELECT invalidate_rel_block('tbl_corr', 1);\n"
            "SELECT invalidate_rel_block('tbl_corr', 2);\n"
            "SELECT inj_io_short_read_attach(8192);\n"
        )

        psql_like(
            io_method,
            psql,
            "shortened multi-block read detects invalid page",
            "SELECT count(*) FROM tbl_corr WHERE ctid < '(2, 1)'",
            r"^$",
            r'ERROR:.*invalid page in block 1 of relation "base/.*',
        )

        # trigger a hard error, should error out
        psql.query_safe(
            "SELECT inj_io_short_read_attach(-errno_from_string('EIO'));\n"
            "SELECT invalidate_rel_block('tbl_ok', 2);\n"
        )

        psql_like(
            io_method,
            psql,
            "first hard IO error is reported",
            "SELECT count(*) FROM tbl_ok",
            r"^$",
            r'ERROR:.*could not read blocks 2\.\.2 in file "base/.*": (?:I/O|Input/output) error',
        )

        psql_like(
            io_method,
            psql,
            "second hard IO error is reported",
            "SELECT count(*) FROM tbl_ok",
            r"^$",
            r'ERROR:.*could not read blocks 2\.\.2 in file "base/.*": (?:I/O|Input/output) error',
        )

        psql.query_safe("SELECT inj_io_short_read_detach()")

        # now the IO should be ok.
        psql_like(
            io_method,
            psql,
            "recovers after hard error",
            "SELECT count(*) FROM tbl_ok",
            r"^10000$",
            r"^$",
        )

        # trigger a different hard error, should error out
        psql.query_safe(
            "SELECT inj_io_short_read_attach(-errno_from_string('EROFS'));\n"
            "SELECT invalidate_rel_block('tbl_ok', 2);\n"
        )
        psql_like(
            io_method,
            psql,
            "different hard IO error is reported",
            "SELECT count(*) FROM tbl_ok",
            r"^$",
            r'ERROR:.*could not read blocks 2\.\.2 in file "base/.*": Read-only file system',
        )
        psql.query_safe("SELECT inj_io_short_read_detach()")
    finally:
        psql.close()


def sub_test_inject_worker(io_method, node):
    """Test worker-only reopen-failure injection."""
    psql = node.connect()
    try:
        # trigger a failure to reopen, should error out, but should recover
        psql.query_safe(
            "SELECT inj_io_reopen_attach();\n"
            "SELECT invalidate_rel_block('tbl_ok', 1);\n"
        )

        psql_like(
            io_method,
            psql,
            "failure to open: detected",
            "SELECT count(*) FROM tbl_ok",
            r"^$",
            r'ERROR:.*could not read blocks 1\.\.1 in file "base/.*": No such file or directory',
        )

        psql.query_safe("SELECT inj_io_reopen_detach();")

        # check that we indeed recover
        psql_like(
            io_method,
            psql,
            "failure to open: recovers",
            "SELECT count(*) FROM tbl_ok",
            r"^10000$",
            r"^$",
        )
    finally:
        psql.close()


def sub_test_invalidate(io_method, node):
    """Test buffer invalidation while IO is in progress.

    Relation getting removed (rollback or DROP TABLE) while IO is ongoing.
    """
    psql = node.connect()
    try:
        for persistency in ("normal", "unlogged", "temporary"):
            sql_persistency = "" if persistency == "normal" else persistency
            tblname = persistency + "_transactional"

            create_sql = (
                f"CREATE {sql_persistency} TABLE {tblname} "
                "(id int not null, data text not null) "
                "WITH (AUTOVACUUM_ENABLED = false);\n"
                f"INSERT INTO {tblname}(id, data) "
                "SELECT generate_series(1, 10000) as id, repeat('a', 200);\n"
            )

            # Verify that outstanding read IO does not cause problems with
            # AbortTransaction -> smgrDoPendingDeletes -> smgrdounlinkall ->
            # ... -> Invalidate[Local]Buffer.
            psql.query_safe(f"BEGIN; {create_sql};")
            psql.query_safe(
                f"SELECT read_rel_block_ll('{tblname}', 1, wait_complete=>false);\n"
            )
            psql_like(
                io_method,
                psql,
                f"rollback of newly created {persistency} table with outstanding IO",
                "ROLLBACK",
                r"^$",
                r"^$",
            )

            # Verify that outstanding read IO does not cause problems with
            # CommitTransaction -> smgrDoPendingDeletes -> smgrdounlinkall ->
            # ... -> Invalidate[Local]Buffer.
            psql.query_safe(f"BEGIN; {create_sql}; COMMIT;")
            psql.query_safe(
                "BEGIN;\n"
                f"SELECT read_rel_block_ll('{tblname}', 1, wait_complete=>false);\n"
            )

            psql_like(
                io_method,
                psql,
                f"drop {persistency} table with outstanding IO",
                f"DROP TABLE {tblname}",
                r"^$",
                r"^$",
            )

            psql_like(
                io_method,
                psql,
                f"commit after drop {persistency} table with outstanding IO",
                "COMMIT",
                r"^$",
                r"^$",
            )
    finally:
        psql.close()


def sub_test_zero(io_method, node):
    """Test ZERO_ON_ERROR and zero_damaged_pages behavior."""
    psql_a = node.connect()
    psql_b = node.connect()
    try:
        for persistency in ("normal", "temporary"):
            sql_persistency = "" if persistency == "normal" else persistency

            psql_a.query_safe(
                f"CREATE {sql_persistency} TABLE tbl_zero(id int) "
                "WITH (AUTOVACUUM_ENABLED = false);\n"
                "INSERT INTO tbl_zero SELECT generate_series(1, 10000);\n"
            )

            psql_a.query_safe(
                "SELECT modify_rel_block('tbl_zero', 0, corrupt_header=>true);\n"
            )

            # Check that page validity errors are detected
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: test reading of invalid block 0",
                "\nSELECT read_rel_block_ll('tbl_zero', 0, zero_on_error=>false)",
                r"^$",
                r'^(?:psql:<stdin>:\d+: )?ERROR:  invalid page in block 0 of relation "base/.*/.*$',
            )

            # Check that page validity errors are zeroed
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: test zeroing of invalid block 0",
                "\nSELECT read_rel_block_ll('tbl_zero', 0, zero_on_error=>true)",
                r"^$",
                r'^(?:psql:<stdin>:\d+: )?WARNING:  invalid page in block 0 of relation "base/.*/.*"; zeroing out page$',
            )

            # And that once the corruption is fixed, we can read again
            psql_a.query("SELECT modify_rel_block('tbl_zero', 0, zero=>true);\n")
            psql_a.clear_stderr()

            psql_like(
                io_method,
                psql_a,
                f"{persistency}: test re-read of block 0",
                "\nSELECT read_rel_block_ll('tbl_zero', 0, zero_on_error=>false)",
                r"^$",
                r"^$",
            )

            # Check a page validity error in another block, to ensure we report
            # the correct block number
            psql_a.query_safe(
                "SELECT modify_rel_block('tbl_zero', 3, corrupt_header=>true);\n"
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: test zeroing of invalid block 3",
                "SELECT read_rel_block_ll('tbl_zero', 3, zero_on_error=>true);",
                r"^$",
                r'^(?:psql:<stdin>:\d+: )?WARNING:  invalid page in block 3 of relation "base/.*/.*"; zeroing out page$',
            )

            # Check one read reporting multiple invalid blocks
            psql_a.query_safe(
                "SELECT modify_rel_block('tbl_zero', 2, corrupt_header=>true);\n"
                "SELECT modify_rel_block('tbl_zero', 3, corrupt_header=>true);\n"
            )
            # First test error
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: test reading of invalid block 2,3 in larger read",
                "SELECT read_rel_block_ll('tbl_zero', 1, nblocks=>4, zero_on_error=>false)",
                r"^$",
                r'^(?:psql:<stdin>:\d+: )?ERROR:  2 invalid pages among blocks 1..4 of relation "base/.*/.*\nDETAIL:  Block 2 held the first invalid page\.\nHINT:[^\n]+$',
            )

            # Then test zeroing via ZERO_ON_ERROR flag
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: test zeroing of invalid block 2,3 in larger read, ZERO_ON_ERROR",
                "SELECT read_rel_block_ll('tbl_zero', 1, nblocks=>4, zero_on_error=>true)",
                r"^$",
                r'^(?:psql:<stdin>:\d+: )?WARNING:  zeroing out 2 invalid pages among blocks 1..4 of relation "base/.*/.*\nDETAIL:  Block 2 held the first zeroed page\.\nHINT:[^\n]+$',
            )

            # Then test zeroing via zero_damaged_pages
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: test zeroing of invalid block 2,3 in larger read, zero_damaged_pages",
                "\nBEGIN;\n"
                "SET LOCAL zero_damaged_pages = true;\n"
                "SELECT read_rel_block_ll('tbl_zero', 1, nblocks=>4, zero_on_error=>false)\n"
                "COMMIT;\n",
                r"^$",
                r'^(?:psql:<stdin>:\d+: )?WARNING:  zeroing out 2 invalid pages among blocks 1..4 of relation "base/.*/.*\nDETAIL:  Block 2 held the first zeroed page\.\nHINT:[^\n]+$',
            )

            psql_a.query_safe("COMMIT")

            # Verify that bufmgr.c IO detects page validity errors
            psql_a.query(
                "SELECT invalidate_rel_block('tbl_zero', g.i)\n"
                "FROM generate_series(0, 15) g(i);\n"
                "SELECT modify_rel_block('tbl_zero', 3, zero=>true);\n"
            )
            psql_a.clear_stderr()

            psql_like(
                io_method,
                psql_a,
                f"{persistency}: verify reading zero_damaged_pages=off",
                "\nSELECT count(*) FROM tbl_zero",
                r"^$",
                r'^(?:psql:<stdin>:\d+: )?ERROR:  invalid page in block 2 of relation "base/.*/.*$',
            )

            # Verify that bufmgr.c IO zeroes out pages with page validity errors
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: verify zero_damaged_pages=on",
                "\nBEGIN;\n"
                "SET LOCAL zero_damaged_pages = true;\n"
                "SELECT count(*) FROM tbl_zero;\n"
                "COMMIT;\n",
                r"^\d+$",
                r'^(?:psql:<stdin>:\d+: )?WARNING:  invalid page in block 2 of relation "base/.*/.*$',
            )

            # Check that warnings/errors about page validity in an IO started by
            # session A that session B might complete aren't logged visibly to
            # session B.
            #
            # This requires cross-session access to the same relation, hence
            # the restriction to non-temporary table.
            if sql_persistency != "temporary":
                # Create a corruption and then read the block without waiting
                # for completion.
                psql_a.query(
                    "SELECT modify_rel_block('tbl_zero', 1, corrupt_header=>true);\n"
                    "SELECT read_rel_block_ll('tbl_zero', 1, wait_complete=>false, zero_on_error=>true)\n"
                )

                psql_like(
                    io_method,
                    psql_b,
                    f"{persistency}: test completing read by other session doesn't generate warning",
                    "SELECT count(*) > 0 FROM tbl_zero;",
                    r"^t$",
                    r"^$",
                )

            # Clean up
            psql_a.query_safe("DROP TABLE tbl_zero;\n")

        psql_a.clear_stderr()
    finally:
        psql_a.close()
        psql_b.close()


def sub_test_checksum(io_method, node):
    """Detect checksum failures and report them."""
    psql_a = node.connect()
    try:
        # Split multi-statement query into separate calls to match psql
        # behavior where errors in one statement don't prevent subsequent
        # statements.
        psql_a.query_safe(
            "CREATE TABLE tbl_normal(id int) WITH (AUTOVACUUM_ENABLED = false)"
        )
        psql_a.query_safe("INSERT INTO tbl_normal SELECT generate_series(1, 5000)")
        psql_a.query_safe(
            "SELECT modify_rel_block('tbl_normal', 3, corrupt_checksum=>true)"
        )
        psql_a.query_safe(
            "CREATE TEMPORARY TABLE tbl_temp(id int) WITH (AUTOVACUUM_ENABLED = false)"
        )
        psql_a.query_safe("INSERT INTO tbl_temp SELECT generate_series(1, 5000)")
        psql_a.query_safe(
            "SELECT modify_rel_block('tbl_temp', 3, corrupt_checksum=>true)"
        )
        psql_a.query_safe(
            "SELECT modify_rel_block('tbl_temp', 4, corrupt_checksum=>true)"
        )

        # To be able to test checksum failures on shared rels we need a shared
        # rel with invalid pages - which is a bit scary. pg_shseclabel seems
        # like a good bet, as it's not accessed in a default configuration.
        psql_a.query_safe(
            "SELECT grow_rel('pg_shseclabel', 4);\n"
            "SELECT modify_rel_block('pg_shseclabel', 2, corrupt_checksum=>true);\n"
            "SELECT modify_rel_block('pg_shseclabel', 3, corrupt_checksum=>true);\n"
        )

        # normal rel: page validity errors detected, checksums stats increase
        cs_count_before, _cs_ts_before = checksum_failures(psql_a, "postgres")
        psql_like(
            io_method,
            psql_a,
            "normal rel: test reading of invalid block 3",
            "\nSELECT read_rel_block_ll('tbl_normal', 3, nblocks=>1, zero_on_error=>false);",
            r"^$",
            r'^(?:psql:<stdin>:\d+: )?ERROR:  invalid page in block 3 of relation "base/\d+/\d+"$',
        )

        cs_count_after, cs_ts_after = checksum_failures(psql_a, "postgres")

        assert int(cs_count_before) + 1 <= int(
            cs_count_after
        ), f"{io_method}: normal rel: checksum count increased"
        assert (
            cs_ts_after != ""
        ), f"{io_method}: normal rel: checksum timestamp is not null"

        # temp rel: page validity errors detected, checksums stats increase
        cs_count_after, cs_ts_after = checksum_failures(psql_a, "postgres")
        psql_like(
            io_method,
            psql_a,
            "temp rel: test reading of invalid block 4, valid block 5",
            "\nSELECT read_rel_block_ll('tbl_temp', 4, nblocks=>2, zero_on_error=>false);",
            r"^$",
            r'^(?:psql:<stdin>:\d+: )?ERROR:  invalid page in block 4 of relation "base/\d+/t\d+_\d+"$',
        )

        cs_count_after, cs_ts_after = checksum_failures(psql_a, "postgres")

        assert int(cs_count_before) + 1 <= int(
            cs_count_after
        ), f"{io_method}: temp rel: checksum count increased"
        assert (
            cs_ts_after != ""
        ), f"{io_method}: temp rel: checksum timestamp is not null"

        # shared rel: page validity errors detected, checksums stats increase
        cs_count_before, cs_ts_after = checksum_failures(psql_a)
        psql_like(
            io_method,
            psql_a,
            "shared rel: reading of invalid blocks 2+3",
            "\nSELECT read_rel_block_ll('pg_shseclabel', 2, nblocks=>2, zero_on_error=>false);",
            r"^$",
            r'^(?:psql:<stdin>:\d+: )?ERROR:  2 invalid pages among blocks 2..3 of relation "global/\d+"\nDETAIL:  Block 2 held the first invalid page\.\nHINT:[^\n]+$',
        )

        cs_count_after, cs_ts_after = checksum_failures(psql_a)

        assert int(cs_count_before) + 1 <= int(
            cs_count_after
        ), f"{io_method}: shared rel: checksum count increased"
        assert (
            cs_ts_after != ""
        ), f"{io_method}: shared rel: checksum timestamp is not null"

        # and restore sanity
        psql_a.query(
            "SELECT modify_rel_block('pg_shseclabel', 1, zero=>true);\n"
            "DROP TABLE tbl_normal;\n"
        )
        psql_a.clear_stderr()
    finally:
        psql_a.close()


def sub_test_checksum_createdb(io_method, node):
    """Test checksum handling when creating a database via CREATE DATABASE.

    Checksum handling when creating a database from a database with an invalid
    block.  Also a minimal check that cross-database IO is handled reasonably.
    """
    psql = node.connect()
    try:
        node.safe_sql("CREATE DATABASE regression_createdb_source")

        # CREATE DATABASE ... TEMPLATE below requires no other connection to
        # the source DB.  Use a short-lived connection that disconnects when
        # done, rather than the cached per-dbname session.
        src = node.connect(dbname="regression_createdb_source")
        try:
            src.query_safe(
                "CREATE EXTENSION test_aio;\n"
                "CREATE TABLE tbl_cs_fail(data int not null) "
                "WITH (AUTOVACUUM_ENABLED = false);\n"
                "INSERT INTO tbl_cs_fail SELECT generate_series(1, 1000);\n"
                "SELECT modify_rel_block('tbl_cs_fail', 1, corrupt_checksum=>true);\n"
            )
        finally:
            src.close()

        createdb_sql = (
            "\nCREATE DATABASE regression_createdb_target\n"
            "TEMPLATE regression_createdb_source\n"
            "STRATEGY wal_log;\n"
        )

        # Verify that CREATE DATABASE of an invalid database fails and is
        # accounted for accurately.
        cs_count_before, _cs_ts_before = checksum_failures(
            psql, "regression_createdb_source"
        )
        psql_like(
            io_method,
            psql,
            "create database w/ wal strategy, invalid source",
            createdb_sql,
            r"^$",
            r'^(?:psql:<stdin>:\d+: )?ERROR:  invalid page in block 1 of relation "base/\d+/\d+"$',
        )
        cs_count_after, _cs_ts_after = checksum_failures(
            psql, "regression_createdb_source"
        )
        assert int(cs_count_before) + 1 <= int(cs_count_after), (
            f"{io_method}: create database w/ wal strategy, invalid source: "
            "checksum count increased"
        )

        # Verify that CREATE DATABASE of the fixed database succeeds.  Again
        # use a transient connection so the source DB has no other session.
        src = node.connect(dbname="regression_createdb_source")
        try:
            src.query_safe("SELECT modify_rel_block('tbl_cs_fail', 1, zero=>true);\n")
        finally:
            src.close()
        psql_like(
            io_method,
            psql,
            "create database w/ wal strategy, valid source",
            createdb_sql,
            r"^$",
            r"^$",
        )
    finally:
        psql.close()


def sub_test_ignore_checksum(io_method, node):
    """Detect and report checksum failures with ignore_checksum_failure.

    In several places we make sure the server log contains individual
    information for each block involved in the IO.
    """
    psql = node.connect()
    try:
        # Test setup
        psql.query_safe(
            "CREATE TABLE tbl_cs_fail(id int) WITH (AUTOVACUUM_ENABLED = false);\n"
            "INSERT INTO tbl_cs_fail SELECT generate_series(1, 10000);\n"
        )

        count_sql = "SELECT count(*) FROM tbl_cs_fail"
        invalidate_sql = (
            "\nSELECT invalidate_rel_block('tbl_cs_fail', g.i)\n"
            "FROM generate_series(0, 6) g(i);\n"
        )

        expect = psql.query_safe(count_sql)

        # Very basic tests for ignore_checksum_failure=off / on
        psql.query_safe(
            "SELECT modify_rel_block('tbl_cs_fail', 1, corrupt_checksum=>true);\n"
            "SELECT modify_rel_block('tbl_cs_fail', 5, corrupt_checksum=>true);\n"
            "SELECT modify_rel_block('tbl_cs_fail', 6, corrupt_checksum=>true);\n"
        )

        psql.query_safe(invalidate_sql)
        psql_like(
            io_method,
            psql,
            "reading block w/ wrong checksum with ignore_checksum_failure=off fails",
            count_sql,
            r"^$",
            r"ERROR:  invalid page in block",
        )

        psql.query_safe("SET ignore_checksum_failure=on")

        psql.query_safe(invalidate_sql)
        psql_like(
            io_method,
            psql,
            "reading block w/ wrong checksum with ignore_checksum_failure=off succeeds",
            count_sql,
            rf"^{expect}$",
            r"WARNING:  ignoring (checksum failure|\d checksum failures)",
        )

        # Verify that ignore_checksum_failure=off works in multi-block reads
        psql.query_safe(
            "SELECT modify_rel_block('tbl_cs_fail', 2, zero=>true);\n"
            "SELECT modify_rel_block('tbl_cs_fail', 3, corrupt_checksum=>true);\n"
            "SELECT modify_rel_block('tbl_cs_fail', 4, corrupt_header=>true);\n"
        )

        log_location = node.log_position()
        psql_like(
            io_method,
            psql,
            "test reading of checksum failed block 3, with ignore",
            "\nSELECT read_rel_block_ll('tbl_cs_fail', 3, nblocks=>1, zero_on_error=>false);",
            r"^$",
            r"^(?:psql:<stdin>:\d+: )?WARNING:  ignoring checksum failure in block 3",
        )

        # Check that the log contains a LOG message about the failure
        log_location = node.wait_for_log(
            r"LOG:  ignoring checksum failure", log_location
        )

        # check that we error
        psql_like(
            io_method,
            psql,
            "test reading of valid block 2, checksum failed 3, invalid 4, zero=false with ignore",
            "\nSELECT read_rel_block_ll('tbl_cs_fail', 2, nblocks=>3, zero_on_error=>false);",
            r"^$",
            r'^(?:psql:<stdin>:\d+: )?ERROR:  invalid page in block 4 of relation "base/\d+/\d+"$',
        )

        # Test multi-block read with different problems in different blocks
        psql.query(
            "SELECT modify_rel_block('tbl_cs_fail', 1, zero=>true);\n"
            "SELECT modify_rel_block('tbl_cs_fail', 2, corrupt_checksum=>true);\n"
            "SELECT modify_rel_block('tbl_cs_fail', 3, corrupt_checksum=>true, corrupt_header=>true);\n"
            "SELECT modify_rel_block('tbl_cs_fail', 4, corrupt_header=>true);\n"
            "SELECT modify_rel_block('tbl_cs_fail', 5, corrupt_header=>true);\n"
        )
        psql.clear_stderr()

        log_location = node.log_position()
        psql_like(
            io_method,
            psql,
            "test reading of valid block 1, checksum failed 2, 3, invalid 3-5, zero=true",
            "\nSELECT read_rel_block_ll('tbl_cs_fail', 1, nblocks=>5, zero_on_error=>true);",
            r"^$",
            r'^(?:psql:<stdin>:\d+: )?WARNING:  zeroing 3 page\(s\) and ignoring 2 checksum failure\(s\) among blocks 1..5 of relation "',
        )

        # Unfortunately have to scan the whole log since determining
        # log_location above in each of the tests, as wait_for_log() returns
        # the size of the file.
        node.wait_for_log(r"LOG:  ignoring checksum failure in block 2", log_location)

        node.wait_for_log(
            r'LOG:  invalid page in block 3 of relation "base.*"; zeroing out page',
            log_location,
        )

        node.wait_for_log(
            r'LOG:  invalid page in block 4 of relation "base.*"; zeroing out page',
            log_location,
        )

        node.wait_for_log(
            r'LOG:  invalid page in block 5 of relation "base.*"; zeroing out page',
            log_location,
        )

        # Reading a page with both an invalid header and an invalid checksum
        psql.query(
            "SELECT modify_rel_block('tbl_cs_fail', 3, corrupt_checksum=>true, corrupt_header=>true);\n"
        )
        psql.clear_stderr()

        psql_like(
            io_method,
            psql,
            "test reading of block with both invalid header and invalid checksum, zero=false",
            "\nSELECT read_rel_block_ll('tbl_cs_fail', 3, nblocks=>1, zero_on_error=>false);",
            r"^$",
            r'^(?:psql:<stdin>:\d+: )?ERROR:  invalid page in block 3 of relation "',
        )

        psql_like(
            io_method,
            psql,
            "test reading of block 3 with both invalid header and invalid checksum, zero=true",
            "\nSELECT read_rel_block_ll('tbl_cs_fail', 3, nblocks=>1, zero_on_error=>true);",
            r"^$",
            r'^(?:psql:<stdin>:\d+: )?WARNING:  invalid page in block 3 of relation "base/.*"; zeroing out page',
        )
    finally:
        psql.close()


def sub_test_read_buffers(io_method, node):
    """Tests for StartReadBuffers()."""
    psql_a = node.connect()
    psql_b = node.connect()
    try:
        psql_a.query_safe(
            "CREATE TEMPORARY TABLE tmp_ok(data int not null);\n"
            "INSERT INTO tmp_ok SELECT generate_series(1, 5000);\n"
        )

        for persistency in ("normal", "temporary"):
            table = "tbl_ok" if persistency == "normal" else "tmp_ok"

            # check that consecutive misses are combined into one read
            psql_a.query_safe(f"SELECT evict_rel('{table}')")
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, combine, block 0-1",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 0, 2)",
                r"^0\|0\|t\|2$",
                r"^$",
            )

            # but if we do it again, i.e. it's in the buffer pool, there will
            # be two operations
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, doesn't combine hits, block 0-1",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 0, 2)",
                r"^0\|0\|f\|1\n1\|1\|f\|1$",
                r"^$",
            )

            # Check that a larger read interrupted by a hit works
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, prep, block 3",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 3, 1)",
                r"^0\|3\|t\|1$",
                r"^$",
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, interrupted by hit on 3, block 2-5",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 2, 4)",
                r"^0\|2\|t\|1\n1\|3\|f\|1\n2\|4\|t\|2$",
                r"^$",
            )

            # Verify that a read with an initial buffer hit works
            psql_a.query_safe(f"SELECT evict_rel('{table}')")
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, miss, block 0",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 0, 1)",
                r"^0\|0\|t\|1$",
                r"^$",
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, hit, block 0",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 0, 1)",
                r"^0\|0\|f\|1$",
                r"^$",
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, miss, block 1",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 1, 1)",
                r"^0\|1\|t\|1$",
                r"^$",
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, hit, block 1",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 1, 1)",
                r"^0\|1\|f\|1$",
                r"^$",
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, hit, block 0-1",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 0, 2)",
                r"^0\|0\|f\|1\n1\|1\|f\|1$",
                r"^$",
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, hit 0-1, miss 2",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 0, 3)",
                r"^0\|0\|f\|1\n1\|1\|f\|1\n2\|2\|t\|1$",
                r"^$",
            )

            # Verify that a read with an initial miss and trailing hit(s) works
            psql_a.query_safe(f"SELECT invalidate_rel_block('{table}', 0)")
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, miss 0, hit 1-2",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 0, 3)",
                r"^0\|0\|t\|1\n1\|1\|f\|1\n2\|2\|f\|1$",
                r"^$",
            )
            psql_a.query_safe(f"SELECT invalidate_rel_block('{table}', 1)")
            psql_a.query_safe(f"SELECT invalidate_rel_block('{table}', 2)")
            psql_a.query_safe(f"SELECT * FROM read_buffers('{table}', 3, 2)")
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, miss 1-2, hit 3-4",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 1, 4)",
                r"^0\|1\|t\|2\n2\|3\|f\|1\n3\|4\|f\|1$",
                r"^$",
            )

            # Verify that we aren't doing reads larger than io_combine_limit.
            psql_a.query_safe(f"SELECT evict_rel('{table}')")
            psql_a.query_safe("SET io_combine_limit=3")
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, io_combine_limit has effect",
                f"SELECT blockoff, blocknum, io_reqd, nblocks FROM read_buffers('{table}', 1, 5)",
                r"^0\|1\|t\|3\n3\|4\|t\|2$",
                r"^$",
            )
            psql_a.query_safe("RESET io_combine_limit")

            # Test encountering buffer IO we started in the first block of the
            # range.  A foreign IO is treated as not having needed to do IO.
            psql_a.query_safe(f"SELECT evict_rel('{table}')")
            psql_a.query_safe(
                f"SELECT read_rel_block_ll('{table}', 1, wait_complete=>false)"
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, in-progress 1, read 1-3",
                f"SELECT blockoff, blocknum, io_reqd and not foreign_io, nblocks FROM read_buffers('{table}', 1, 3)",
                r"^0\|1\|f\|1\n1\|2\|t\|2$",
                r"^$",
            )

            # Test in-progress IO in the middle block of the range
            psql_a.query_safe(f"SELECT evict_rel('{table}')")
            psql_a.query_safe(
                f"SELECT read_rel_block_ll('{table}', 2, wait_complete=>false)"
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, in-progress 2, read 1-3",
                f"SELECT blockoff, blocknum, io_reqd and not foreign_io, nblocks FROM read_buffers('{table}', 1, 3)",
                r"^0\|1\|t\|1\n1\|2\|f\|1\n2\|3\|t\|1$",
                r"^$",
            )

            # Test in-progress IO on the last block of the range
            psql_a.query_safe(f"SELECT evict_rel('{table}')")
            psql_a.query_safe(
                f"SELECT read_rel_block_ll('{table}', 3, wait_complete=>false)"
            )
            psql_like(
                io_method,
                psql_a,
                f"{persistency}: read buffers, in-progress 3, read 1-3",
                f"SELECT blockoff, blocknum, io_reqd and not foreign_io, nblocks FROM read_buffers('{table}', 1, 3)",
                r"^0\|1\|t\|2\n2\|3\|f\|1$",
                r"^$",
            )

        # The remaining tests don't make sense for temp tables, as they are
        # concerned with multiple sessions interacting with each other.
        table = "tbl_ok"
        persistency = "normal"

        # Test start buffer IO will split IO if there's IO in progress. We
        # can't observe this with sync, as that does not start the IO operation
        # in StartReadBuffers().
        if io_method != "sync":
            psql_a.query_safe(f"SELECT evict_rel('{table}')")

            buf_id = psql_b.query_oneval(f"SELECT buffer_create_toy('{table}', 3)")
            psql_b.query_safe(
                f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>true)"
            )

            query_wait_block(
                io_method,
                node,
                psql_a,
                f"{persistency}: read buffers blocks waiting for concurrent IO",
                f"SELECT blockoff, blocknum, io_reqd, foreign_io, nblocks FROM read_buffers('{table}', 1, 5);\n",
                "BufferIo",
                True,
            )
            psql_b.query_safe(
                f"SELECT buffer_call_terminate_io({buf_id}, for_input=>true, succeed=>false, io_error=>false, release_aio=>false)"
            )
            # Because no IO wref was assigned, block 3 should not report
            # foreign IO
            expected = r"0\|1\|t\|f\|2\n2\|3\|t\|f\|3"
            out = psql_a.wait_for_async_pattern(expected)
            assert re.search(
                expected, out
            ), f"{io_method}: {persistency}: IO was split due to concurrent failed IO"

            # Same as before, except the concurrent IO succeeds this time
            psql_a.query_safe(f"SELECT evict_rel('{table}')")
            buf_id = psql_b.query_oneval(f"SELECT buffer_create_toy('{table}', 3)")
            psql_b.query_safe(
                f"SELECT buffer_call_start_io({buf_id}, for_input=>true, wait=>true)"
            )

            query_wait_block(
                io_method,
                node,
                psql_a,
                f"{persistency}: read buffers blocks waiting for concurrent IO",
                f"SELECT blockoff, blocknum, io_reqd, foreign_io, nblocks FROM read_buffers('{table}', 1, 5);\n",
                "BufferIo",
                True,
            )
            psql_b.query_safe(
                f"SELECT buffer_call_terminate_io({buf_id}, for_input=>true, succeed=>true, io_error=>false, release_aio=>false)"
            )
            # Because no IO wref was assigned, block 3 should not report
            # foreign IO
            expected = r"0\|1\|t\|f\|2\n2\|3\|f\|f\|1\n3\|4\|t\|f\|2"
            out = psql_a.wait_for_async_pattern(expected)
            assert re.search(
                expected, out
            ), f"{io_method}: {persistency}: IO was split due to concurrent successful IO"
    finally:
        psql_a.close()
        psql_b.close()


def sub_test_read_buffers_inject(io_method, node):
    """Tests for StartReadBuffers() that depend on injection point support."""
    psql_a = node.connect()
    psql_b = node.connect()
    psql_c = node.connect()
    try:
        # We can't easily test waiting for foreign IOs on temporary tables, as
        # the waiting in the completion hook will just stall the backend.
        table = "tbl_ok"
        persistency = "normal"

        # ---
        # Test if a read buffers encounters AIO in progress by another backend,
        # it recognizes that other IO as a foreign IO.
        # ---
        psql_a.query_safe(f"SELECT evict_rel('{table}')")

        # B: Trigger wait in the next AIO read for block 1.
        psql_b.query_safe(
            "SELECT inj_io_completion_wait(pid=>pg_backend_pid(),\n"
            f"\t\t   relfilenode=>pg_relation_filenode('{table}'),\n"
            "\t\t   blockno=>1);"
        )

        # B: Read block 1 and wait for the completion hook to be reached
        query_wait_block(
            io_method,
            node,
            psql_b,
            f"{persistency}: wait in completion of block 1",
            f"SELECT read_rel_block_ll('{table}', blockno=>1, nblocks=>1)",
            "completion_wait",
            False,
        )

        # A: Start read, wait until we're waiting for IO completion
        query_wait_block(
            io_method,
            node,
            psql_a,
            f"{persistency}: read 1-4, blocked on in-progress 1",
            f"SELECT blockoff, blocknum, io_reqd, foreign_io, nblocks FROM read_buffers('{table}', 1, 4)",
            "AioIoCompletion",
            True,
        )

        # C: Release B from completion hook
        psql_c.query_safe("SELECT inj_io_completion_continue()")

        # A: Check that we recognized the foreign IO wait, if possible
        if io_method != "sync":
            # A foreign IO covering block 1, and one IO covering blocks 2-4.
            expected = r"0\|1\|t\|t\|1\n1\|2\|t\|f\|3"
        else:
            # One IO covering everything, as that's what StartReadBuffers()
            # will return for something with misses in sync mode.
            expected = r"0\|1\|t\|f\|4"
        out = psql_a.wait_for_async_pattern(expected)
        assert re.search(
            expected, out
        ), f"{io_method}: {persistency}: read 1-3, blocked on in-progress 1, see expected result"

        # B's low-level read has completed now that C released it; drain its
        # result before B is reused below.
        psql_b.wait_for_completion()

        # ---
        # Test if a read buffers encounters AIO in progress by another backend,
        # it recognizes that other IO as a foreign IO. This time we encounter
        # the foreign IO multiple times.
        # ---
        psql_a.query_safe(f"SELECT evict_rel('{table}')")

        # B: Trigger wait in the next AIO read for block 3.
        psql_b.query_safe(
            "SELECT inj_io_completion_wait(pid=>pg_backend_pid(),\n"
            f"\t\t   relfilenode=>pg_relation_filenode('{table}'),\n"
            "\t\t   blockno=>3);"
        )

        # B: Read block 2-3 and wait for the completion hook to be reached
        query_wait_block(
            io_method,
            node,
            psql_b,
            f"{persistency}: wait in completion of block 2+3",
            f"SELECT read_rel_block_ll('{table}', blockno=>2, nblocks=>2)",
            "completion_wait",
            False,
        )

        # A: Start read, wait until we're waiting for IO completion.
        # Note that we need to defer waiting for IO until the end of
        # read_buffers(), to be able to see that the IO on 3 is still in
        # progress.
        query_wait_block(
            io_method,
            node,
            psql_a,
            f"{persistency}: read 0-3, blocked on in-progress 2+3",
            f"SELECT blockoff, blocknum, io_reqd, foreign_io, nblocks FROM\nread_buffers('{table}', 0, 4)",
            "AioIoCompletion",
            True,
        )

        # C: Release B from completion hook
        psql_c.query_safe("SELECT inj_io_completion_continue()")

        # A: Check that we recognized the foreign IO wait, if possible
        if io_method != "sync":
            # One IO covering blocks 0-1, A foreign IO covering block 2, and a
            # foreign IO covering block 3 (same wref as for block 2).
            expected = r"0\|0\|t\|f\|2\n2\|2\|t\|t\|1\n3\|3\|t\|t\|1"
        else:
            # One IO covering everything.
            expected = r"0\|0\|t\|f\|4"
        out = psql_a.wait_for_async_pattern(expected)
        assert re.search(
            expected, out
        ), f"{io_method}: {persistency}: read 0-3, blocked on in-progress 2+3, see expected result"

        # Drain B's now-completed low-level read before closing.
        psql_b.wait_for_completion()
    finally:
        psql_a.close()
        psql_b.close()
        psql_c.close()


# -- per-io_method entrypoint ------------------------------------------------


def run_io_method(io_method, node, injection_points_available):
    """Run all sub-tests for a node configured with a given io_method."""
    assert (
        node.safe_sql("SHOW io_method") == io_method
    ), f"{io_method}: io_method set correctly"

    node.safe_sql(
        "CREATE EXTENSION test_aio;\n"
        "CREATE TABLE tbl_corr(data int not null) WITH (AUTOVACUUM_ENABLED = false);\n"
        "CREATE TABLE tbl_ok(data int not null) WITH (AUTOVACUUM_ENABLED = false);\n"
        "\n"
        "INSERT INTO tbl_corr SELECT generate_series(1, 10000);\n"
        "INSERT INTO tbl_ok SELECT generate_series(1, 10000);\n"
        "SELECT grow_rel('tbl_corr', 16);\n"
        "SELECT grow_rel('tbl_ok', 16);\n"
        "\n"
        "SELECT modify_rel_block('tbl_corr', 1, corrupt_header=>true);\n"
        "CHECKPOINT;\n"
    )

    sub_test_handle(io_method, node)
    sub_test_io_error(io_method, node)
    sub_test_batchmode(io_method, node)
    sub_test_startwait_io(io_method, node)
    sub_test_complete_foreign(io_method, node)
    sub_test_close_fd(io_method, node)
    sub_test_invalidate(io_method, node)
    sub_test_zero(io_method, node)
    sub_test_checksum(io_method, node)
    sub_test_ignore_checksum(io_method, node)
    sub_test_checksum_createdb(io_method, node)
    sub_test_read_buffers(io_method, node)

    # generic injection tests
    if injection_points_available:
        sub_test_inject(io_method, node)
        sub_test_read_buffers_inject(io_method, node)

    # worker specific injection tests
    if io_method == "worker" and injection_points_available:
        sub_test_inject_worker(io_method, node)


# -- pytest test -------------------------------------------------------------


def test_001_aio(create_pg, bindir, libdir):
    methods = supported_io_methods(bindir, libdir)

    # Create and configure one instance for each io_method.  A fresh data
    # directory per method matters: the tests corrupt the shared catalog
    # pg_shseclabel, so reusing a
    # data dir across methods would carry corruption forward.
    nodes = {}
    for method in methods:
        node = create_pg(method, start=False)
        configure(node)
        node.append_conf(f"io_method={method}\n")
        nodes[method] = node

    # Just to have one test not use the default auto-tuning.
    if "sync" in nodes:
        nodes["sync"].append_conf("io_max_concurrency=4\n")

    # Determine extension/injection-point availability once, using the first
    # node (any node would do; this only inspects pg_available_extensions).
    probe = nodes[methods[0]]
    probe.start()
    extension_available = (
        probe.safe_sql(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'test_aio'"
        )
        != "0"
    )

    # The injection tests are gated on the enable_injection_points build
    # flag.  An injection-points build installs the injection_points extension;
    # treat its availability (or the env var) as the signal.
    injection_points_available = (
        probe.safe_sql(
            "SELECT count(*) > 0 FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        == "t"
    )
    if os.environ.get("enable_injection_points", "no") == "yes":
        injection_points_available = True
    probe.stop()

    # Skip faithfully if the test_aio module is not installed in this install.
    if not extension_available:
        pytest.skip("Extension test_aio not installed")

    # Execute the tests for each io_method.
    for method in methods:
        node = nodes[method]
        node.start()
        try:
            run_io_method(method, node, injection_points_available)
        finally:
            node.stop()
