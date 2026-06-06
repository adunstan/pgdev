# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Exercise the psql binary itself.

These tests cover psql's CLI flags, output formatting, error handling,
backslash commands and exit codes.  Here psql is the program under test, so it
is run as a subprocess (via the installed binary), feeding scripts in on stdin
or via -c/-f.
"""

import os
import re
import subprocess

import pytest


# ---------------------------------------------------------------------------
# psql runner helpers backing the psql_like/psql_fails_like checks.
# ---------------------------------------------------------------------------


def _run_psql(pg, sql, *, replication=None, on_error_stop=True):
    """Run psql, feeding *sql* on stdin, returning (ret, out, err).

    Uses --no-psqlrc --no-align --tuples-only --quiet, connecting via a connstr
    (with optional ``replication=`` appended), reading the script from stdin
    (-f -), with ON_ERROR_STOP on by default.  stdout and stderr have a single
    trailing newline removed.
    """
    connstr = pg.connstr("postgres")
    if replication is not None and replication != "":
        connstr += f" replication={replication}"
    argv = [
        os.path.join(pg.bindir, "psql"),
        "--no-psqlrc",
        "--no-align",
        "--tuples-only",
        "--quiet",
        "--dbname",
        connstr,
        "--file",
        "-",
    ]
    if on_error_stop:
        argv += ["--variable", "ON_ERROR_STOP=1"]
    print("# Running: " + " ".join(argv))
    proc = subprocess.run(
        argv,
        input=sql,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out = proc.stdout
    err = proc.stderr
    # Strip a single trailing newline.
    if out.endswith("\n"):
        out = out[:-1]
    if err.endswith("\n"):
        err = err[:-1]
    return proc.returncode, out, err


def _psql_like(pg, sql, expected_stdout, test_name):
    """Run *sql* and check exit 0, empty stderr, stdout matching the regex."""
    ret, stdout, stderr = _run_psql(pg, sql)
    assert ret == 0, f"{test_name}: exit code 0 (got {ret}, stderr: {stderr})"
    assert stderr == "", f"{test_name}: no stderr (got: {stderr})"
    assert re.search(expected_stdout, stdout), (
        f"{test_name}: stdout matches /{expected_stdout.pattern}/\n{stdout}"
    )


def _psql_fails_like(pg, sql, expected_stderr, test_name, replication=None):
    """Run *sql* and check nonzero exit and stderr matching the regex."""
    ret, stdout, stderr = _run_psql(pg, sql, replication=replication)
    assert ret != 0, f"{test_name}: exit code not 0\n{stdout}\n{stderr}"
    assert re.search(expected_stderr, stderr), (
        f"{test_name}: stderr matches /{expected_stderr.pattern}/\n{stderr}"
    )


def _append_to_file(path, text):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def _slurp_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Program-level checks that need no server.
# ---------------------------------------------------------------------------


def test_program_help_version_options(pg_bin):
    pg_bin.program_help_ok("psql")
    pg_bin.program_version_ok("psql")
    pg_bin.program_options_handling_ok("psql")


@pytest.mark.parametrize("arg", ["commands", "variables"])
def test_help_arg(pg_bin, arg):
    # Test --help=foo, analogous to program_help_ok().
    res = pg_bin.result(["psql", f"--help={arg}"])
    assert res.returncode == 0, f"psql --help={arg} exit code 0"
    assert res.stdout != "", f"psql --help={arg} goes to stdout"
    assert res.stderr == "", f"psql --help={arg} nothing to stderr"


# ---------------------------------------------------------------------------
# Server fixture: --locale=C --encoding=UTF8 (from init defaults) plus the
# logical replication settings.
# ---------------------------------------------------------------------------


@pytest.fixture
def node(create_pg):
    return create_pg(
        "main",
        allows_streaming="logical",
        initdb_extra=["--locale=C", "--encoding=UTF8"],
    )


# ---------------------------------------------------------------------------
# Basic backslash commands.
# ---------------------------------------------------------------------------


def test_copyright(node):
    _psql_like(node, "\\copyright", re.compile(r"Copyright"), "\\copyright")


def test_help_no_args(node):
    _psql_like(node, "\\help", re.compile(r"ALTER"), "\\help without arguments")


def test_help_with_arg(node):
    _psql_like(node, "\\help SELECT", re.compile(r"SELECT"), "\\help with argument")


def test_unsupported_replication_command(node):
    # Clean handling of unsupported replication command responses.
    _psql_fails_like(
        node,
        "START_REPLICATION 0/0",
        re.compile(r"unexpected PQresultStatus: 8$"),
        "handling of unexpected PQresultStatus",
        replication="database",
    )


def test_timing_successful_query(node):
    _psql_like(
        node,
        "\\timing on\nSELECT 1",
        re.compile(r"^1$\n^Time: \d+[.,]\d\d\d ms", re.M),
        "\\timing with successful query",
    )


def test_timing_query_error(node):
    ret, stdout, stderr = _run_psql(node, "\\timing on\nSELECT error")
    assert ret != 0, "\\timing with query error: query failed"
    assert re.search(r"^Time: \d+[.,]\d\d\d ms", stdout, re.M), (
        "\\timing with query error: timing output appears\n" + stdout
    )
    assert not re.search(r"^Time: 0[.,]000 ms", stdout, re.M), (
        "\\timing with query error: timing was updated\n" + stdout
    )


def test_encoding_variable(node):
    # ENCODING variable is set and updated when client encoding changes.
    _psql_like(
        node,
        "\\echo :ENCODING\nset client_encoding = LATIN1;\n\\echo :ENCODING",
        re.compile(r"^UTF8$\n^LATIN1$", re.M),
        "ENCODING variable is set and updated",
    )


def test_listen_notify(node):
    _psql_like(
        node,
        "LISTEN foo;\nNOTIFY foo;",
        re.compile(
            r'^Asynchronous notification "foo" received from server '
            r"process with PID \d+\.$",
            re.M,
        ),
        "notification",
    )


def test_listen_notify_payload(node):
    _psql_like(
        node,
        "LISTEN foo;\nNOTIFY foo, 'bar';",
        re.compile(
            r'^Asynchronous notification "foo" with payload "bar" received '
            r"from server process with PID \d+\.$",
            re.M,
        ),
        "notification with payload",
    )


def test_server_crash(node):
    # Behavior and output on server crash.
    ret, out, err = _run_psql(
        node,
        "SELECT 'before' AS running;\n"
        "SELECT pg_terminate_backend(pg_backend_pid());\n"
        "SELECT 'AFTER' AS not_running;\n",
    )
    assert ret == 2, f"server crash: psql exit code (got {ret})"
    assert re.search(r"before", out), "server crash: output before crash"
    assert not re.search(r"AFTER", out), "server crash: no output after crash"
    assert re.search(
        r"psql:<stdin>:2: FATAL:  terminating connection due to administrator command\n"
        r"psql:<stdin>:2: server closed the connection unexpectedly\n"
        r"\tThis probably means the server terminated abnormally\n"
        r"\tbefore or while processing the request\.\n"
        r"psql:<stdin>:2: error: connection to server was lost",
        err,
    ), "server crash: error message\n" + err


# ---------------------------------------------------------------------------
# \errverbose
#
# (Not in the regular regression tests because the output contains the source
# code location which we don't want to have to update.)
# ---------------------------------------------------------------------------


def test_errverbose_no_previous_error(node):
    _psql_like(
        node,
        "SELECT 1;\n\\errverbose",
        re.compile(r"^1\nThere is no previous error\.$"),
        "\\errverbose with no previous error",
    )


# There are three main ways to run a query that might affect \errverbose: the
# normal way, piecemeal retrieval using FETCH_COUNT, and using \gdesc.

ERRVERBOSE_CASES = [
    (
        "SELECT error;\n\\errverbose",
        r"\A^psql:<stdin>:1: ERROR:  .*$\n"
        r"^LINE 1: SELECT error;$\n"
        r"^ *\^.*$\n"
        r"^psql:<stdin>:2: error: ERROR:  [0-9A-Z]{5}: .*$\n"
        r"^LINE 1: SELECT error;$\n"
        r"^ *\^.*$\n"
        r"^LOCATION: .*$",
        "\\errverbose after normal query with error",
    ),
    (
        "\\set FETCH_COUNT 1\nSELECT error;\n\\errverbose",
        r"\A^psql:<stdin>:2: ERROR:  .*$\n"
        r"^LINE 1: SELECT error;$\n"
        r"^ *\^.*$\n"
        r"^psql:<stdin>:3: error: ERROR:  [0-9A-Z]{5}: .*$\n"
        r"^LINE 1: SELECT error;$\n"
        r"^ *\^.*$\n"
        r"^LOCATION: .*$",
        "\\errverbose after FETCH_COUNT query with error",
    ),
    (
        "SELECT error\\gdesc\n\\errverbose",
        r"\A^psql:<stdin>:1: ERROR:  .*$\n"
        r"^LINE 1: SELECT error$\n"
        r"^ *\^.*$\n"
        r"^psql:<stdin>:2: error: ERROR:  [0-9A-Z]{5}: .*$\n"
        r"^LINE 1: SELECT error$\n"
        r"^ *\^.*$\n"
        r"^LOCATION: .*$",
        "\\errverbose after \\gdesc with error",
    ),
]


@pytest.mark.parametrize(
    "sql,pattern,name", ERRVERBOSE_CASES, ids=[c[2] for c in ERRVERBOSE_CASES]
)
def test_errverbose(node, sql, pattern, name):
    ret, stdout, stderr = _run_psql(node, sql, on_error_stop=False)
    assert re.search(pattern, stderr, re.M), f"{name}\n{stderr}"


# ---------------------------------------------------------------------------
# Multiple -c and -f switches.
#
# Note that we cannot test backend-side errors as tests are unstable in this
# case.
# ---------------------------------------------------------------------------


def test_single_transaction_multiple_switches(node, tmp_path):
    tempdir = str(tmp_path)
    node.safe_sql("CREATE TABLE tab_psql_single (a int);")

    def row_count():
        return node.safe_sql("SELECT count(*) FROM tab_psql_single").strip()

    # Tests with ON_ERROR_STOP.
    node.command_ok(
        [
            "psql",
            "--no-psqlrc",
            "--single-transaction",
            "--set", "ON_ERROR_STOP=1",
            "--command", "INSERT INTO tab_psql_single VALUES (1)",
            "--command", "INSERT INTO tab_psql_single VALUES (2)",
        ],
        "ON_ERROR_STOP, --single-transaction and multiple -c switches",
    )
    assert row_count() == "2", (
        "--single-transaction commits transaction, ON_ERROR_STOP and "
        "multiple -c switches"
    )

    node.command_fails(
        [
            "psql",
            "--no-psqlrc",
            "--single-transaction",
            "--set", "ON_ERROR_STOP=1",
            "--command", "INSERT INTO tab_psql_single VALUES (3)",
            "--command", f"\\copy tab_psql_single FROM '{tempdir}/nonexistent'",
        ],
        "ON_ERROR_STOP, --single-transaction and multiple -c switches, error",
    )
    assert row_count() == "2", (
        "client-side error rolls back transaction, ON_ERROR_STOP and "
        "multiple -c switches"
    )

    # Tests mixing files and commands.
    copy_sql_file = os.path.join(tempdir, "tab_copy.sql")
    insert_sql_file = os.path.join(tempdir, "tab_insert.sql")
    _append_to_file(
        copy_sql_file, f"\\copy tab_psql_single FROM '{tempdir}/nonexistent';"
    )
    _append_to_file(insert_sql_file, "INSERT INTO tab_psql_single VALUES (4);")

    node.command_ok(
        [
            "psql",
            "--no-psqlrc",
            "--single-transaction",
            "--set", "ON_ERROR_STOP=1",
            "--file", insert_sql_file,
            "--file", insert_sql_file,
        ],
        "ON_ERROR_STOP, --single-transaction and multiple -f switches",
    )
    assert row_count() == "4", (
        "--single-transaction commits transaction, ON_ERROR_STOP and "
        "multiple -f switches"
    )

    node.command_fails(
        [
            "psql",
            "--no-psqlrc",
            "--single-transaction",
            "--set", "ON_ERROR_STOP=1",
            "--file", insert_sql_file,
            "--file", copy_sql_file,
        ],
        "ON_ERROR_STOP, --single-transaction and multiple -f switches, error",
    )
    assert row_count() == "4", (
        "client-side error rolls back transaction, ON_ERROR_STOP and "
        "multiple -f switches"
    )

    # Tests without ON_ERROR_STOP.
    # The last switch fails on \copy.  The command returns a failure and the
    # transaction commits.
    node.command_fails(
        [
            "psql",
            "--no-psqlrc",
            "--single-transaction",
            "--file", insert_sql_file,
            "--file", insert_sql_file,
            "--command", f"\\copy tab_psql_single FROM '{tempdir}/nonexistent'",
        ],
        "no ON_ERROR_STOP, --single-transaction and multiple -f/-c switches",
    )
    assert row_count() == "6", (
        "client-side error commits transaction, no ON_ERROR_STOP and "
        "multiple -f/-c switches"
    )

    # The last switch fails on \copy coming from an input file.  The command
    # returns a success and the transaction commits.
    node.command_ok(
        [
            "psql",
            "--no-psqlrc",
            "--single-transaction",
            "--file", insert_sql_file,
            "--file", insert_sql_file,
            "--file", copy_sql_file,
        ],
        "no ON_ERROR_STOP, --single-transaction and multiple -f switches",
    )
    assert row_count() == "8", (
        "client-side error commits transaction, no ON_ERROR_STOP and "
        "multiple -f switches"
    )

    # The last switch makes the command return a success, and the contents of
    # the transaction commit even if there is a failure in-between.
    node.command_ok(
        [
            "psql",
            "--no-psqlrc",
            "--single-transaction",
            "--command", "INSERT INTO tab_psql_single VALUES (5)",
            "--file", copy_sql_file,
            "--command", "INSERT INTO tab_psql_single VALUES (6)",
        ],
        "no ON_ERROR_STOP, --single-transaction and multiple -c switches",
    )
    assert row_count() == "10", (
        "client-side error commits transaction, no ON_ERROR_STOP and "
        "multiple -c switches"
    )


def test_copy_from_with_default(node, tmp_path):
    # Test \copy from with DEFAULT option.
    node.safe_sql(
        "CREATE TABLE copy_default ("
        "id integer PRIMARY KEY, "
        "text_value text NOT NULL DEFAULT 'test', "
        "ts_value timestamp without time zone NOT NULL DEFAULT '2022-07-05')"
    )

    copy_default_file = str(tmp_path / "copy_default.csv")
    _append_to_file(copy_default_file, "1,value,2022-07-04\n")
    _append_to_file(copy_default_file, "2,placeholder,2022-07-03\n")
    _append_to_file(copy_default_file, "3,placeholder,placeholder\n")

    _psql_like(
        node,
        f"\\copy copy_default from {copy_default_file} with "
        "(format 'csv', default 'placeholder');\n"
        "SELECT * FROM copy_default",
        re.compile(
            r"1\|value\|2022-07-04 00:00:00\n"
            r"2\|test\|2022-07-03 00:00:00\n"
            r"3\|test\|2022-07-05 00:00:00"
        ),
        "\\copy from with DEFAULT",
    )


# ---------------------------------------------------------------------------
# \watch
# ---------------------------------------------------------------------------


def test_watch_three_iterations(node):
    # Note: the interval value is parsed with locale-aware strtod(); in the C
    # locale the formatting is the same as Python's %g.
    _psql_like(
        node,
        "SELECT 1 \\watch c=3 i=%g" % 0.01,
        re.compile(r"1\n1\n1"),
        "\\watch with 3 iterations, interval of 0.01",
    )


def test_watch_submillisecond(node):
    # Sub-millisecond wait works, equivalent to 0.
    _psql_like(
        node,
        "SELECT 1 \\watch c=3 i=%g" % 0.0001,
        re.compile(r"1\n1\n1"),
        "\\watch with 3 iterations, interval of 0.0001",
    )


def test_watch_zero_interval(node):
    _psql_like(
        node,
        "\\set WATCH_INTERVAL 0\nSELECT 1 \\watch c=3",
        re.compile(r"1\n1\n1"),
        "\\watch with 3 iterations, interval of 0",
    )


def test_watch_invalid_minimum_row(node):
    _psql_fails_like(
        node,
        "SELECT 3 \\watch m=x",
        re.compile(r"incorrect minimum row count"),
        "\\watch, invalid minimum row setting",
    )


def test_watch_minimum_rows_twice(node):
    _psql_fails_like(
        node,
        "SELECT 3 \\watch m=1 min_rows=2",
        re.compile(r"minimum row count specified more than once"),
        "\\watch, minimum rows is specified more than once",
    )


def test_watch_two_minimum_rows(node):
    _psql_like(
        node,
        "with x as (\n"
        "\t\tselect now()-backend_start AS howlong\n"
        "\t\tfrom pg_stat_activity\n"
        "\t\twhere pid = pg_backend_pid()\n"
        "\t  ) select 123 from x where howlong < '2 seconds' \\watch i=%g m=2"
        % 0.5,
        re.compile(r"^123$", re.M),
        "\\watch, 2 minimum rows",
    )


WATCH_ERROR_CASES = [
    ("SELECT 1 \\watch -10", r'incorrect interval value "-10"',
     "\\watch, negative interval"),
    ("SELECT 1 \\watch 10ab", r'incorrect interval value "10ab"',
     "\\watch, incorrect interval"),
    ("SELECT 1 \\watch 10e400", r'incorrect interval value "10e400"',
     "\\watch, out-of-range interval"),
    ("SELECT 1 \\watch 1 1", r"interval value is specified more than once",
     "\\watch, interval value is specified more than once"),
    ("SELECT 1 \\watch c=1 c=1", r"iteration count is specified more than once",
     "\\watch, iteration count is specified more than once"),
]


@pytest.mark.parametrize(
    "sql,pattern,name", WATCH_ERROR_CASES, ids=[c[2] for c in WATCH_ERROR_CASES]
)
def test_watch_errors(node, sql, pattern, name):
    _psql_fails_like(node, sql, re.compile(pattern), name)


def test_watch_interval_variable(node):
    # Check WATCH_INTERVAL.
    _psql_like(
        node,
        "\\echo :WATCH_INTERVAL\n"
        "\\set WATCH_INTERVAL 10\n"
        "\\echo :WATCH_INTERVAL\n"
        "\\unset WATCH_INTERVAL\n"
        "\\echo :WATCH_INTERVAL",
        re.compile(r"^2$\n^10$\n^2$", re.M),
        "WATCH_INTERVAL variable is set and updated",
    )
    _psql_fails_like(
        node,
        "\\set WATCH_INTERVAL 1e500",
        re.compile(r"is out of range"),
        "WATCH_INTERVAL variable is out of range",
    )
    _psql_like(
        node,
        "\\echo :WATCH_INTERVAL",
        re.compile(r"^2$", re.M),
        "WATCH_INTERVAL variable was not altered",
    )


# ---------------------------------------------------------------------------
# \g output piped into a program.
#
# The program is "perl -pe ''" to simply copy the input to the output.
# ---------------------------------------------------------------------------


def test_g_pipe(node, tmp_path):
    import shutil

    perlbin = shutil.which("perl")
    if perlbin is None:
        pytest.skip("perl not available for \\g pipe test")

    g_file = str(tmp_path / "g_file_1.out")
    pipe_cmd = f"{perlbin} -pe '' >{g_file}"

    _psql_like(node, f"SELECT 'one' \\g | {pipe_cmd}", re.compile(r""),
               "one command \\g")
    c1 = _slurp_file(g_file)
    assert re.search(r"one", c1)

    _psql_like(node, f"SELECT 'two' \\; SELECT 'three' \\g | {pipe_cmd}",
               re.compile(r""), "two commands \\g")
    c2 = _slurp_file(g_file)
    assert re.search(r"two.*three", c2, re.S)

    _psql_like(
        node,
        f"\\set SHOW_ALL_RESULTS 0\nSELECT 'four' \\; SELECT 'five' \\g | {pipe_cmd}",
        re.compile(r""),
        "two commands \\g with only last result",
    )
    c3 = _slurp_file(g_file)
    assert re.search(r"five", c3)
    assert not re.search(r"four", c3)

    _psql_like(
        node,
        f"copy (values ('foo'),('bar')) to stdout \\g | {pipe_cmd}",
        re.compile(r""),
        "copy output passed to \\g pipe",
    )
    c4 = _slurp_file(g_file)
    assert re.search(r"foo.*bar", c4, re.S)


# ---------------------------------------------------------------------------
# COPY within pipelines.  These abort the connection from the frontend so they
# cannot be tested via SQL.
# ---------------------------------------------------------------------------


def test_copy_from_in_pipeline(node):
    node.safe_sql("CREATE TABLE psql_pipeline()")
    log_location = node.log_position()
    _psql_fails_like(
        node,
        "\\startpipeline\n"
        "COPY psql_pipeline FROM STDIN;\n"
        "SELECT 'val1';\n"
        "\\syncpipeline\n"
        "\\endpipeline",
        re.compile(r"COPY in a pipeline is not supported, aborting connection"),
        "COPY FROM in pipeline: fails",
    )
    node.wait_for_log(
        r"FATAL: .*terminating connection because protocol synchronization was lost",
        log_location,
    )


def test_copy_to_in_pipeline(node):
    node.safe_sql("CREATE TABLE psql_pipeline()")
    # Remove \syncpipeline here.
    _psql_fails_like(
        node,
        "\\startpipeline\n"
        "COPY psql_pipeline TO STDOUT;\n"
        "SELECT 'val1';\n"
        "\\endpipeline",
        re.compile(r"COPY in a pipeline is not supported, aborting connection"),
        "COPY TO in pipeline: fails",
    )


def test_copy_meta_from_in_pipeline(node):
    node.safe_sql("CREATE TABLE psql_pipeline()")
    _psql_fails_like(
        node,
        "\\startpipeline\n"
        "\\copy psql_pipeline from stdin;\n"
        "SELECT 'val1';\n"
        "\\syncpipeline\n"
        "\\endpipeline",
        re.compile(r"COPY in a pipeline is not supported, aborting connection"),
        "\\copy from in pipeline: fails",
    )


def test_copy_meta_to_in_pipeline(node):
    node.safe_sql("CREATE TABLE psql_pipeline()")
    # Sync attempt after a COPY TO/FROM.
    _psql_fails_like(
        node,
        "\\startpipeline\n"
        "\\copy psql_pipeline to stdout;\n"
        "\\syncpipeline\n"
        "\\endpipeline",
        re.compile(r"COPY in a pipeline is not supported, aborting connection"),
        "\\copy to in pipeline: fails",
    )


def test_meta_command_in_restrict_mode(node):
    _psql_fails_like(
        node,
        "\\restrict test\n\\! should_fail",
        re.compile(
            r"backslash commands are restricted; only \\unrestrict is allowed"
        ),
        "meta-command in restrict mode fails",
    )
