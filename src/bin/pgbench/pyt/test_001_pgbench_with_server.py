# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""pgbench tests that need a running server.

Covers built-in scripts, custom scripts (via
``\\set``/``\\if``/``\\gset``/``\\aset``/pipelines), expression and error
handling, ``--init-steps``, tablespaces, zipfian/permute, serialization and
deadlock retries, logging/aggregation, late evaluation, and so on.

The installed ``pgbench`` binary is the program under test and is run as a
subprocess (via the ``pg_bin`` helper).  Any SQL that the test itself needs to
run (setup, verification) goes through the in-process libpq Session
(``pg.safe_sql``/``pg.sql``) rather than forking psql.

One shared server is used for the whole module.  Temporary script and log files
are written under a per-module temporary directory.
"""

import os
import random
import re
import shutil
import socket
import tempfile

import pytest

from pypg.server import PostgresServer

EMPTY = [r"^$"]


# ---------------------------------------------------------------------------
# Module-scoped server (one node for the whole file)
# ---------------------------------------------------------------------------


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def basedir():
    """A per-module scratch directory for data dir, scripts, and logs."""
    d = tempfile.mkdtemp(prefix="pgbench_001_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def pg(bindir, libdir, basedir):
    """A single started PostgresServer shared across the module.

    Initialized with ``--locale C`` so program output can be matched against
    untranslated message strings.
    """
    sockdir = tempfile.mkdtemp(prefix="pgt")
    server = PostgresServer(
        "main",
        bindir,
        libdir,
        os.path.join(basedir, "main"),
        _free_port(),
        sockdir,
    )
    server.init(extra=["--locale", "C"])
    server.start()
    yield server
    server.teardown()
    shutil.rmtree(sockdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The pgbench() helper
# ---------------------------------------------------------------------------


def _make_files(basedir, files):
    """Write *files* (dict name->contents) and return the --file option list.

    The ``--file`` argument keeps any ``@weight`` suffix, while the file written
    to disk has the suffix stripped.
    Filenames must be unique within a test; existing files are removed first.
    """
    file_opts = []
    if files:
        for fn in sorted(files):
            arg = os.path.join(basedir, fn)
            file_opts += ["--file", arg]
            filename = re.sub(r"@\d+$", "", arg)
            if os.path.exists(filename):
                os.unlink(filename)
            with open(filename, "a", encoding="utf-8") as fh:
                fh.write(files[fn])
    return file_opts


def pgbench(pg, opts, stat, out, err, name, files=None, args=None,
            extra_env=None):
    """Invoke pgbench against *pg* and check exit code / stdout / stderr.

    *opts* is a string split on whitespace; *files* is an optional dict of
    script files written under the server's basedir; *args* is an optional
    list of extra trailing arguments (e.g. ``--log-prefix=...``).
    """
    cmd = ["pgbench", *opts.split()]
    cmd += _make_files(pg.basedir, files)
    if args:
        cmd += list(args)
    pg.pg_bin.command_checks_all(cmd, stat, out, err, name, extra_env=extra_env)


def check_data_state(pg, kind):
    """Verify the pgbench tables' initial filler/history state."""
    assert pg.safe_sql(
        "SELECT count(*) AS null_count FROM pgbench_accounts "
        "WHERE filler IS NULL LIMIT 10;"
    ) == "0", f"{kind}: filler column of pgbench_accounts has no NULL data"
    assert pg.safe_sql(
        "SELECT count(*) AS null_count FROM pgbench_branches WHERE filler IS NULL;"
    ) == "1", f"{kind}: filler column of pgbench_branches has only NULL data"
    assert pg.safe_sql(
        "SELECT count(*) AS null_count FROM pgbench_tellers WHERE filler IS NULL;"
    ) == "10", f"{kind}: filler column of pgbench_tellers has only NULL data"
    assert pg.safe_sql(
        "SELECT count(*) AS data_count FROM pgbench_history;"
    ) == "0", f"{kind}: pgbench_history has no data"


# ---------------------------------------------------------------------------
# Setup: tablespace + initialization, then data-state checks.
#
# These run in declared order (the module server carries state forward), so we
# keep the ordering: tablespace, concurrency, connection errors, init.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tablespace(pg):
    """Create a tablespace for partitioned-table init testing.

    Partitioned tables cannot use pg_default explicitly; this exercises table
    creation with a tablespace for partitioned tables.
    """
    ts = os.path.join(pg.basedir, "regress_pgbench_tap_1_ts_dir")
    os.mkdir(ts)
    pg.safe_sql(f"CREATE TABLESPACE regress_pgbench_tap_1_ts LOCATION '{ts}';")
    yield "regress_pgbench_tap_1_ts"
    pg.safe_sql("DROP TABLESPACE regress_pgbench_tap_1_ts")


def test_concurrent_oid_generation(pg):
    """Test concurrent OID generation via pg_enum_oid_index.

    Indirectly exercises LWLock and spinlock concurrency.
    """
    labels = ",".join(f"'l{i}'" for i in range(1, 1001))
    pgbench(
        pg,
        "--no-vacuum --client=5 --protocol=prepared --transactions=25",
        0,
        [r"processed: 125/125"],
        EMPTY,
        "concurrent OID generation",
        {
            "001_pgbench_concurrent_insert":
                f"CREATE TYPE pg_temp.e AS ENUM ({labels}); DROP TYPE pg_temp.e;"
        },
    )


def test_concurrent_grant_vacuum(pg):
    """Inplace updates from VACUUM concurrent with heap_update from GRANT.

    This is a known-flaky case ('PROC_IN_VACUUM scan breakage') that fails
    rarely.  We mark it as an xfail so a spurious failure does not break the
    suite.
    """
    pg.safe_sql("CREATE TABLE ddl_target ()")
    try:
        pgbench(
            pg,
            "--no-vacuum --client=5 --protocol=prepared --transactions=50",
            0,
            [r"processed: 250/250"],
            EMPTY,
            "concurrent GRANT/VACUUM",
            {
                "001_pgbench_grant@9": (
                    "\n"
                    "\t\t\tDO $$\n"
                    "\t\t\tBEGIN\n"
                    "\t\t\t\tPERFORM pg_advisory_xact_lock(42);\n"
                    "\t\t\t\tFOR i IN 1 .. 10 LOOP\n"
                    "\t\t\t\t\tGRANT SELECT ON ddl_target TO PUBLIC;\n"
                    "\t\t\t\t\tREVOKE SELECT ON ddl_target FROM PUBLIC;\n"
                    "\t\t\t\tEND LOOP;\n"
                    "\t\t\tEND\n"
                    "\t\t\t$$;\n"
                ),
                "001_pgbench_vacuum_ddl_target@1": "VACUUM ddl_target;",
            },
        )
    except AssertionError:
        pytest.xfail("PROC_IN_VACUUM scan breakage (known flaky)")


def test_no_such_database(pg):
    pgbench(
        pg,
        "no-such-database",
        1,
        EMPTY,
        [
            r"connection to server .* failed",
            r'FATAL:  database "no-such-database" does not exist',
        ],
        "no such database",
    )


def test_run_without_init(pg):
    pgbench(
        pg,
        "-S -t 1",
        1,
        [],
        [r"Perhaps you need to do initialization"],
        "run without init",
    )


def test_initialization_scale_1(pg):
    """Initialize pgbench tables scale 1 (client-side data generation)."""
    pgbench(
        pg,
        "-i",
        0,
        EMPTY,
        [
            r"creating tables",
            r"vacuuming",
            r"creating primary keys",
            r"done in \d+\.\d\d s ",
        ],
        "pgbench scale 1 initialization",
    )
    check_data_state(pg, "client-side")


def test_initialization_all_options(pg, tablespace):
    """Initialize again with all possible options and a tablespace."""
    pgbench(
        pg,
        "--initialize --init-steps=dtpvg --scale=1 --unlogged-tables "
        "--fillfactor=98 --foreign-keys --quiet "
        f"--tablespace={tablespace} --index-tablespace={tablespace} "
        "--partitions=2 --partition-method=hash",
        0,
        [r"(?i)^$"],
        [
            r"dropping old tables",
            r"creating tables",
            r"creating 2 partitions",
            r"vacuuming",
            r"creating primary keys",
            r"creating foreign keys",
            r"(?!vacuuming)",  # no vacuum
            r"done in \d+\.\d\d s ",
        ],
        "pgbench scale 1 initialization",
    )


def test_init_steps(pg):
    """Interaction of --init-steps with legacy step-selection options."""
    pgbench(
        pg,
        "--initialize --init-steps=dtpvGvv --no-vacuum --foreign-keys "
        "--unlogged-tables --partitions=3",
        0,
        EMPTY,
        [
            r"dropping old tables",
            r"creating tables",
            r"creating 3 partitions",
            r"creating primary keys",
            r"generating data \(server-side\)",
            r"creating foreign keys",
            r"(?!vacuuming)",  # no vacuum
            r"done in \d+\.\d\d s ",
        ],
        "pgbench --init-steps",
    )
    check_data_state(pg, "server-side")


# ---------------------------------------------------------------------------
# Built-in scripts
# ---------------------------------------------------------------------------


def test_builtin_tpcb_like(pg):
    pgbench(
        pg,
        "--transactions=5 -Dfoo=bla --client=2 --protocol=simple --builtin=t"
        " --connect -n -v -n",
        0,
        [
            r"builtin: TPC-B",
            r"clients: 2\b",
            r"processed: 10/10",
            r"mode: simple",
            r"maximum number of tries: 1",
        ],
        EMPTY,
        "pgbench tpcb-like",
    )


def test_builtin_simple_update(pg):
    pgbench(
        pg,
        "--transactions=20 --client=5 -M extended --builtin=si -C --no-vacuum -s 1",
        0,
        [
            r"builtin: simple update",
            r"clients: 5\b",
            r"threads: 1\b",
            r"processed: 100/100",
            r"mode: extended",
        ],
        [r"scale option ignored"],
        "pgbench simple update",
    )


def test_builtin_select_only(pg):
    pgbench(
        pg,
        "-t 100 -c 7 -M prepared -b se --debug",
        0,
        [
            r"builtin: select only",
            r"clients: 7\b",
            r"threads: 1\b",
            r"processed: 700/700",
            r"mode: prepared",
        ],
        [
            r"vacuum", r"client 0", r"client 1", r"sending",
            r"receiving", r"executing",
        ],
        "pgbench select only",
    )


# ---------------------------------------------------------------------------
# Thread support detection (used by custom-script test below)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def nthreads(pg):
    """Number of jobs to use: 2 if threads are supported, else 1."""
    res = pg.pg_bin.result(["pgbench", "--jobs", "2", "--bad-option"])
    if re.search(r"threads are not supported on this platform", res.stderr):
        return 1
    return 2


def test_custom_scripts_weighted(pg, nthreads):
    pgbench(
        pg,
        f"-t 100 -c 1 -j {nthreads} -M prepared -n",
        0,
        [
            r"type: multiple scripts",
            r"mode: prepared",
            r"script 1: .*/001_pgbench_custom_script_1",
            r"weight: 2",
            r"script 2: .*/001_pgbench_custom_script_2",
            r"weight: 1",
            r"processed: 100/100",
        ],
        EMPTY,
        "pgbench custom scripts",
        {
            "001_pgbench_custom_script_1@1": (
                "-- select only\n"
                "\\set aid random(1, :scale * 100000)\n"
                "SELECT abalance::INTEGER AS balance\n"
                "  FROM pgbench_accounts\n"
                "  WHERE aid=:aid;\n"
            ),
            "001_pgbench_custom_script_2@2": (
                "-- special variables\n"
                "BEGIN;\n"
                "\\set foo 1\n"
                "-- cast are needed for typing under -M prepared\n"
                "SELECT :foo::INT + :scale::INT * :client_id::INT AS bla;\n"
                "COMMIT;\n"
            ),
        },
    )


def test_custom_script_simple(pg):
    pgbench(
        pg,
        "-n -t 10 -c 1 -M simple",
        0,
        [
            r"type: .*/001_pgbench_custom_script_3",
            r"processed: 10/10",
            r"mode: simple",
        ],
        EMPTY,
        "pgbench custom script",
        {
            "001_pgbench_custom_script_3": (
                "-- select only variant\n"
                "\\set aid random(1, :scale * 100000)\n"
                "BEGIN;\n"
                "SELECT abalance::INTEGER AS balance\n"
                "  FROM pgbench_accounts\n"
                "  WHERE aid=:aid;\n"
                "COMMIT;\n"
            ),
        },
    )


def test_custom_script_extended(pg):
    pgbench(
        pg,
        "-n -t 10 -c 2 -M extended",
        0,
        [
            r"type: .*/001_pgbench_custom_script_4",
            r"processed: 20/20",
            r"mode: extended",
        ],
        EMPTY,
        "pgbench custom script",
        {
            "001_pgbench_custom_script_4": (
                "-- select only variant\n"
                "\\set aid random(1, :scale * 100000)\n"
                "BEGIN;\n"
                "SELECT abalance::INTEGER AS balance\n"
                "  FROM pgbench_accounts\n"
                "  WHERE aid=:aid;\n"
                "COMMIT;\n"
            ),
        },
    )


# ---------------------------------------------------------------------------
# Server logging of query parameters
#
# (This doesn't really belong here, but pgbench is a convenient way to issue
# commands using extended query mode with parameters.)
# ---------------------------------------------------------------------------


def test_server_parameter_logging(pg):
    # 1. Logging neither with errors nor with statements
    pg.append_conf(
        "log_min_duration_statement = 0\n"
        "log_parameter_max_length = 0\n"
        "log_parameter_max_length_on_error = 0"
    )
    pg.reload()
    offset = pg.log_position()
    pgbench(
        pg,
        "-n -t1 -c1 -M prepared",
        2,
        [],
        [
            r"ERROR:  invalid input syntax for type json",
            r"(?!unnamed portal with parameters)",
        ],
        "server parameter logging",
        {
            "001_param_1": (
                "select '{ invalid ' as value \\gset\n"
                "select $$'Valame Dios!' dijo Sancho; 'no le dije yo a vuestra "
                "merced que mirase bien lo que hacia?'$$ as long \\gset\n"
                "select column1::jsonb from (values (:value), (:long)) as q;\n"
            )
        },
    )
    log = pg.log_content()[offset:]
    assert not re.search(r"DETAIL:  Parameters: \$1 = '\{ invalid ',", log), \
        "no parameters logged"

    # 2. Logging truncated parameters on error, full with statements
    pg.append_conf(
        "log_parameter_max_length = -1\n"
        "log_parameter_max_length_on_error = 64"
    )
    pg.reload()
    pgbench(
        pg,
        "-n -t1 -c1 -M prepared",
        2,
        [],
        [
            r"ERROR:  division by zero",
            r"CONTEXT:  unnamed portal with parameters: \$1 = '1', \$2 = NULL",
        ],
        "server parameter logging",
        {
            "001_param_2": (
                "select '1' as one \\gset\n"
                "SELECT 1 / (random() / 2)::int, :one::int, :two::int;\n"
            )
        },
    )
    offset = pg.log_position()
    pgbench(
        pg,
        "-n -t1 -c1 -M prepared",
        2,
        [],
        [
            r"ERROR:  invalid input syntax for type json",
            r"(?m)CONTEXT:  JSON data, line 1: \{ invalid\.\.\.[\r\n]+"
            r"unnamed portal with parameters: \$1 = '\{ invalid ', "
            r"\$2 = '''Valame Dios!'' dijo Sancho; ''no le dije yo a vuestra "
            r"merced que \.\.\.'",
        ],
        "server parameter logging",
        {
            "001_param_3": (
                "select '{ invalid ' as value \\gset\n"
                "select $$'Valame Dios!' dijo Sancho; 'no le dije yo a vuestra "
                "merced que mirase bien lo que hacia?'$$ as long \\gset\n"
                "select column1::jsonb from (values (:value), (:long)) as q;\n"
            )
        },
    )
    log = pg.log_content()[offset:]
    assert re.search(
        r"DETAIL:  Parameters: \$1 = '\{ invalid ', \$2 = '''Valame Dios!'' "
        r"dijo Sancho; ''no le dije yo a vuestra merced que mirase bien lo que "
        r"hacia\?'''",
        log,
    ), "parameter report does not truncate"

    # 3. Logging full parameters on error, truncated with statements
    pg.append_conf(
        "log_min_duration_statement = -1\n"
        "log_parameter_max_length = 7\n"
        "log_parameter_max_length_on_error = -1"
    )
    pg.reload()
    pgbench(
        pg,
        "-n -t1 -c1 -M prepared",
        2,
        [],
        [
            r"ERROR:  division by zero",
            r"CONTEXT:  unnamed portal with parameters: \$1 = '1', \$2 = NULL",
        ],
        "server parameter logging",
        {
            "001_param_4": (
                "select '1' as one \\gset\n"
                "SELECT 1 / (random() / 2)::int, :one::int, :two::int;\n"
            )
        },
    )

    pg.append_conf("log_min_duration_statement = 0")
    pg.reload()
    offset = pg.log_position()
    pgbench(
        pg,
        "-n -t1 -c1 -M prepared",
        2,
        [],
        [
            r"ERROR:  invalid input syntax for type json",
            r"(?m)CONTEXT:  JSON data, line 1: \{ invalid\.\.\.[\r\n]+"
            r"unnamed portal with parameters: \$1 = '\{ invalid ', "
            r"\$2 = '''Valame Dios!'' dijo Sancho; ''no le dije yo a vuestra "
            r"merced que mirase bien lo que hacia\?'",
        ],
        "server parameter logging",
        {
            "001_param_5": (
                "select '{ invalid ' as value \\gset\n"
                "select $$'Valame Dios!' dijo Sancho; 'no le dije yo a vuestra "
                "merced que mirase bien lo que hacia?'$$ as long \\gset\n"
                "select column1::jsonb from (values (:value), (:long)) as q;\n"
            )
        },
    )
    log = pg.log_content()[offset:]
    assert re.search(
        r"DETAIL:  Parameters: \$1 = '\{ inval\.\.\.', \$2 = '''Valame\.\.\.'",
        log,
    ), "parameter report truncates"

    # Check that bad parameters are reported during typinput phase of BIND
    pgbench(
        pg,
        "-n -t1 -c1 -M prepared",
        2,
        [],
        [
            r'ERROR:  invalid input syntax for type smallint: "1a"',
            r"CONTEXT:  unnamed portal parameter \$2 = '1a'",
        ],
        "server parameter logging",
        {
            "001_param_6": (
                "select 42 as value1, '1a' as value2 \\gset\n"
                "select :value1::smallint, :value2::smallint;\n"
            )
        },
    )

    # Restore default logging config
    pg.append_conf(
        "log_min_duration_statement = -1\n"
        "log_parameter_max_length_on_error = 0\n"
        "log_parameter_max_length = -1"
    )
    pg.reload()


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

_EXPRESSIONS_SCRIPT = r"""-- integer functions
\set i1 debug(random(10, 19))
\set i2 debug(random_exponential(100, 199, 10.0))
\set i3 debug(random_gaussian(1000, 1999, 10.0))
\set i4 debug(abs(-4))
\set i5 debug(greatest(5, 4, 3, 2))
\set i6 debug(11 + least(-5, -4, -3, -2))
\set i7 debug(int(7.3))
-- integer arithmetic and bit-wise operators
\set i8 debug(17 / (4|1) + ( 4 + (7 >> 2)))
\set i9 debug(- (3 * 4 - (-(~ 1) + -(~ 0))) / -1 + 3 % -1)
\set ia debug(10 + (0 + 0 * 0 - 0 / 1))
\set ib debug(:ia + :scale)
\set ic debug(64 % (((2 + 1 * 2 + (1 # 2) | 4 * (2 & 11)) - (1 << 2)) + 2))
-- double functions and operators
\set d1 debug(sqrt(+1.5 * 2.0) * abs(-0.8E1))
\set d2 debug(double(1 + 1) * (-75.0 / :foo))
\set pi debug(pi() * 4.9)
\set d4 debug(greatest(4, 2, -1.17) * 4.0 * Ln(Exp(1.0)))
\set d5 debug(least(-5.18, .0E0, 1.0/0) * -3.3)
-- reset variables
\set i1 0
\set d1 false
-- yet another integer function
\set id debug(random_zipfian(1, 9, 1.3))
--- pow and power
\set poweri debug(pow(-3,3))
\set powerd debug(pow(2.0,10))
\set poweriz debug(pow(0,0))
\set powerdz debug(pow(0.0,0.0))
\set powernegi debug(pow(-2,-3))
\set powernegd debug(pow(-2.0,-3.0))
\set powernegd2 debug(power(-5.0,-5.0))
\set powerov debug(pow(9223372036854775807, 2))
\set powerov2 debug(pow(10,30))
-- comparisons and logical operations
\set c0 debug(1.0 = 0.0 and 1.0 != 0.0)
\set c1 debug(0 = 1 Or 1.0 = 1)
\set c4 debug(case when 0 < 1 then 32 else 0 end)
\set c5 debug(case when true then 33 else 0 end)
\set c6 debug(case when false THEN -1 when 1 = 1 then 13 + 19 + 2.0 end )
\set c7 debug(case when (1 > 0) and (1 >= 0) and (0 < 1) and (0 <= 1) and (0 != 1) and (0 = 0) and (0 <> 1) then 35 else 0 end)
\set c8 debug(CASE \
                WHEN (1.0 > 0.0) AND (1.0 >= 0.0) AND (0.0 < 1.0) AND (0.0 <= 1.0) AND \
                     (0.0 != 1.0) AND (0.0 = 0.0) AND (0.0 <> 1.0) AND (0.0 = 0.0) \
                  THEN 36 \
                  ELSE 0 \
              END)
\set c9 debug(CASE WHEN NOT FALSE THEN 3 * 12.3333334 END)
\set ca debug(case when false then 0 when 1-1 <> 0 then 1 else 38 end)
\set cb debug(10 + mod(13 * 7 + 12, 13) - mod(-19 * 11 - 17, 19))
\set cc debug(NOT (0 > 1) AND (1 <= 1) AND NOT (0 >= 1) AND (0 < 1) AND \
    NOT (false and true) AND (false OR TRUE) AND (NOT :f) AND (NOT FALSE) AND \
    NOT (NOT TRUE))
-- NULL value and associated operators
\set n0 debug(NULL + NULL * exp(NULL))
\set n1 debug(:n0)
\set n2 debug(NOT (:n0 IS NOT NULL OR :d1 IS NULL))
\set n3 debug(:n0 IS NULL AND :d1 IS NOT NULL AND :d1 NOTNULL)
\set n4 debug(:n0 ISNULL AND NOT :n0 IS TRUE AND :n0 IS NOT FALSE)
\set n5 debug(CASE WHEN :n IS NULL THEN 46 ELSE NULL END)
-- use a variables of all types
\set n6 debug(:n IS NULL AND NOT :f AND :t)
-- conditional truth
\set cs debug(CASE WHEN 1 THEN TRUE END AND CASE WHEN 1.0 THEN TRUE END AND CASE WHEN :n THEN NULL ELSE TRUE END)
-- hash functions
\set h0 debug(hash(10, 5432))
\set h1 debug(:h0 = hash_murmur2(10, 5432))
\set h3 debug(hash_fnv1a(10, 5432))
\set h4 debug(hash(10))
\set h5 debug(hash(10) = hash(10, :default_seed))
-- lazy evaluation
\set zy 0
\set yz debug(case when :zy = 0 then -1 else (1 / :zy) end)
\set yz debug(case when :zy = 0 or (1 / :zy) < 0 then -1 else (1 / :zy) end)
\set yz debug(case when :zy > 0 and (1 / :zy) < 0 then (1 / :zy) else 1 end)
-- substitute variables of all possible types
\set v0 NULL
\set v1 TRUE
\set v2 5432
\set v3 -54.21E-2
SELECT :v0, :v1, :v2, :v3;
-- if tests
\set nope 0
\if 1 > 0
\set id debug(65)
\elif 0
\set nope 1
\else
\set nope 1
\endif
\if 1 < 0
\set nope 1
\elif 1 > 0
\set ie debug(74)
\else
\set nope 1
\endif
\if 1 < 0
\set nope 1
\elif 1 < 0
\set nope 1
\else
\set if debug(83)
\endif
\if 1 = 1
\set ig debug(86)
\elif 0
\set nope 1
\endif
\if 1 = 0
\set nope 1
\elif 1 <> 0
\set ih debug(93)
\endif
-- must be zero if false branches where skipped
\set nope debug(:nope)
-- check automatic variables
\set sc debug(:scale)
\set ci debug(:client_id)
\set rs debug(:random_seed)
-- minint constant parsing
\set min debug(-9223372036854775808)
\set max debug(-(:min + 1))
-- parametric pseudorandom permutation function
\set t debug(permute(0, 2) + permute(1, 2) = 1)
\set t debug(permute(0, 3) + permute(1, 3) + permute(2, 3) = 3)
\set t debug(permute(0, 4) + permute(1, 4) + permute(2, 4) + permute(3, 4) = 6)
\set t debug(permute(0, 5) + permute(1, 5) + permute(2, 5) + permute(3, 5) + permute(4, 5) = 10)
\set t debug(permute(0, 16) + permute(1, 16) + permute(2, 16) + permute(3, 16) + \
             permute(4, 16) + permute(5, 16) + permute(6, 16) + permute(7, 16) + \
             permute(8, 16) + permute(9, 16) + permute(10, 16) + permute(11, 16) + \
             permute(12, 16) + permute(13, 16) + permute(14, 16) + permute(15, 16) = 120)
-- random sanity checks
\set size random(2, 1000)
\set v random(0, :size - 1)
\set p permute(:v, :size)
\set t debug(0 <= :p and :p < :size and :p = permute(:v + :size, :size) and :p <> permute(:v + 1, :size))
-- actual values
\set t debug(permute(:v, 1) = 0)
\set t debug(permute(0, 2, 5431) = 0 and permute(1, 2, 5431) = 1 and \
             permute(0, 2, 5433) = 1 and permute(1, 2, 5433) = 0)
-- check permute's portability across architectures
\set size debug(:max - 10)
\set t debug(permute(:size-1, :size, 5432) = 520382784483822430 and \
             permute(:size-2, :size, 5432) = 1143715004660802862 and \
             permute(:size-3, :size, 5432) = 447293596416496998 and \
             permute(:size-4, :size, 5432) = 916527772266572956 and \
             permute(:size-5, :size, 5432) = 2763809008686028849 and \
             permute(:size-6, :size, 5432) = 8648551549198294572 and \
             permute(:size-7, :size, 5432) = 4542876852200565125)
"""


def test_expressions(pg):
    pgbench(
        pg,
        "--random-seed=5432 -t 1 -Dfoo=-10.1 -Dbla=false -Di=+3 -Dn=null "
        "-Dt=t -Df=of -Dd=1.0",
        0,
        [r"type: .*/001_pgbench_expressions", r"processed: 1/1"],
        [
            r"setting random seed to 5432\b",
            # After explicit seeding, the four random checks (1-3,20) are
            # deterministic; but see also magic values in checks 111,113.
            r"command=1.: int 17\b",      # uniform random
            r"command=2.: int 104\b",     # exponential random
            r"command=3.: int 1498\b",    # gaussian random
            r"command=4.: int 4\b",
            r"command=5.: int 5\b",
            r"command=6.: int 6\b",
            r"command=7.: int 7\b",
            r"command=8.: int 8\b",
            r"command=9.: int 9\b",
            r"command=10.: int 10\b",
            r"command=11.: int 11\b",
            r"command=12.: int 12\b",
            r"command=15.: double 15\b",
            r"command=16.: double 16\b",
            r"command=17.: double 17\b",
            r"command=20.: int 3\b",    # zipfian random
            r"command=21.: double -27\b",
            r"command=22.: double 1024\b",
            r"command=23.: double 1\b",
            r"command=24.: double 1\b",
            r"command=25.: double -0.125\b",
            r"command=26.: double -0.125\b",
            r"command=27.: double -0.00032\b",
            r"command=28.: double 8.50705917302346e\+0?37\b",
            r"command=29.: double 1e\+0?30\b",
            r"command=30.: boolean false\b",
            r"command=31.: boolean true\b",
            r"command=32.: int 32\b",
            r"command=33.: int 33\b",
            r"command=34.: double 34\b",
            r"command=35.: int 35\b",
            r"command=36.: int 36\b",
            r"command=37.: double 37\b",
            r"command=38.: int 38\b",
            r"command=39.: int 39\b",
            r"command=40.: boolean true\b",
            r"command=41.: null\b",
            r"command=42.: null\b",
            r"command=43.: boolean true\b",
            r"command=44.: boolean true\b",
            r"command=45.: boolean true\b",
            r"command=46.: int 46\b",
            r"command=47.: boolean true\b",
            r"command=48.: boolean true\b",
            r"command=49.: int -5817877081768721676\b",
            r"command=50.: boolean true\b",
            r"command=51.: int -7793829335365542153\b",
            r"command=52.: int -?\d+\b",
            r"command=53.: boolean true\b",
            r"command=65.: int 65\b",
            r"command=74.: int 74\b",
            r"command=83.: int 83\b",
            r"command=86.: int 86\b",
            r"command=93.: int 93\b",
            r"command=95.: int 0\b",
            r"command=96.: int 1\b",                       # :scale
            r"command=97.: int 0\b",                       # :client_id
            r"command=98.: int 5432\b",                    # :random_seed
            r"command=99.: int -9223372036854775808\b",    # min int
            r"command=100.: int 9223372036854775807\b",    # max int
            # pseudorandom permutation tests
            r"command=101.: boolean true\b",
            r"command=102.: boolean true\b",
            r"command=103.: boolean true\b",
            r"command=104.: boolean true\b",
            r"command=105.: boolean true\b",
            r"command=109.: boolean true\b",
            r"command=110.: boolean true\b",
            r"command=111.: boolean true\b",
            r"command=113.: boolean true\b",
        ],
        "pgbench expressions",
        {"001_pgbench_expressions": _EXPRESSIONS_SCRIPT},
    )


def test_nested_ifs(pg):
    pgbench(
        pg,
        "--no-vacuum --client=1 --exit-on-abort --transactions=1",
        0,
        [r"actually processed"],
        EMPTY,
        "nested ifs",
        {
            "pgbench_nested_if": (
                "\n"
                "\t\t\t\\if false\n"
                "\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\if true\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\elif true\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\else\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\endif\n"
                "\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\\elif false\n"
                "\t\t\t\t\\if true\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\elif true\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\else\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\endif\n"
                "\t\t\t\\else\n"
                "\t\t\t\t\\if false\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\elif false\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\else\n"
                "\t\t\t\t\tSELECT 'correct';\n"
                "\t\t\t\t\\endif\n"
                "\t\t\t\\endif\n"
                "\t\t\t\\if true\n"
                "\t\t\t\tSELECT 'correct';\n"
                "\t\t\t\\else\n"
                "\t\t\t\t\\if true\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\elif true\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\else\n"
                "\t\t\t\t\tSELECT 1 / 0;\n"
                "\t\t\t\t\\endif\n"
                "\t\t\t\\endif\n"
                "\t\t\t"
            )
        },
    )


def test_random_seeded_determinism(pg):
    """Random determinism when seeded: same seed gives same 4 values twice."""
    pg.safe_sql(
        "CREATE UNLOGGED TABLE seeded_random("
        "seed INT8 NOT NULL, rand TEXT NOT NULL, val INTEGER NOT NULL);"
    )

    seed = random.randint(0, 999999999)
    for i in (1, 2):
        pgbench(
            pg,
            f"--random-seed={seed} -t 1",
            0,
            [r"processed: 1/1"],
            [rf"setting random seed to {seed}\b"],
            f"random seeded with {seed}",
            {
                f"001_pgbench_random_seed_{i}": (
                    "-- test random functions\n"
                    "\\set ur random(1000, 1999)\n"
                    "\\set er random_exponential(2000, 2999, 2.0)\n"
                    "\\set gr random_gaussian(3000, 3999, 3.0)\n"
                    "\\set zr random_zipfian(4000, 4999, 1.5)\n"
                    "INSERT INTO seeded_random(seed, rand, val) VALUES\n"
                    "  (:random_seed, 'uniform', :ur),\n"
                    "  (:random_seed, 'exponential', :er),\n"
                    "  (:random_seed, 'gaussian', :gr),\n"
                    "  (:random_seed, 'zipfian', :zr);\n"
                )
            },
        )

    # check that all runs generated the same 4 values
    out = pg.safe_sql(
        "SELECT seed, rand, val, COUNT(*) FROM seeded_random "
        "GROUP BY seed, rand, val"
    )
    assert re.search(rf"\b{seed}\|uniform\|1\d\d\d\|2", out), \
        "psql seeded_random count uniform"
    assert re.search(rf"\b{seed}\|exponential\|2\d\d\d\|2", out), \
        "psql seeded_random count exponential"
    assert re.search(rf"\b{seed}\|gaussian\|3\d\d\d\|2", out), \
        "psql seeded_random count gaussian"
    assert re.search(rf"\b{seed}\|zipfian\|4\d\d\d\|2", out), \
        "psql seeded_random count zipfian"

    pg.safe_sql("DROP TABLE seeded_random;")


def test_backslash_commands(pg):
    pgbench(
        pg,
        "-t 1",
        0,
        [
            r"type: .*/001_pgbench_backslash_commands",
            r"processed: 1/1",
            r"shell-echo-output",
        ],
        [r"command=8.: int 1\b"],
        "pgbench backslash commands",
        {
            "001_pgbench_backslash_commands": (
                "-- run set\n"
                "\\set zero 0\n"
                "\\set one 1.0\n"
                "-- sleep\n"
                "\\sleep :one ms\n"
                "\\sleep 100 us\n"
                "\\sleep 0 s\n"
                "\\sleep :zero\n"
                "-- setshell and continuation\n"
                "\\setshell another_one\\\n"
                "  echo \\\n"
                "    :one\n"
                "\\set n debug(:another_one)\n"
                "-- shell\n"
                "\\shell echo shell-echo-output\n"
            )
        },
    )


def test_gset(pg):
    pgbench(
        pg,
        "-t 1",
        0,
        [r"type: .*/001_pgbench_gset", r"processed: 1/1"],
        [
            r"command=3.: int 0\b",
            r"command=5.: int 1\b",
            r"command=6.: int 2\b",
            r"command=8.: int 3\b",
            r"command=10.: int 4\b",
            r"command=12.: int 5\b",
        ],
        "pgbench gset command",
        {
            "001_pgbench_gset": (
                "-- test gset\n"
                "-- no columns\n"
                "SELECT \\gset\n"
                "-- one value\n"
                "SELECT 0 AS i0 \\gset\n"
                "\\set i debug(:i0)\n"
                "-- two values\n"
                "SELECT 1 AS i1, 2 AS i2 \\gset\n"
                "\\set i debug(:i1)\n"
                "\\set i debug(:i2)\n"
                "-- with prefix\n"
                "SELECT 3 AS i3 \\gset x_\n"
                "\\set i debug(:x_i3)\n"
                "-- overwrite existing variable\n"
                "SELECT 0 AS i4, 4 AS i4 \\gset\n"
                "\\set i debug(:i4)\n"
                "-- work on the last SQL command under \\;\n"
                "\\; \\; SELECT 0 AS i5 \\; SELECT 5 AS i5 \\; \\; \\gset\n"
                "\\set i debug(:i5)\n"
            )
        },
    )


def test_gset_two_rows(pg):
    # \gset cannot accept more than one row, causing command to fail.
    pgbench(
        pg,
        "-t 1",
        2,
        [r"type: .*/001_pgbench_gset_two_rows", r"processed: 0/1"],
        [r"expected one row, got 2\b"],
        "pgbench gset command with two rows",
        {
            "001_pgbench_gset_two_rows": (
                "\nSELECT 5432 AS fail UNION SELECT 5433 ORDER BY 1 \\gset\n"
            )
        },
    )


def test_aset(pg):
    # working \aset, valid cases.
    pgbench(
        pg,
        "-t 1",
        0,
        [r"type: .*/001_pgbench_aset", r"processed: 1/1"],
        [r"command=3.: int 8\b", r"command=4.: int 7\b"],
        "pgbench aset command",
        {
            "001_pgbench_aset": (
                "\n"
                "-- test aset, which applies to a combined query\n"
                "\\; SELECT 6 AS i6 \\; SELECT 7 AS i7 \\; \\aset\n"
                "-- unless it returns more than one row, last is kept\n"
                "SELECT 8 AS i6 UNION SELECT 9 ORDER BY 1 DESC \\aset\n"
                "\\set i debug(:i6)\n"
                "\\set i debug(:i7)\n"
            )
        },
    )


def test_aset_empty(pg):
    # Empty result set with \aset, causing command to fail.
    pgbench(
        pg,
        "-t 1",
        2,
        [r"type: .*/001_pgbench_aset_empty", r"processed: 0/1"],
        [
            r'undefined variable "i8"',
            r"evaluation of meta-command failed\b",
        ],
        "pgbench aset command with empty result",
        {
            "001_pgbench_aset_empty": (
                "\n"
                "-- empty result\n"
                "\\; SELECT 5432 AS i8 WHERE FALSE \\; \\aset\n"
                "\\set i debug(:i8)\n"
            )
        },
    )


# ---------------------------------------------------------------------------
# Pipeline tests
# ---------------------------------------------------------------------------


def test_pipeline_basic(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        0,
        [r"type: .*/001_pgbench_pipeline", r"actually processed: 1/1"],
        [],
        "working \\startpipeline",
        {
            "001_pgbench_pipeline": (
                "\n-- test startpipeline\n\\startpipeline\n"
                + "select 1;\n" * 10
                + "\n\\endpipeline\n"
            )
        },
    )


def test_pipeline_sync(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        0,
        [r"type: .*/001_pgbench_pipeline_sync", r"actually processed: 1/1"],
        [],
        "working \\startpipeline with \\syncpipeline",
        {
            "001_pgbench_pipeline_sync": (
                "\n-- test startpipeline\n\\startpipeline\nselect 1;\n"
                "\\syncpipeline\n\\syncpipeline\nselect 2;\n\\syncpipeline\n"
                "select 3;\n\\endpipeline\n"
            )
        },
    )


def test_pipeline_prepared(pg):
    pgbench(
        pg,
        "-t 1 -n -M prepared",
        0,
        [r"type: .*/001_pgbench_pipeline_prep", r"actually processed: 1/1"],
        [],
        "working \\startpipeline",
        {
            "001_pgbench_pipeline_prep": (
                "\n-- test startpipeline\n\\startpipeline\n\\endpipeline\n"
                "\\startpipeline\n"
                + "select 1;\n" * 10
                + "\n\\endpipeline\n"
            )
        },
    )


def test_pipeline_twice_error(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        2,
        [],
        [r"already in pipeline mode"],
        "error: call \\startpipeline twice",
        {
            "001_pgbench_pipeline_2":
                "\n-- startpipeline twice\n\\startpipeline\n\\startpipeline\n"
        },
    )


def test_pipeline_end_no_start_error(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        2,
        [],
        [r"not in pipeline mode"],
        "error: \\endpipeline with no start",
        {
            "001_pgbench_pipeline_3":
                "\n-- pipeline not started\n\\endpipeline\n"
        },
    )


def test_pipeline_gset_error(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        2,
        [],
        [r"gset is not allowed in pipeline mode"],
        "error: \\gset not allowed in pipeline mode",
        {
            "001_pgbench_pipeline_4":
                "\n\\startpipeline\nselect 1 \\gset f\n\\endpipeline\n"
        },
    )


def test_pipeline_no_end_single_txn_error(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        2,
        [],
        [r"end of script reached with pipeline open"],
        "error: call \\startpipeline without \\endpipeline in a single transaction",
        {
            "001_pgbench_pipeline_5":
                "\n-- startpipeline only with single transaction\n\\startpipeline\n"
        },
    )


def test_pipeline_no_end_error(pg):
    pgbench(
        pg,
        "-t 2 -n -M extended",
        2,
        [],
        [r"end of script reached with pipeline open"],
        "error: call \\startpipeline without \\endpipeline",
        {
            "001_pgbench_pipeline_6":
                "\n-- startpipeline only\n\\startpipeline\n"
        },
    )


def test_pipeline_sync_no_end_error(pg):
    pgbench(
        pg,
        "-t 2 -n -M extended",
        2,
        [],
        [r"end of script reached with pipeline open"],
        "error: call \\startpipeline and \\syncpipeline without \\endpipeline",
        {
            "001_pgbench_pipeline_7":
                "\n-- startpipeline with \\syncpipeline only\n"
                "\\startpipeline\n\\syncpipeline\n"
        },
    )


def test_pipeline_set_local_first(pg):
    # SET LOCAL as first pipeline command: succeeds with a WARNING.
    pgbench(
        pg,
        "-t 1 -n -M extended",
        0,
        [],
        [r"WARNING:  SET LOCAL can only be used in transaction blocks"],
        "SET LOCAL outside implicit transaction block of pipeline",
        {
            "001_pgbench_pipeline_set_local_1":
                "\n\\startpipeline\nSET LOCAL statement_timeout='1h';\n\\endpipeline\n"
        },
    )


def test_pipeline_set_local_second(pg):
    # SET LOCAL as second pipeline command: succeeds, no WARNING.
    pgbench(
        pg,
        "-t 1 -n -M extended",
        0,
        [],
        EMPTY,
        "SET LOCAL inside implicit transaction block of pipeline",
        {
            "001_pgbench_pipeline_set_local_2":
                "\n\\startpipeline\nSELECT 1;\n"
                "SET LOCAL statement_timeout='1h';\n\\endpipeline\n"
        },
    )


def test_pipeline_set_local_sync(pg):
    # SET LOCAL with \syncpipeline: command after sync is outside the
    # implicit transaction block, causing a WARNING.
    pgbench(
        pg,
        "-t 1 -n -M extended",
        0,
        [],
        [r"WARNING:  SET LOCAL can only be used in transaction blocks"],
        "SET LOCAL and \\syncpipeline",
        {
            "001_pgbench_pipeline_set_local_3":
                "\n\\startpipeline\nSELECT 1;\n\\syncpipeline\n"
                "SET LOCAL statement_timeout='1h';\n\\endpipeline\n"
        },
    )


def test_pipeline_reindex_first(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        0,
        [],
        [],
        "REINDEX CONCURRENTLY outside implicit transaction block of pipeline",
        {
            "001_pgbench_pipeline_reindex_1":
                "\n\\startpipeline\n"
                "REINDEX TABLE CONCURRENTLY pgbench_accounts;\n"
                "SELECT 1;\n\\endpipeline\n"
        },
    )


def test_pipeline_reindex_second_error(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        2,
        [],
        [],
        "error: REINDEX CONCURRENTLY inside implicit transaction block of pipeline",
        {
            "001_pgbench_pipeline_reindex_2":
                "\n\\startpipeline\nSELECT 1;\n"
                "REINDEX TABLE CONCURRENTLY pgbench_accounts;\n\\endpipeline\n"
        },
    )


def test_pipeline_vacuum_first(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        0,
        [],
        [],
        "VACUUM outside implicit transaction block of pipeline",
        {
            "001_pgbench_pipeline_vacuum_1":
                "\n\\startpipeline\nVACUUM pgbench_accounts;\n\\endpipeline\n"
        },
    )


def test_pipeline_vacuum_second_error(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        2,
        [],
        [],
        "error: VACUUM inside implicit transaction block of pipeline",
        {
            "001_pgbench_pipeline_vacuum_2":
                "\n\\startpipeline\nSELECT 1;\n"
                "VACUUM pgbench_accounts;\n\\endpipeline\n"
        },
    )


def test_pipeline_subtransactions_error(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        2,
        [],
        [],
        "error: subtransactions not allowed in pipeline",
        {
            "001_pgbench_pipeline_subtrans":
                "\n\\startpipeline\nSAVEPOINT a;\nSELECT 1;\n"
                "ROLLBACK TO SAVEPOINT a;\nSELECT 2;\n\\endpipeline\n"
        },
    )


def test_pipeline_lock_first_error(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        2,
        [],
        [],
        "error: LOCK TABLE outside implicit transaction block of pipeline",
        {
            "001_pgbench_pipeline_lock_1":
                "\n\\startpipeline\nLOCK pgbench_accounts;\n"
                "SELECT 1;\n\\endpipeline\n"
        },
    )


def test_pipeline_lock_second(pg):
    pgbench(
        pg,
        "-t 1 -n -M extended",
        0,
        [],
        [],
        "LOCK TABLE inside implicit transaction block of pipeline",
        {
            "001_pgbench_pipeline_lock_2":
                "\n\\startpipeline\nSELECT 1;\n"
                "LOCK pgbench_accounts;\n\\endpipeline\n"
        },
    )


def test_pipeline_serializable(pg):
    pgbench(
        pg,
        "-c4 -t 10 -n -M prepared",
        0,
        [
            r"type: .*/001_pgbench_pipeline_serializable",
            r"actually processed: (\d+)/\1",
        ],
        [],
        "working \\startpipeline with serializable",
        {
            "001_pgbench_pipeline_serializable": (
                "\n-- test startpipeline with serializable\n\\startpipeline\n"
                "BEGIN ISOLATION LEVEL SERIALIZABLE;\n"
                + "select 1;\n" * 10
                + "\nEND;\n\\endpipeline\n"
            )
        },
    )


# ---------------------------------------------------------------------------
# Many expression / meta-command errors
# ---------------------------------------------------------------------------

# [ test name, expected status, expected stderr regexes, script ]
_ERRORS = [
    # SQL
    ["sql syntax error", 2,
     [r"ERROR:  syntax error", r"prepared statement .* does not exist"],
     "-- SQL syntax error\n    SELECT 1 + ;\n"],
    ["sql too many args", 1,
     [r"statement has too many arguments.*\b255\b"],
     "-- MAX_ARGS=256 for prepared\n\\set i 0\nSELECT LEAST("
     + ", ".join([":i"] * 256) + ")"],

    # SHELL
    ["shell bad command", 2,
     [r"\(shell\) .* meta-command failed"], "\\shell no-such-command"],
    ["shell undefined variable", 2,
     [r'undefined variable ":nosuchvariable"'],
     "-- undefined variable in shell\n\\shell echo ::foo :nosuchvariable\n"],
    ["shell missing command", 1, [r"missing command "], "\\shell"],
    ["shell too many args", 1, [r'too many arguments in command "shell"'],
     "-- 256 arguments to \\shell\n\\shell echo " + " ".join(["arg"] * 255)],

    # SET
    ["set syntax error", 1, [r'syntax error in command "set"'], "\\set i 1 +"],
    ["set no such function", 1, [r"unexpected function name"],
     "\\set i noSuchFunction()"],
    ["set invalid variable name", 2, [r"invalid variable name"], "\\set . 1"],
    ["set division by zero", 2, [r"division by zero"], "\\set i 1/0"],
    ["set undefined variable", 2,
     [r'undefined variable "nosuchvariable"'], "\\set i :nosuchvariable"],
    ["set unexpected char", 1, [r"unexpected character .;."], "\\set i ;"],
    ["set too many args", 2, [r"too many function arguments"],
     "\\set i least(0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16)"],
    ["set empty random range", 2, [r"empty range given to random"],
     "\\set i random(5,3)"],
    ["set random range too large", 2, [r"random range is too large"],
     "\\set i random(:minint, :maxint)"],
    ["set gaussian param too small", 2, [r"gaussian param.* at least 2"],
     "\\set i random_gaussian(0, 10, 1.0)"],
    ["set exponential param greater 0", 2,
     [r"exponential parameter must be greater "],
     "\\set i random_exponential(0, 10, 0.0)"],
    ["set zipfian param to 1", 2,
     [r"zipfian parameter must be in range \[1\.001, 1000\]"],
     "\\set i random_zipfian(0, 10, 1)"],
    ["set zipfian param too large", 2,
     [r"zipfian parameter must be in range \[1\.001, 1000\]"],
     "\\set i random_zipfian(0, 10, 1000000)"],
    ["set non numeric value", 2, [r'malformed variable "foo" value: "bla"'],
     "\\set i :foo + 1"],
    ["set no expression", 1, [r"syntax error"], "\\set i"],
    ["set missing argument", 1, [r"(?i)missing argument"], "\\set"],
    ["set not a bool", 2, [r"cannot coerce double to boolean"],
     "\\set b NOT 0.0"],
    ["set not an int", 2, [r"cannot coerce boolean to int"],
     "\\set i TRUE + 2"],
    ["set not a double", 2, [r"cannot coerce boolean to double"],
     "\\set d ln(TRUE)"],
    ["set case error", 1, [r'syntax error in command "set"'],
     "\\set i CASE TRUE THEN 1 ELSE 0 END"],
    ["set random error", 2, [r"cannot coerce boolean to int"],
     "\\set b random(FALSE, TRUE)"],
    ["set number of args mismatch", 1, [r"unexpected number of arguments"],
     "\\set d ln(1.0, 2.0))"],
    ["set at least one arg", 1, [r"at least one argument expected"],
     "\\set i greatest())"],

    # SET: ARITHMETIC OVERFLOW DETECTION
    ["set double to int overflow", 2, [r"double to int overflow for 100"],
     "\\set i int(1E32)"],
    ["set bigint add overflow", 2, [r"int add out"],
     "\\set i (1<<62) + (1<<62)"],
    ["set bigint sub overflow", 2, [r"int sub out"],
     "\\set i 0 - (1<<62) - (1<<62) - (1<<62)"],
    ["set bigint mul overflow", 2, [r"int mul out"], "\\set i 2 * (1<<62)"],
    ["set bigint div out of range", 2, [r"bigint div out of range"],
     "\\set i :minint / -1"],

    # SETSHELL
    ["setshell not an int", 2, [r"command must return an integer"],
     "\\setshell i echo -n one"],
    ["setshell missing arg", 1, [r"missing argument "], "\\setshell var"],
    ["setshell no such command", 2, [r"could not read result "],
     "\\setshell var no-such-command"],

    # SLEEP
    ["sleep undefined variable", 2, [r"sleep: undefined variable"],
     "\\sleep :nosuchvariable"],
    ["sleep too many args", 1, [r"too many arguments"], "\\sleep too many args"],
    ["sleep missing arg", 1, [r"missing argument", r"\\sleep"], "\\sleep"],
    ["sleep unknown unit", 1, [r"unrecognized time unit"], "\\sleep 1 week"],

    # MISC
    ["misc invalid backslash command", 1,
     [r'invalid command .* "nosuchcommand"'], "\\nosuchcommand"],
    ["misc empty script", 1, [r"empty command list for script"], ""],
    ["bad boolean", 2, [r"malformed variable.*trueXXX"],
     "\\set b :badtrue or true"],
    ["invalid permute size", 2,
     [r"permute size parameter must be greater than zero"],
     "\\set i permute(0, 0)"],

    # GSET
    ["gset no row", 2, [r"expected one row, got 0\b"],
     "SELECT WHERE FALSE \\gset"],
    ["gset alone", 1, [r"gset must follow an SQL command"], "\\gset"],
    ["gset no SQL", 1, [r"gset must follow an SQL command"], "\\set i +1\n\\gset"],
    ["gset too many arguments", 1, [r"too many arguments"],
     "SELECT 1 \\gset a b"],
    ["gset after gset", 1, [r"gset must follow an SQL command"],
     "SELECT 1 AS i \\gset\n\\gset"],
    ["gset non SELECT", 2, [r"expected one row, got 0"],
     "DROP TABLE IF EXISTS no_such_table \\gset"],
    ["gset bad default name", 2, [r"error storing into variable \?column\?"],
     "SELECT 1 \\gset"],
    ["gset bad name", 2, [r"error storing into variable bad name!"],
     'SELECT 1 AS "bad name!" \\gset'],
]


@pytest.mark.parametrize(
    "name,status,err,script", _ERRORS,
    ids=[e[0] for e in _ERRORS],
)
def test_script_errors(pg, name, status, err, script):
    assert status != 0, f'invalid expected status for test "{name}"'
    n = "001_pgbench_error_" + name.replace(" ", "_")
    pgbench(
        pg,
        "-n -t 1 -Dfoo=bla -Dnull=null -Dtrue=true -Done=1 -Dzero=0.0 "
        "-Dbadtrue=trueXXX -Dmaxint=9223372036854775807 "
        "-Dminint=-9223372036854775808 -M prepared",
        status,
        [r"^$" if status == 1 else r"processed: 0/1"],
        err,
        "pgbench script error: " + name,
        {n: script},
    )


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------


def test_throttling(pg):
    pgbench(
        pg,
        "-t 100 -S --rate=100000 --latency-limit=1000000 -c 2 -n -r",
        0,
        [r"processed: 200/200", r"builtin: select only"],
        EMPTY,
        "pgbench throttling",
    )


def test_late_throttling(pg):
    pgbench(
        pg,
        # given the expected rate and the 2 ms tx duration, at most one is executed
        "-t 10 --rate=100000 --latency-limit=1 -n -r",
        0,
        [
            r"processed: [01]/10",
            r"type: .*/001_pgbench_sleep",
            r"above the 1.0 ms latency limit: [01]/",
        ],
        EMPTY,
        "pgbench late throttling",
        {"001_pgbench_sleep": "\\sleep 2ms"},
    )


# ---------------------------------------------------------------------------
# Logging contents
# ---------------------------------------------------------------------------


def _list_files(directory, regex):
    """Return files in *directory* whose names match *regex*."""
    pat = re.compile(regex)
    return [os.path.join(directory, f) for f in os.listdir(directory)
            if pat.search(f)]


def check_pgbench_logs(directory, prefix, nb, min_lines, max_lines, line_re):
    """Check per-thread log files and their contents."""
    logs = _list_files(directory, rf"^{prefix}\..*$")
    assert len(logs) == nb, "number of log files"
    name_re = re.compile(rf"/{prefix}\.\d+(\.\d+)?$")
    assert len([log for log in logs if name_re.search(log)]) == nb, \
        "file name format"

    body_re = re.compile(line_re)
    for log in sorted(logs):
        with open(log, "r", encoding="utf-8", errors="replace") as fh:
            contents = fh.read().split("\n")
        # split() on a trailing newline yields a final empty element; drop it
        # so trailing empty lines are discarded.
        while contents and contents[-1] == "":
            contents.pop()
        clen = len(contents)
        assert clen >= min_lines, \
            f"transaction count for {log} ({clen}) is above min"
        assert clen <= max_lines, \
            f"transaction count for {log} ({clen}) is below max"
        clen_match = len([c for c in contents if body_re.search(c)])
        assert clen_match == clen, f"transaction format for {prefix}"


def test_logs_sampling(pg):
    # Run with sampling rate, 2 clients with 50 transactions each.
    bdir = pg.basedir
    pgbench(
        pg,
        "-n -S -t 50 -c 2 --log --sampling-rate=0.5",
        0,
        [r"select only", r"processed: 100/100"],
        EMPTY,
        "pgbench logs",
        None,
        [f"--log-prefix={bdir}/001_pgbench_log_2"],
    )
    # The IDs of the clients (1st field) in the logs should be either 0 or 1.
    check_pgbench_logs(bdir, "001_pgbench_log_2", 1, 8, 92,
                       r"^[01] \d{1,2} \d+ \d \d+ \d+$")


def test_logs_contents(pg):
    # Run with different read-only option pattern, 1 client with 10 transactions.
    bdir = pg.basedir
    pgbench(
        pg,
        "-n -b select-only -t 10 -l",
        0,
        [r"select only", r"processed: 10/10"],
        EMPTY,
        "pgbench logs contents",
        None,
        [f"--log-prefix={bdir}/001_pgbench_log_3"],
    )
    # The ID of a single client (1st field) should match 0.
    check_pgbench_logs(bdir, "001_pgbench_log_3", 1, 10, 10,
                       r"^0 \d{1,2} \d+ \d \d+ \d+$")


def test_incomplete_transaction_block(pg):
    # abortion of the client if the script contains an incomplete transaction block
    pgbench(
        pg,
        "--no-vacuum",
        2,
        [r"processed: 1/10"],
        [
            r"client 0 aborted: end of script reached without completing the "
            r"last transaction"
        ],
        "incomplete transaction block",
        {"001_pgbench_incomplete_transaction_block": "BEGIN;SELECT 1;"},
    )


# ---------------------------------------------------------------------------
# Concurrent update / deadlock retries
# ---------------------------------------------------------------------------

_SERIALIZATION_SCRIPT = r"""
-- What's happening:
-- The first client starts the transaction with the isolation level Repeatable
-- Read:
--
-- BEGIN;
-- UPDATE xy SET y = ... WHERE x = 1;
--
-- The second client starts a similar transaction with the same isolation level:
--
-- BEGIN;
-- UPDATE xy SET y = ... WHERE x = 1;
-- <waiting for the first client>
--
-- The first client commits its transaction, and the second client gets a
-- serialization error.

\set delta random(-5000, 5000)

-- The second client will stop here
SELECT pg_advisory_lock(0);

-- Start transaction with concurrent update
BEGIN;
UPDATE xy SET y = y + :delta WHERE x = 1 AND pg_advisory_lock(1) IS NOT NULL;

-- Wait for the second client
DO $$
DECLARE
  exists boolean;
  waiters integer;
BEGIN
  -- The second client always comes in second, and the number of rows in the
  -- table first_client_table reflect this. Here the first client inserts a row,
  -- so the second client will see a non-empty table when repeating the
  -- transaction after the serialization error.
  SELECT EXISTS (SELECT * FROM first_client_table) INTO STRICT exists;
  IF NOT exists THEN
	-- Let the second client begin
	PERFORM pg_advisory_unlock(0);
	-- And wait until the second client tries to get the same lock
	LOOP
	  SELECT COUNT(*) INTO STRICT waiters FROM pg_locks WHERE
	  locktype = 'advisory' AND objsubid = 1 AND
	  ((classid::bigint << 32) | objid::bigint = 1::bigint) AND NOT granted;
	  IF waiters = 1 THEN
		INSERT INTO first_client_table VALUES (1);

		-- Exit loop
		EXIT;
	  END IF;
	END LOOP;
  END IF;
END$$;

COMMIT;
SELECT pg_advisory_unlock_all();
"""

_DEADLOCK_SCRIPT = r"""
-- What's happening:
-- The first client gets the lock 2.
-- The second client gets the lock 3 and tries to get the lock 2.
-- The first client tries to get the lock 3 and one of them gets a deadlock
-- error.
--
-- A client that does not get a deadlock error must hold a lock at the
-- transaction start. Thus in the end it releases all of its locks before the
-- client with the deadlock error starts a retry (we do not want any errors
-- again).

-- Since the client with the deadlock error has not released the blocking locks,
-- let's do this here.
SELECT pg_advisory_unlock_all();

-- The second client and the client with the deadlock error stop here
SELECT pg_advisory_lock(0);
SELECT pg_advisory_lock(1);

-- The second client and the client with the deadlock error always come after
-- the first and the number of rows in the table first_client_table reflects
-- this. Here the first client inserts a row, so in the future the table is
-- always non-empty.
DO $$
DECLARE
  exists boolean;
BEGIN
  SELECT EXISTS (SELECT * FROM first_client_table) INTO STRICT exists;
  IF exists THEN
	-- We are the second client or the client with the deadlock error

	-- The first client will take care by itself of this lock (see below)
	PERFORM pg_advisory_unlock(0);

	PERFORM pg_advisory_lock(3);

	-- The second client can get a deadlock here
	PERFORM pg_advisory_lock(2);
  ELSE
	-- We are the first client

	-- This code should not be used in a new transaction after an error
	INSERT INTO first_client_table VALUES (1);

	PERFORM pg_advisory_lock(2);
  END IF;
END$$;

DO $$
DECLARE
  num_rows integer;
  waiters integer;
BEGIN
  -- Check if we are the first client
  SELECT COUNT(*) FROM first_client_table INTO STRICT num_rows;
  IF num_rows = 1 THEN
	-- This code should not be used in a new transaction after an error
	INSERT INTO first_client_table VALUES (2);

	-- Let the second client begin
	PERFORM pg_advisory_unlock(0);
	PERFORM pg_advisory_unlock(1);

	-- Make sure the second client is ready for deadlock
	LOOP
	  SELECT COUNT(*) INTO STRICT waiters FROM pg_locks WHERE
	  locktype = 'advisory' AND
	  objsubid = 1 AND
	  ((classid::bigint << 32) | objid::bigint = 2::bigint) AND
	  NOT granted;

	  IF waiters = 1 THEN
	    -- Exit loop
		EXIT;
	  END IF;
	END LOOP;

	PERFORM pg_advisory_lock(0);
    -- And the second client took care by itself of the lock 1
  END IF;
END$$;

-- The first client can get a deadlock here
SELECT pg_advisory_lock(3);

SELECT pg_advisory_unlock_all();
"""


@pytest.fixture(scope="module")
def retry_tables(pg):
    """Create tables used by the serialization/deadlock retry tests."""
    pg.safe_sql(
        "CREATE UNLOGGED TABLE first_client_table (value integer); "
        "CREATE UNLOGGED TABLE xy (x integer, y integer); "
        "INSERT INTO xy VALUES (1, 2);"
    )
    yield
    pg.safe_sql("DROP TABLE first_client_table, xy;")


def test_serialization_retry(pg, retry_tables):
    # Serialization error and retry (repeatable read).
    err_pattern = (
        r"(?s)"
        r"(client (0|1) sending UPDATE xy SET y = y \+ -?\d+\b).*"
        r"client \2 got an error in command 3 \(SQL\) of script 0; "
        r"ERROR:  could not serialize access due to concurrent update\b.*"
        r"\1"
    )
    pgbench(
        pg,
        "-n -c 2 -t 1 --debug --verbose-errors --max-tries 2",
        0,
        [
            r"processed: 2/2\b",
            r"number of transactions retried: 1\b",
            r"total number of retries: 1\b",
        ],
        [err_pattern],
        "concurrent update with retrying",
        {"001_pgbench_serialization": _SERIALIZATION_SCRIPT},
        extra_env={
            "PGOPTIONS": "-c default_transaction_isolation=repeatable\\ read"
        },
    )
    # Clean up
    pg.safe_sql("DELETE FROM first_client_table;")


def test_deadlock_retry(pg, retry_tables):
    # Deadlock error and retry (read committed).
    err_pattern = (
        r"client (0|1) got an error in command (3|5) \(SQL\) of script 0; "
        r"ERROR:  deadlock detected\b"
    )
    pgbench(
        pg,
        "-n -c 2 -t 1 --max-tries 2 --verbose-errors",
        0,
        [
            r"processed: 2/2\b",
            r"number of transactions retried: 1\b",
            r"total number of retries: 1\b",
        ],
        [err_pattern],
        "deadlock with retrying",
        {"001_pgbench_deadlock": _DEADLOCK_SCRIPT},
        extra_env={
            "PGOPTIONS": "-c default_transaction_isolation=read\\ committed"
        },
    )


# ---------------------------------------------------------------------------
# --exit-on-abort, COPY, --continue-on-error
# ---------------------------------------------------------------------------


def test_exit_on_abort(pg):
    pg.safe_sql("CREATE TABLE counter(i int); INSERT INTO counter VALUES (0);")
    try:
        pgbench(
            pg,
            "-t 10 -c 2 -j 2 --exit-on-abort",
            2,
            [],
            [r"division by zero", r"Run was aborted due to an error in thread"],
            "test --exit-on-abort",
            {
                "001_exit_on_abort": (
                    "\nupdate counter set i = i+1 returning i \\gset\n"
                    "\\if :i = 5\n\\set y 1/0\n\\endif\n"
                )
            },
        )
    finally:
        pg.safe_sql("DROP TABLE counter;")


def test_copy_in_script(pg):
    pgbench(
        pg,
        "-t 10",
        2,
        [],
        [r"COPY is not supported in pgbench, aborting"],
        "Test copy in script",
        {"001_copy": " COPY pgbench_accounts FROM stdin "},
    )


def test_continue_on_error(pg):
    pg.safe_sql("CREATE TABLE unique_table(i int unique);")
    try:
        pgbench(
            pg,
            "-n -t 10 --continue-on-error --failures-detailed",
            0,
            [r"processed: 1/10\b", r"other failures: 9\b"],
            [],
            "test --continue-on-error",
            {
                "001_continue_on_error":
                    "\n\t\t\tINSERT INTO unique_table VALUES(0);\n\t\t\t"
            },
        )
    finally:
        pg.safe_sql("DROP TABLE unique_table;")
