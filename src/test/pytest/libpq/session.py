# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""A libpq session for tests.

A :class:`Session` owns one libpq connection and runs queries in-process, so
tests do not have to spawn psql.  Several methods return a
:class:`~libpq.result.ResultData`.

Asynchronous waits use PQsocketPoll, with one-second periodic deadline checks.
"""

import getpass
import os
import re
import sys
import time
from ctypes import c_char_p

from . import bindings
from .constants import (
    CONNECTION_BAD,
    CONNECTION_OK,
    PGRES_COMMAND_OK,
    PGRES_PIPELINE_ABORTED,
    PGRES_PIPELINE_SYNC,
    PGRES_POLLING_FAILED,
    PGRES_POLLING_OK,
    PGRES_POLLING_READING,
    PGRES_POLLING_WRITING,
    PGRES_TUPLES_OK,
    PQTRANS_INERROR,
)
from .errors import PqConnectionError
from .errors import QueryError
from .pgnotify import read_notification
from .result import extract_result_data

# Default per-operation timeout in seconds.
DEFAULT_TIMEOUT = int(os.environ.get("PG_TEST_TIMEOUT_DEFAULT") or "180")

# Cache of loaded libpq handles, keyed by resolved library path, so multiple
# clusters with different libdirs each get the right library exactly once.
_LIBS: dict = {}

# Last connection error, for callers that treat a failed connect as fatal but
# obtained None/raise without the libpq message handy.
connect_error = None


def _load_lib(libdir):
    from .findlib import find_lib_or_die

    if libdir:
        path = find_lib_or_die("pq", libpath=[libdir], systempath=False)
    else:
        path = find_lib_or_die("pq", systempath=True)
    lib = _LIBS.get(path)
    if lib is None:
        lib = bindings.load(path)
        _LIBS[path] = lib
    return lib


def _str_array(values):
    """Build a char** from a list of str/None (None -> SQL NULL), or None."""
    if not values:
        return None
    arr = (c_char_p * len(values))()
    for i, val in enumerate(values):
        arr[i] = None if val is None else val.encode("utf-8")
    return arr


def _enc(text):
    return text.encode("utf-8") if text is not None else None


def _dec(raw):
    return raw.decode("utf-8", "replace") if raw is not None else None


def conninfo_quote(value):
    """Escape *value* for use inside single quotes in a libpq conninfo string.

    libpq treats backslash as an escape inside single quotes, so a literal
    backslash or single quote in the value must be backslash-escaped.
    """
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


class Session:
    """A libpq connection with synchronous, async and pipeline helpers."""

    def __init__(
        self,
        connstr=None,
        node=None,
        dbname="postgres",
        libdir=None,
        user=None,
        wait=True,
        timeout=DEFAULT_TIMEOUT,
    ):
        global connect_error  # pylint: disable=global-statement

        if libdir is None and node is not None:
            libdir = node.libdir
        self._lib = _load_lib(libdir)

        if connstr is None:
            if node is None:
                raise ValueError("Session requires connstr or node")
            connstr = node.connstr(dbname)

        # Pin the connecting role unless the connection string names one, so a
        # stray PGUSER cannot select a role the cluster does not recognize.
        if not re.search(r"\buser\s*=", connstr):
            if user is None:
                user = (
                    os.environ.get("USERNAME")
                    if sys.platform == "win32"
                    else getpass.getuser()
                )
            if user:
                connstr += f" user='{conninfo_quote(user)}'"

        self.connstr = connstr
        self._notices = []
        self._notice_cb = None
        self._last_error = None
        self._closed = False
        self._timeout = timeout
        lib = self._lib

        if wait:
            self._conn = lib.PQconnectdb(_enc(connstr))
            if lib.PQstatus(self._conn) != CONNECTION_OK:
                connect_error = _dec(lib.PQerrorMessage(self._conn))
                msg = connect_error
                self.close()
                raise PqConnectionError(msg)
            self._setup_notice_processor()
        else:
            self._conn = lib.PQconnectStart(_enc(connstr))
            if lib.PQstatus(self._conn) == CONNECTION_BAD:
                connect_error = _dec(lib.PQerrorMessage(self._conn))
                msg = connect_error
                self.close()
                raise PqConnectionError(msg)

    # -- connection lifecycle ------------------------------------------------

    def _setup_notice_processor(self):
        notices = self._notices

        def _cb(_arg, message):
            notices.append(_dec(message) or "")

        # Keep a reference so libpq's stored function pointer stays valid.
        self._notice_cb = bindings.NOTICE_PROCESSOR(_cb)
        self._lib.PQsetNoticeProcessor(self._conn, self._notice_cb, None)

    def wait_connect(self, timeout=DEFAULT_TIMEOUT):
        """Drive an async (wait=False) connection to CONNECTION_OK."""
        lib = self._lib
        conn = self._conn
        start = time.monotonic()
        while True:
            poll_res = lib.PQconnectPoll(conn)
            status = lib.PQstatus(conn)
            if poll_res == PGRES_POLLING_OK or status == CONNECTION_OK:
                self._setup_notice_processor()
                return
            if poll_res == PGRES_POLLING_FAILED or status == CONNECTION_BAD:
                raise PqConnectionError(
                    "connection failed: " + (_dec(lib.PQerrorMessage(conn)) or "")
                )
            if time.monotonic() - start > timeout:
                raise TimeoutError("timed out waiting for connection")
            sock = lib.PQsocket(conn)
            if sock >= 0:
                for_read = 1 if poll_res == PGRES_POLLING_READING else 0
                for_write = 1 if poll_res == PGRES_POLLING_WRITING else 0
                end_time = lib.PQgetCurrentTimeUSec() + 1_000_000
                lib.PQsocketPoll(sock, for_read, for_write, end_time)

    def poll_connect(self):
        """Single non-blocking step of async connection polling."""
        return self._lib.PQconnectPoll(self._conn)

    def close(self):
        if getattr(self, "_closed", True):
            return
        conn = getattr(self, "_conn", None)
        if conn is not None:
            self._lib.PQfinish(conn)
        self._conn = None
        self._notice_cb = None
        self._closed = True

    quit = close

    def __del__(self):
        try:
            self.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def reconnect(self):
        if not self._closed:
            self.close()
        lib = self._lib
        self._conn = lib.PQconnectdb(_enc(self.connstr))
        self._closed = False
        status = lib.PQstatus(self._conn)
        if status == CONNECTION_OK:
            self._setup_notice_processor()
        else:
            # Failed reconnect: finish the dead conn rather than leaving a
            # half-open session whose later calls would deref a bad PGconn.
            self.close()
        return status

    def reconnect_and_clear(self):
        status = self.reconnect()
        self.clear_notices()
        return status

    def conn_status(self):
        return self._lib.PQstatus(self._conn) if not self._closed else None

    def connected(self):
        """True if the session has a live connection (status CONNECTION_OK)."""
        return not self._closed and self._lib.PQstatus(self._conn) == CONNECTION_OK

    def backend_pid(self):
        return self._lib.PQbackendPID(self._conn)

    # -- notice / stderr capture --------------------------------------------

    def conninfo_value(self, keyword):
        """Return libpq's resolved value for connection option *keyword*.

        For example ``conninfo_value("servicefile")`` reports the service file
        libpq actually settled on, the same value psql exposes as the
        :SERVICEFILE variable.  Returns None if the option has no value.
        """
        lib = self._lib
        opts = lib.PQconninfo(self._conn)
        if not opts:
            return None
        try:
            i = 0
            while opts[i].keyword:
                if _dec(opts[i].keyword) == keyword:
                    return _dec(opts[i].val)
                i += 1
            return None
        finally:
            lib.PQconninfoFree(opts)

    def get_notices_str(self):
        return "".join(self._notices)

    def clear_notices(self):
        # Clear in place: the notice callback holds a reference to this list.
        self._notices[:] = []

    def get_stderr(self):
        stderr = self.get_notices_str()
        if self._last_error is not None:
            stderr += self._last_error
        return stderr

    def clear_stderr(self):
        self.clear_notices()
        self._last_error = None

    # -- synchronous execution ----------------------------------------------

    def do(self, *sql_statements):
        """Run statements with PQexec; return the status of the last one."""
        lib = self._lib
        conn = self._conn
        status = None
        for sql in sql_statements:
            result = lib.PQexec(conn, _enc(sql))
            status = lib.PQresultStatus(result)
            lib.PQclear(result)
            if status != PGRES_COMMAND_OK:
                return status
        return status

    def query(self, sql):
        """Run SQL that may return tuples; return a ResultData.

        *sql* may contain several semicolon-separated statements; their output
        is collected like psql.  Note, however, that the whole string is sent as
        a single libpq command and therefore runs in ONE implicit transaction --
        unlike psql, which runs each statement in its own autocommit
        transaction.  Statements that cannot run inside a transaction block
        (CREATE/DROP DATABASE, VACUUM, CHECKPOINT, CREATE/ALTER/DROP
        SUBSCRIPTION, CREATE TABLESPACE, ...) must be issued one per call.
        """
        lib = self._lib
        conn = self._conn

        if not lib.PQsendQuery(conn, _enc(sql)):
            from .result import ResultData

            return ResultData(status=-1, error_message=_dec(lib.PQerrorMessage(conn)))

        final_res = None
        last_error = None
        error_status = None
        all_psqlout = []
        while True:
            result = self._get_result()
            if not result:
                break
            res = extract_result_data(lib, result, conn)
            lib.PQclear(result)
            if res.psqlout != "":
                all_psqlout.append(res.psqlout)
            if res.error_message is not None:
                last_error = res.error_message
                error_status = res.status
            if res.status == PGRES_TUPLES_OK or final_res is None:
                final_res = res

        if final_res is None:
            from .result import ResultData

            final_res = ResultData(status=PGRES_COMMAND_OK)

        if all_psqlout:
            final_res.psqlout = "\n".join(all_psqlout)
        if last_error is not None:
            final_res.error_message = last_error
        # Reflect a later statement's error in the status even when an earlier
        # statement returned tuples, so callers that check status (not just
        # error_message) see the failure.
        if error_status is not None:
            final_res.status = error_status
        self._last_error = last_error

        # A multi-statement query that errors can leave the session in an open,
        # aborted transaction: libpq aborts processing of the query string at
        # the error, so a trailing COMMIT (e.g. "BEGIN; <error>; COMMIT") never
        # runs.  Roll back so a later query on this reused session is not
        # rejected with "current transaction is aborted".
        if lib.PQtransactionStatus(conn) == PQTRANS_INERROR:
            rb = lib.PQexec(conn, b"ROLLBACK")
            if rb:
                lib.PQclear(rb)
        return final_res

    def query_safe(self, sql):
        """query() that raises on error; returns the psqlout string."""
        res = self.query(sql)
        if res.error_message is not None:
            short = re.sub(r"\s+", " ", sql[:100])
            raise QueryError(f"query_safe failed on [{short}...]: {res.error_message}")
        return res.psqlout

    def query_oneval(self, sql, missing_ok=False):
        """Return the single value of a one-row, one-column query."""
        lib = self._lib
        conn = self._conn
        result = lib.PQexec(conn, _enc(sql))
        status = lib.PQresultStatus(result)
        if status != PGRES_TUPLES_OK:
            if result:
                lib.PQclear(result)
            raise QueryError(_dec(lib.PQerrorMessage(conn)))
        ntuples = lib.PQntuples(result)
        if missing_ok and not ntuples:
            lib.PQclear(result)
            return None
        nfields = lib.PQnfields(result)
        if ntuples != 1 or nfields != 1:
            lib.PQclear(result)
            raise QueryError(f"{ntuples} tuples != 1 or {nfields} fields != 1")
        val = _dec(lib.PQgetvalue(result, 0, 0))
        if val == "" and lib.PQgetisnull(result, 0, 0):
            val = None
        lib.PQclear(result)
        return val

    def query_tuples(self, *sql_statements):
        """Run queries and return output like ``psql -A -t``."""
        # Use the pipelined path for 4+ queries.
        if len(sql_statements) >= 4:
            return self.query_tuples_pipelined(*sql_statements)

        results = []
        for sql in sql_statements:
            res = self.query(sql)
            if res.status != PGRES_TUPLES_OK:
                raise QueryError(res.error_message)
            # query() already built psqlout in "psql -A -t" form; skip only
            # when there are no rows.
            if res.rows:
                results.append(res.psqlout)
        return "\n".join(results)

    # -- asynchronous execution ---------------------------------------------

    def do_async(self, sql):
        """Send a single statement with PQsendQuery; return bool success."""
        return bool(self._lib.PQsendQuery(self._conn, _enc(sql)))

    def _get_result(self):
        """Fetch the next async result, waiting on the socket with a deadline.

        Waits for the socket to become readable -- and also writable while
        PQflush() reports unsent data, since on a non-blocking connection the
        request may not be fully flushed yet and waiting only for readable would
        deadlock (the server cannot reply to a request it has not received).
        Raises TimeoutError once the per-session timeout passes.
        """
        lib = self._lib
        conn = self._conn
        sock = lib.PQsocket(conn)
        deadline = lib.PQgetCurrentTimeUSec() + self._timeout * 1_000_000
        while lib.PQisBusy(conn):
            flush = lib.PQflush(conn)
            if flush < 0:
                raise QueryError(
                    "PQflush failed: " + (_dec(lib.PQerrorMessage(conn)) or "")
                )
            now = lib.PQgetCurrentTimeUSec()
            if now >= deadline:
                raise TimeoutError("timed out waiting for query result")
            # Wake at least once a second to recheck the deadline.
            end = min(now + 1_000_000, deadline)
            lib.PQsocketPoll(sock, 1, 1 if flush > 0 else 0, end)
            if lib.PQconsumeInput(conn) == 0:
                # Connection trouble (including the server closing the socket
                # right after a FATAL error, e.g. "cannot alter invalid
                # database").  Stop and return whatever PQgetResult yields: any
                # error result already received is reported, and a clean drop
                # with no result comes back as NULL, which get_async_result()
                # surfaces as None for crash-detection callers.
                break
        return lib.PQgetResult(conn)

    def wait_for_completion(self):
        """Drain and discard all outstanding async results."""
        lib = self._lib
        while True:
            res = self._get_result()
            if not res:
                break
            lib.PQclear(res)

    def get_async_result(self):
        """Wait for and return the next async result as ResultData."""
        lib = self._lib
        conn = self._conn
        result = self._get_result()
        if not result:
            return None
        res = extract_result_data(lib, result, conn)
        lib.PQclear(result)
        while True:
            extra = self._get_result()
            if not extra:
                break
            lib.PQclear(extra)
        return res

    # -- password change -----------------------------------------------------

    def set_password(self, user, password):
        lib = self._lib
        conn = self._conn
        result = lib.PQchangePassword(conn, _enc(user), _enc(password))
        ret = extract_result_data(lib, result, conn)
        lib.PQclear(result)
        return ret

    # -- pipeline mode -------------------------------------------------------

    def setnonblocking(self, val):
        if self._lib.PQsetnonblocking(self._conn, val):
            raise QueryError("problem setting non-blocking")

    # The camelCase names below mirror the libpq PQ* functions they wrap.

    def enterPipelineMode(self):  # pylint: disable=invalid-name
        return self._lib.PQenterPipelineMode(self._conn)

    def pipelineSync(self):  # pylint: disable=invalid-name
        return self._lib.PQpipelineSync(self._conn)

    def do_pipeline(self, statement, *args):
        arr = _str_array(list(args))
        return self._lib.PQsendQueryParams(
            self._conn, _enc(statement), len(args), None, arr, None, None, 0
        )

    def query_tuples_pipelined(self, *queries):
        """Run multiple queries in one pipeline round trip."""
        lib = self._lib
        conn = self._conn
        results = []

        if not lib.PQenterPipelineMode(conn):
            raise QueryError("Failed to enter pipeline mode")

        for sql in queries:
            if not lib.PQsendQueryParams(conn, _enc(sql), 0, None, None, None, None, 0):
                lib.PQexitPipelineMode(conn)
                raise QueryError(
                    "Failed to send query: " + (_dec(lib.PQerrorMessage(conn)) or "")
                )

        if not lib.PQpipelineSync(conn):
            lib.PQexitPipelineMode(conn)
            raise QueryError("Failed to sync pipeline")

        for i in range(len(queries)):
            result = self._get_result()
            if not result:
                lib.PQexitPipelineMode(conn)
                raise QueryError(f"No result for query {i}")
            status = lib.PQresultStatus(result)
            if status == PGRES_PIPELINE_ABORTED:
                lib.PQclear(result)
                lib.PQexitPipelineMode(conn)
                raise QueryError(f"Pipeline aborted at query {i}")
            if status == PGRES_TUPLES_OK:
                res = extract_result_data(lib, result, conn)
                if res.rows:
                    tuples = [
                        "|".join("" if v is None else v for v in row)
                        for row in res.rows
                    ]
                    results.append("\n".join(tuples))
            elif status != PGRES_COMMAND_OK:
                err = _dec(lib.PQerrorMessage(conn)) or ""
                lib.PQclear(result)
                lib.PQexitPipelineMode(conn)
                raise QueryError(f"Query {i} failed: {err}")
            lib.PQclear(result)

            # Consume the NULL result that ends this query's results.
            while True:
                extra = lib.PQgetResult(conn)
                if not extra:
                    break
                lib.PQclear(extra)

        sync_result = self._get_result()
        if sync_result:
            status = lib.PQresultStatus(sync_result)
            lib.PQclear(sync_result)
            if status != PGRES_PIPELINE_SYNC:
                lib.PQexitPipelineMode(conn)
                raise QueryError(f"Expected PGRES_PIPELINE_SYNC, got {status}")

        if not lib.PQexitPipelineMode(conn):
            raise QueryError("Failed to exit pipeline mode")

        return "\n".join(results)

    # -- notifications -------------------------------------------------------

    def get_notification(self):
        """Return one pending LISTEN/NOTIFY notification, or None."""
        lib = self._lib
        conn = self._conn
        lib.PQconsumeInput(conn)
        raw = lib.PQnotifies(conn)
        return read_notification(lib, raw)

    def get_all_notifications(self):
        """Return all pending notifications as a list of dicts."""
        notifications = []
        while True:
            notify = self.get_notification()
            if notify is None:
                break
            notifications.append(notify)
        return notifications


def connect(connstr=None, node=None, dbname="postgres", libdir=None, **kwargs):
    """Convenience constructor for a :class:`Session`."""
    return Session(connstr=connstr, node=node, dbname=dbname, libdir=libdir, **kwargs)
