# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Check how temporary file removals and statement queries are associated.

Verify the association in the server logs for various query sequences with the
simple and extended query protocols.

The extended query protocol message sequences are produced in-process via the
libpq C API on the Session's connection (PQsendQueryParams / PQsendPrepare /
PQsendQueryPrepared plus pipeline mode).
"""

import ctypes

from libpq.constants import (
    PGRES_FATAL_ERROR,
    PGRES_PIPELINE_SYNC,
)

# This test intentionally exercises the low-level libpq handles on the
# Session, so accessing its private attributes is the point.
# pylint: disable=protected-access


# ---------------------------------------------------------------------------
# Low-level extended-query helpers, operating directly on the Session's libpq
# connection.  These mirror what psql's \bind / \parse / \bind_named /
# pipeline backslash commands emit on the wire.
# ---------------------------------------------------------------------------


def _ensure_send_query_prepared(lib):
    """Configure the PQsendQueryPrepared prototype on *lib* if needed.

    PQsendQueryPrepared is not part of the shared bindings table, so set its
    ctypes prototype here (idempotent).  Used to reproduce psql's
    \\bind_named, which calls PQsendQueryPrepared.
    """
    fn = lib.PQsendQueryPrepared
    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.c_void_p,  # PGconn *
        ctypes.c_char_p,  # stmtName
        ctypes.c_int,  # nParams
        ctypes.POINTER(ctypes.c_char_p),  # paramValues
        ctypes.POINTER(ctypes.c_int),  # paramLengths
        ctypes.POINTER(ctypes.c_int),  # paramFormats
        ctypes.c_int,  # resultFormat
    ]
    return fn


def _param_array(values):
    arr = (ctypes.c_char_p * len(values))()
    for i, val in enumerate(values):
        arr[i] = None if val is None else str(val).encode("utf-8")
    return arr


def _drain(sess):
    """Consume and discard all outstanding results on the connection."""
    lib = sess._lib
    conn = sess._conn
    while True:
        res = sess._get_result()
        if not res:
            break
        status = lib.PQresultStatus(res)
        lib.PQclear(res)
        if status == PGRES_FATAL_ERROR:
            raise AssertionError(
                "unexpected error: "
                + (lib.PQerrorMessage(conn).decode("utf-8", "replace") or "")
            )


def _send_params(sess, sql, params):
    """Issue an extended query with parameters (cf psql \\bind ... \\g)."""
    lib = sess._lib
    arr = _param_array(params)
    ok = lib.PQsendQueryParams(
        sess._conn, sql.encode("utf-8"), len(params), None, arr, None, None, 0
    )
    assert ok, "PQsendQueryParams failed"


def _send_prepare(sess, name, sql):
    """Parse a named prepared statement (cf psql \\parse <name>)."""
    lib = sess._lib
    ok = lib.PQsendPrepare(
        sess._conn, name.encode("utf-8"), sql.encode("utf-8"), 0, None
    )
    assert ok, "PQsendPrepare failed"


def _send_prepared(sess, name, params):
    """Bind+execute a named prepared statement (cf psql \\bind_named ... \\g)."""
    fn = _ensure_send_query_prepared(sess._lib)
    arr = _param_array(params)
    ok = fn(sess._conn, name.encode("utf-8"), len(params), arr, None, None, 0)
    assert ok, "PQsendQueryPrepared failed"


def _run_pipeline(sess, sends):
    """Run *sends* (callables taking the session) inside one pipeline.

    Reproduces psql's \\startpipeline / \\sendpipeline / \\endpipeline: every
    send is queued, then a single sync flushes and the results are drained.
    """
    lib = sess._lib
    conn = sess._conn
    assert lib.PQenterPipelineMode(conn), "failed to enter pipeline mode"
    try:
        for send in sends:
            send(sess)
        assert lib.PQpipelineSync(conn), "failed to sync pipeline"

        # Each queued query yields a result followed by a NULL terminator; the
        # final PGRES_PIPELINE_SYNC result then closes the pipeline.  Keep
        # reading across the per-query NULLs until the SYNC arrives, then
        # consume its trailing NULL, so PQexitPipelineMode() succeeds.
        for _ in sends:
            res = sess._get_result()
            assert res, "missing pipeline result"
            status = lib.PQresultStatus(res)
            lib.PQclear(res)
            if status == PGRES_FATAL_ERROR:
                raise AssertionError(
                    "unexpected error: "
                    + (lib.PQerrorMessage(conn).decode("utf-8", "replace") or "")
                )
            term = sess._get_result()
            assert not term, "expected NULL terminator after pipeline result"

        sync = sess._get_result()
        assert (
            sync and lib.PQresultStatus(sync) == PGRES_PIPELINE_SYNC
        ), "expected PGRES_PIPELINE_SYNC"
        lib.PQclear(sync)
    finally:
        assert lib.PQexitPipelineMode(conn), "failed to exit pipeline mode"


def _run_statements(sess, *statements):
    """Run each statement as its own simple query (cf psql script splitting).

    psql sends every statement of a script as a separate simple Query message,
    so a temporary file dropped at the end of a statement's processing is
    associated with that individual statement.  node.safe_sql() instead sends
    a whole multi-statement string as one Query, which would misattribute the
    drop; running statements individually here reproduces psql's behavior.
    """
    for stmt in statements:
        res = sess.query(stmt)
        if res.error_message is not None:
            raise AssertionError(f"statement failed [{stmt}]: {res.error_message}")


# Regex fragments shared by the assertions.
def _tempfile_under(stmt):
    return r"LOG:\s+temporary file: path.*\n.*\ STATEMENT:\s+" + stmt


def test_009_log_temp_files(create_pg):
    node = create_pg("primary", start=False)
    node.append_conf(
        """
work_mem = 64kB
log_temp_files = 0
debug_parallel_query = off
log_error_verbosity = default
"""
    )
    node.start()

    # Setup table and populate with data.
    node.safe_sql(
        """
CREATE UNLOGGED TABLE foo(a int);
INSERT INTO foo(a) SELECT * FROM generate_series(1, 5000);
"""
    )

    # A dedicated session for the extended-protocol cases so the simple-query
    # safe_sql() helper (which uses a separate cached session) is unaffected.
    sess = node.connect()
    try:
        # unnamed portal: temporary file dropped under second SELECT query
        log_offset = node.log_position()
        sess.do("BEGIN")
        _send_params(sess, "SELECT a FROM foo ORDER BY a OFFSET $1", [4990])
        _drain(sess)
        sess.query("SELECT 'unnamed portal'")
        sess.do("END")
        node.wait_for_log(_tempfile_under(r"SELECT 'unnamed portal'"), log_offset)

        # bind and implicit transaction: temporary file dropped without query
        log_offset = node.log_position()
        _send_params(sess, "SELECT a FROM foo ORDER BY a OFFSET $1", [4991])
        _drain(sess)
        node.wait_for_log(r"LOG:\s+temporary file:", log_offset)
        assert not node.log_contains(
            r"STATEMENT:", log_offset
        ), "bind and implicit transaction, no statement logged"

        # named portal: temporary file dropped under second SELECT query
        log_offset = node.log_position()
        sess.do("BEGIN")
        _send_prepare(sess, "stmt", "SELECT a FROM foo ORDER BY a OFFSET $1")
        _drain(sess)
        _send_prepared(sess, "stmt", [4999])
        _drain(sess)
        sess.query("SELECT 'named portal'")
        sess.do("END")
        node.wait_for_log(_tempfile_under(r"SELECT 'named portal'"), log_offset)

        # pipelined query: temporary file dropped under second SELECT query
        log_offset = node.log_position()
        _run_pipeline(
            sess,
            [
                lambda s: _send_params(
                    s, "SELECT a FROM foo ORDER BY a OFFSET $1", [4992]
                ),
                lambda s: _send_params(s, "SELECT 'pipelined query'", []),
            ],
        )
        node.wait_for_log(_tempfile_under(r"SELECT 'pipelined query'"), log_offset)

        # parse and bind: temporary file dropped without query
        log_offset = node.log_position()
        # Use a name distinct from the SQL-level "p1" prepared in the
        # prepare/execute case below: all cases share this one connection,
        # whereas the original test used a fresh psql per case.
        _send_prepare(sess, "p1_ext", "SELECT a, a, a FROM foo ORDER BY a OFFSET $1")
        _drain(sess)
        _send_prepared(sess, "p1_ext", [4993])
        _drain(sess)
        node.wait_for_log(r"LOG:\s+temporary file:", log_offset)
        assert not node.log_contains(
            r"STATEMENT:", log_offset
        ), "parse and bind, no statement logged"

        # simple query: temporary file dropped under SELECT query
        log_offset = node.log_position()
        _run_statements(
            sess,
            "BEGIN;",
            "SELECT a FROM foo ORDER BY a OFFSET 4994;",
            "END;",
        )
        node.wait_for_log(
            _tempfile_under(r"SELECT a FROM foo ORDER BY a OFFSET 4994;"), log_offset
        )

        # cursor: temporary file dropped under CLOSE
        log_offset = node.log_position()
        _run_statements(
            sess,
            "BEGIN;",
            "DECLARE mycur CURSOR FOR SELECT a FROM foo ORDER BY a OFFSET 4995;",
            "FETCH 10 FROM mycur;",
            "SELECT 1;",
            "CLOSE mycur;",
            "END;",
        )
        node.wait_for_log(_tempfile_under(r"CLOSE mycur;"), log_offset)

        # cursor WITH HOLD: temporary file dropped under COMMIT
        log_offset = node.log_position()
        _run_statements(
            sess,
            "BEGIN;",
            "DECLARE holdcur CURSOR WITH HOLD FOR "
            "SELECT a FROM foo ORDER BY a OFFSET 4996;",
            "FETCH 10 FROM holdcur;",
            "COMMIT;",
            "CLOSE holdcur;",
        )
        node.wait_for_log(_tempfile_under(r"COMMIT;"), log_offset)

        # prepare/execute: temporary file dropped under EXECUTE
        log_offset = node.log_position()
        _run_statements(
            sess,
            "BEGIN;",
            "PREPARE p1 AS SELECT a FROM foo ORDER BY a OFFSET 4997;",
            "EXECUTE p1;",
            "DEALLOCATE p1;",
            "END;",
        )
        node.wait_for_log(_tempfile_under(r"EXECUTE p1;"), log_offset)
    finally:
        sess.close()

    node.stop("fast")
