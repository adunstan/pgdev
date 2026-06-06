# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Self-tests for the in-process libpq Session layer.

These exercise the parts of the Session API that distinguish it from a thin
synchronous wrapper: tuple results, one-value queries, LISTEN/NOTIFY, async
execution, and pipeline mode.
"""

from libpq import ExecStatusType


def test_query_oneval(conn):
    assert conn.query_oneval("SELECT 1") == "1"
    assert conn.query_oneval("SELECT 'hello'") == "hello"
    assert conn.query_oneval("SELECT NULL") is None


def test_query_tuples_and_metadata(conn):
    res = conn.query("SELECT n, s FROM (VALUES (1, 'a'), (2, 'b')) t(n, s) ORDER BY n")
    assert res.status == ExecStatusType.PGRES_TUPLES_OK
    assert res.names == ["n", "s"]
    assert res.rows == [["1", "a"], ["2", "b"]]
    assert res.psqlout == "1|a\n2|b"


def test_do_and_error_capture(conn):
    assert conn.do("CREATE TEMP TABLE t (a int)") == ExecStatusType.PGRES_COMMAND_OK
    res = conn.query("SELECT * FROM no_such_table")
    assert res.error_message is not None
    assert "no_such_table" in res.error_message


def test_listen_notify(conn):
    conn.do("LISTEN test_chan")
    conn.do("NOTIFY test_chan, 'payload-1'")
    note = conn.get_notification()
    assert note is not None
    assert note["channel"] == "test_chan"
    assert note["payload"] == "payload-1"
    assert note["pid"] == conn.backend_pid()


def test_async_query(conn):
    assert conn.do_async("SELECT 42")
    res = conn.get_async_result()
    assert res is not None
    assert res.psqlout == "42"


def test_pipeline(conn):
    out = conn.query_tuples_pipelined(
        "SELECT 1", "SELECT 2", "SELECT 3", "SELECT 4"
    )
    assert out == "1\n2\n3\n4"


def test_query_tuples_helper(conn):
    # Fewer than 4 queries: non-pipelined path.
    assert conn.query_tuples("SELECT 1", "SELECT 2") == "1\n2"
