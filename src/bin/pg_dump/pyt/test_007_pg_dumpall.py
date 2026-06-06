# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests pg_dumpall and pg_restore of cluster-wide dumps.

Tests pg_dumpall and pg_restore of cluster-wide dumps: multiple databases,
roles/globals, the --globals-only/--no-globals options, the various dump
formats, and a number of pg_restore option-combination error cases that are
specific to pg_dumpall archives.

pg_dumpall / pg_restore are the binaries under test and run as subprocesses
through the node's pg_bin (PGHOST/PGPORT point at the server).  The seed SQL
the test itself runs is executed in-process via safe_sql.  Each test-matrix
"run" is dumped from the source node and restored into a freshly created
target node so no per-run cleanup is needed.
"""

import os
import re

from pypg.util import slurp_file

# Verbose, multiline-allowing regex flags used by the patterns below.
_XM = re.VERBOSE | re.MULTILINE


def _pgdumpall_runs(tempdir, tablespace1, tablespace2, tablespace2_orig):
    """Definition of the pg_dumpall test cases.

    Each entry has setup_sql (a list of statements run in-process, where each
    element is (sql, dbname)), a dump_cmd and restore_cmd (argv lists), and
    like and/or unlike compiled regexps matched against the restore output.
    """
    # The dumped tablespace LOCATION may come back with either path separator
    # (and a Windows string literal may double its backslashes), depending on
    # how the server canonicalizes the path.  Match the path components with a
    # separator class that accepts "/", "\" or "\\" so the expected pattern
    # agrees with the dump on every platform.
    ts2_loc = r"[\\/]+".join(
        re.escape(part) for part in re.split(r"[\\/]", tablespace2_orig)
    )

    return {
        "restore_roles": {
            "setup_sql": [
                (
                    "CREATE ROLE dumpall WITH ENCRYPTED PASSWORD 'admin' SUPERUSER;",
                    "postgres",
                ),
                (
                    "CREATE ROLE dumpall2 WITH REPLICATION CONNECTION LIMIT 10;",
                    "postgres",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/restore_roles",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/restore_roles.sql",
                f"{tempdir}/restore_roles",
            ],
            "like": re.compile(
                r"\s*CREATE\ ROLE\ dumpall2;"
                r"\s*ALTER\ ROLE\ dumpall2\ WITH\ NOSUPERUSER\ INHERIT\ "
                r"NOCREATEROLE\ NOCREATEDB\ NOLOGIN\ REPLICATION\ NOBYPASSRLS\ "
                r"CONNECTION\ LIMIT\ 10;",
                _XM,
            ),
        },
        "restore_tablespace": {
            "setup_sql": [
                ("CREATE ROLE tap;", "postgres"),
                (
                    f"CREATE TABLESPACE tbl1 OWNER tap LOCATION '{tablespace1}';",
                    "postgres",
                ),
                (
                    f"CREATE TABLESPACE tbl2 OWNER tap LOCATION '{tablespace2}' "
                    "WITH (seq_page_cost=1.0);",
                    "postgres",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/restore_tablespace",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/restore_tablespace.sql",
                f"{tempdir}/restore_tablespace",
            ],
            # Match "E" as optional since it is added on LOCATION when running
            # on Windows.
            "like": re.compile(
                r"^"
                r"\n CREATE\ TABLESPACE\ tbl2\ OWNER\ tap\ LOCATION\ (?:E)?'"
                + ts2_loc
                + r"';"
                + r"\n ALTER\ TABLESPACE\ tbl2\ SET\ \(seq_page_cost=1.0\);",
                _XM,
            ),
        },
        "restore_grants": {
            "setup_sql": [
                ("CREATE DATABASE tapgrantsdb;", "postgres"),
                ("CREATE SCHEMA private;", "postgres"),
                ("CREATE SEQUENCE serial START 101;", "postgres"),
                (
                    "CREATE FUNCTION fn() RETURNS void AS $$\n"
                    "BEGIN\nEND;\n$$ LANGUAGE plpgsql;",
                    "postgres",
                ),
                ("CREATE ROLE super;", "postgres"),
                ("CREATE ROLE grant1;", "postgres"),
                ("CREATE ROLE grant2;", "postgres"),
                ("CREATE ROLE grant3;", "postgres"),
                ("CREATE ROLE grant4;", "postgres"),
                ("CREATE ROLE grant5;", "postgres"),
                ("CREATE ROLE grant6;", "postgres"),
                ("CREATE ROLE grant7;", "postgres"),
                ("CREATE ROLE grant8;", "postgres"),
                (
                    "CREATE TABLE t (id int);\n"
                    "INSERT INTO t VALUES (1), (2), (3), (4);\n"
                    "GRANT SELECT ON TABLE t TO grant1;\n"
                    "GRANT INSERT ON TABLE t TO grant2;\n"
                    "GRANT ALL PRIVILEGES ON TABLE t to grant3;\n"
                    "GRANT CONNECT, CREATE ON DATABASE tapgrantsdb TO grant4;\n"
                    "GRANT USAGE, CREATE ON SCHEMA private TO grant5;\n"
                    "GRANT USAGE, SELECT, UPDATE ON SEQUENCE serial TO grant6;\n"
                    "GRANT super TO grant7;\n"
                    "GRANT EXECUTE ON FUNCTION fn() TO grant8;\n",
                    "postgres",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/restore_grants",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/restore_grants.sql",
                f"{tempdir}/restore_grants",
            ],
            "like": re.compile(
                r"^"
                r"\n GRANT\ ALL\ ON\ SCHEMA\ private\ TO\ grant5;"
                r"(.*\n)*"
                r"\n GRANT\ ALL\ ON\ FUNCTION\ public.fn\(\)\ TO\ grant8;"
                r"(.*\n)*"
                r"\n GRANT\ ALL\ ON\ SEQUENCE\ public.serial\ TO\ grant6;"
                r"(.*\n)*"
                r"\n GRANT\ SELECT\ ON\ TABLE\ public.t\ TO\ grant1;"
                r"\n GRANT\ INSERT\ ON\ TABLE\ public.t\ TO\ grant2;"
                r"\n GRANT\ ALL\ ON\ TABLE\ public.t\ TO\ grant3;"
                r"(.*\n)*"
                r"\n GRANT\ CREATE,CONNECT\ ON\ DATABASE\ tapgrantsdb\ "
                r"TO\ grant4;",
                _XM,
            ),
        },
        "excluding_databases": {
            "setup_sql": [
                ("CREATE DATABASE db1;", "postgres"),
                (
                    "CREATE TABLE t1 (id int);\n"
                    "INSERT INTO t1 VALUES (1), (2), (3), (4);\n"
                    "CREATE TABLE t2 (id int);\n"
                    "INSERT INTO t2 VALUES (1), (2), (3), (4);",
                    "db1",
                ),
                ("CREATE DATABASE db2;", "postgres"),
                (
                    "CREATE TABLE t3 (id int);\n"
                    "INSERT INTO t3 VALUES (1), (2), (3), (4);\n"
                    "CREATE TABLE t4 (id int);\n"
                    "INSERT INTO t4 VALUES (1), (2), (3), (4);",
                    "db2",
                ),
                ("CREATE DATABASE dbex3;", "postgres"),
                (
                    "CREATE TABLE t5 (id int);\n"
                    "INSERT INTO t5 VALUES (1), (2), (3), (4);\n"
                    "CREATE TABLE t6 (id int);\n"
                    "INSERT INTO t6 VALUES (1), (2), (3), (4);",
                    "dbex3",
                ),
                ("CREATE DATABASE dbex4;", "postgres"),
                (
                    "CREATE TABLE t7 (id int);\n"
                    "INSERT INTO t7 VALUES (1), (2), (3), (4);\n"
                    "CREATE TABLE t8 (id int);\n"
                    "INSERT INTO t8 VALUES (1), (2), (3), (4);",
                    "dbex4",
                ),
                ("CREATE DATABASE db5;", "postgres"),
                (
                    "CREATE TABLE t9 (id int);\n"
                    "INSERT INTO t9 VALUES (1), (2), (3), (4);\n"
                    "CREATE TABLE t10 (id int);\n"
                    "INSERT INTO t10 VALUES (1), (2), (3), (4);",
                    "db5",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/excluding_databases",
                "--exclude-database",
                "dbex*",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/excluding_databases.sql",
                "--exclude-database",
                "db5",
                f"{tempdir}/excluding_databases",
            ],
            "like": re.compile(
                r"^"
                r"\n CREATE\ DATABASE\ db1"
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t1\ \("
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t2\ \("
                r"(.*\n)*"
                r"\n CREATE\ DATABASE\ db2"
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t3\ \("
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t4\ \(",
                _XM,
            ),
            "unlike": re.compile(
                r"^"
                r"\n CREATE\ DATABASE\ db3"
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t5\ \("
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t6\ \("
                r"(.*\n)*"
                r"\n CREATE\ DATABASE\ db4"
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t7\ \("
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t8\ \("
                r"\n CREATE\ DATABASE\ db5"
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t9\ \("
                r"(.*\n)*"
                r"\n CREATE\ TABLE\ public.t10\ \(",
                _XM,
            ),
        },
        "format_directory": {
            "setup_sql": [
                (
                    "CREATE TABLE format_directory(a int, b boolean, c text);\n"
                    "INSERT INTO format_directory VALUES "
                    "(1, true, 'name1'), (2, false, 'name2');",
                    "postgres",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/format_directory",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/format_directory.sql",
                f"{tempdir}/format_directory",
            ],
            "like": re.compile(
                r"^\n COPY\ public.format_directory\ \(a,\ b,\ c\)\ FROM\ stdin;",
                _XM,
            ),
        },
        "format_tar": {
            "setup_sql": [
                (
                    "CREATE TABLE format_tar(a int, b boolean, c text);\n"
                    "INSERT INTO format_tar VALUES "
                    "(1, false, 'name3'), (2, true, 'name4');",
                    "postgres",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "tar",
                "--file",
                f"{tempdir}/format_tar",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--format",
                "tar",
                "--file",
                f"{tempdir}/format_tar.sql",
                f"{tempdir}/format_tar",
            ],
            "like": re.compile(
                r"^\n COPY\ public.format_tar\ \(a,\ b,\ c\)\ FROM\ stdin;",
                _XM,
            ),
        },
        "format_custom": {
            "setup_sql": [
                (
                    "CREATE TABLE format_custom(a int, b boolean, c text);\n"
                    "INSERT INTO format_custom VALUES "
                    "(1, false, 'name5'), (2, true, 'name6');",
                    "postgres",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "custom",
                "--file",
                f"{tempdir}/format_custom",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--format",
                "custom",
                "--file",
                f"{tempdir}/format_custom.sql",
                f"{tempdir}/format_custom",
            ],
            "like": re.compile(
                r"^\n COPY\ public.format_custom\ \(a,\ b,\ c\)\ FROM\ stdin;",
                _XM,
            ),
        },
        "dump_globals_only": {
            "setup_sql": [
                (
                    "CREATE TABLE format_dir(a int, b boolean, c text);\n"
                    "INSERT INTO format_dir VALUES "
                    "(1, false, 'name5'), (2, true, 'name6');",
                    "postgres",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "directory",
                "--globals-only",
                "--file",
                f"{tempdir}/dump_globals_only",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--globals-only",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/dump_globals_only.sql",
                f"{tempdir}/dump_globals_only",
            ],
            "like": re.compile(
                r"^\s* CREATE\ ROLE\ dumpall;\s*\n",
                _XM,
            ),
        },
        "restore_no_globals": {
            "setup_sql": [
                (
                    "CREATE TABLE no_globals_test(a int, b text);\n"
                    "INSERT INTO no_globals_test VALUES "
                    "(1, 'hello'), (2, 'world');",
                    "postgres",
                ),
            ],
            "dump_cmd": [
                "pg_dumpall",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/restore_no_globals",
            ],
            "restore_cmd": [
                "pg_restore",
                "-C",
                "--no-globals",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/restore_no_globals.sql",
                f"{tempdir}/restore_no_globals",
            ],
            "like": re.compile(
                r"^\n COPY\ public.no_globals_test\ \(a,\ b\)\ FROM\ stdin;",
                _XM,
            ),
            "unlike": re.compile(r"^CREATE\ ROLE\ dumpall;", _XM),
        },
    }


def test_pg_dumpall(pg, create_pg, tmp_path):
    node = pg

    tempdir = str(tmp_path)
    run_db = "postgres"

    # Tablespace locations used by the "restore_tablespace" test case.
    tablespace1 = os.path.join(tempdir, "tbl1")
    tablespace2 = os.path.join(tempdir, "tbl2")
    os.mkdir(tablespace1)
    os.mkdir(tablespace2)
    tablespace2_orig = tablespace2

    pgdumpall_runs = _pgdumpall_runs(
        tempdir, tablespace1, tablespace2, tablespace2_orig
    )

    # First execute the setup_sql for all runs.
    for run in sorted(pgdumpall_runs):
        for sql, dbname in pgdumpall_runs[run].get("setup_sql", []):
            node.safe_sql(sql, dbname=dbname)

    # Execute the tests.
    for run in sorted(pgdumpall_runs):
        spec = pgdumpall_runs[run]

        # Create a new target cluster to pg_restore each run into so that we
        # don't need to clean up the target cluster after each run.
        target_node = create_pg(f"target_{run}")

        # Dumpall from the source node cluster.
        node.command_ok(spec["dump_cmd"], f"{run}: pg_dumpall runs")

        # Restore the dump on the target_node cluster.  We deliberately
        # don't assert on the restore's exit status (it may emit warnings);
        # the assertions are on the --file output.
        restore_cmd = list(spec["restore_cmd"]) + [
            "--host",
            target_node.host,
            "--port",
            str(target_node.port),
        ]
        node.pg_bin.result(restore_cmd)

        output_file = slurp_file(os.path.join(tempdir, f"{run}.sql"))

        assert spec.get("like") or spec.get(
            "unlike"
        ), f'missing "like" or "unlike" in test "{run}"'

        if spec.get("like"):
            assert spec["like"].search(
                output_file
            ), f"should dump {run}\nReview results in {tempdir}"
        if spec.get("unlike"):
            assert not spec["unlike"].search(
                output_file
            ), f"should not dump {run}\nReview results in {tempdir}"

        target_node.stop("fast")

    # Some negative test cases for pg_restore with a dump of pg_dumpall.
    custom = f"{tempdir}/format_custom"

    # error when -C is not used in pg_restore with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "--format",
            "custom",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: option -C/--create must be specified "
            "when restoring an archive created by pg_dumpall"
        ),
        "When -C is not used in pg_restore with dump of pg_dumpall",
    )

    # error when \l/--list option is used with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--list",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: option -l/--list cannot be used when "
            "restoring an archive created by pg_dumpall"
        ),
        "When --list is used in pg_restore with dump of pg_dumpall",
    )

    # error when -L/--use-list option is used with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--use-list",
            "use",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: option -L/--use-list cannot be used "
            "when restoring an archive created by pg_dumpall"
        ),
        "When -L/--use-list is used in pg_restore with dump of pg_dumpall",
    )

    # error when --strict-names option is used with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--strict-names",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: option --strict-names cannot be used "
            "when restoring an archive created by pg_dumpall"
        ),
        "When --strict-names is used in pg_restore with dump of pg_dumpall",
    )

    # error when --clean and -g/--globals-only are used with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--clean",
            "--globals-only",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: options --clean and -g/--globals-only "
            "cannot be used together when restoring an archive created "
            "by pg_dumpall"
        ),
        "When --clean and -g/--globals-only are used in pg_restore with dump "
        "of pg_dumpall",
    )

    # error when non-existent database is given with -d option
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "-d",
            "dbpq",
        ],
        re.escape('FATAL:  database "dbpq" does not exist'),
        "When non-existent database is given with -d option in pg_restore "
        "with dump of pg_dumpall",
    )

    # error when --no-schema is used with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--no-schema",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: option --no-schema cannot be used when "
            "restoring an archive created by pg_dumpall"
        ),
        "When --no-schema is used in pg_restore with dump of pg_dumpall",
    )

    # error when --data-only is used with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--data-only",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: option -a/--data-only cannot be used "
            "when restoring an archive created by pg_dumpall"
        ),
        "When --data-only is used in pg_restore with dump of pg_dumpall",
    )

    # error when --statistics-only is used with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--statistics-only",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: option --statistics-only cannot be used "
            "when restoring an archive created by pg_dumpall"
        ),
        "When --statistics-only is used in pg_restore with dump of pg_dumpall",
    )

    # error when --section excludes pre-data with dump of pg_dumpall
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--section",
            "post-data",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: option --section cannot exclude "
            "--pre-data when restoring a pg_dumpall archive"
        ),
        "When --section=post-data is used in pg_restore with dump of pg_dumpall",
    )

    # error when --globals-only and --data-only are used together
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--globals-only",
            "--data-only",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: options -a/--data-only and "
            "-g/--globals-only cannot be used together"
        ),
        "When --globals-only and --data-only are used together",
    )

    # error when --globals-only and --schema-only are used together
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--globals-only",
            "--schema-only",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: options -g/--globals-only and "
            "-s/--schema-only cannot be used together"
        ),
        "When --globals-only and --schema-only are used together",
    )

    # error when --globals-only and --statistics-only are used together
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--globals-only",
            "--statistics-only",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: options -g/--globals-only and "
            "--statistics-only cannot be used together"
        ),
        "When --globals-only and --statistics-only are used together",
    )

    # error when --globals-only and --statistics are used together
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--globals-only",
            "--statistics",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: options --statistics and "
            "-g/--globals-only cannot be used together"
        ),
        "When --globals-only and --statistics are used together",
    )

    # error when --globals-only and --exit-on-error are used together
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--globals-only",
            "--exit-on-error",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: options --exit-on-error and "
            "-g/--globals-only cannot be used together"
        ),
        "When --globals-only and --exit-on-error are used together",
    )

    # error when --globals-only and --single-transaction are used together
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--globals-only",
            "--single-transaction",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: options -g/--globals-only and "
            "-1/--single-transaction cannot be used together"
        ),
        "When --globals-only and --single-transaction are used together",
    )

    # error when --globals-only and --transaction-size are used together
    node.command_fails_like(
        [
            "pg_restore",
            custom,
            "-C",
            "--format",
            "custom",
            "--globals-only",
            "--transaction-size",
            "100",
            "--file",
            f"{tempdir}/error_test.sql",
        ],
        re.escape(
            "pg_restore: error: options -g/--globals-only and "
            "--transaction-size cannot be used together"
        ),
        "When --globals-only and --transaction-size are used together",
    )

    # verify map.dat preamble exists
    map_dat_content = slurp_file(os.path.join(tempdir, "format_directory", "map.dat"))
    assert re.search(
        r"^# map\.dat\n.*# This file maps oids to database names",
        map_dat_content,
        re.MULTILINE | re.DOTALL,
    ), "map.dat contains expected preamble"

    # verify commenting out a line in map.dat skips that database
    node.safe_sql("CREATE DATABASE comment_test_db;", dbname=run_db)
    node.safe_sql("CREATE TABLE comment_test_table (id int);", dbname="comment_test_db")

    node.command_ok(
        [
            "pg_dumpall",
            "--format",
            "directory",
            "--file",
            f"{tempdir}/comment_test",
        ],
        "pg_dumpall for comment test",
    )

    # Modify map.dat to comment out the comment_test_db entry.
    map_path = os.path.join(tempdir, "comment_test", "map.dat")
    map_content = slurp_file(map_path)
    map_content = re.sub(
        r"^(\d+ comment_test_db)$", r"# \1", map_content, flags=re.MULTILINE
    )
    with open(map_path, "w", encoding="utf-8") as fh:
        fh.write(map_content)

    # Create a target node and restore - commented db should be skipped.
    target_comment = create_pg("target_comment")

    node.command_ok(
        [
            "pg_restore",
            "-C",
            "--format",
            "directory",
            "--file",
            f"{tempdir}/comment_test_restore.sql",
            "--host",
            target_comment.host,
            "--port",
            str(target_comment.port),
            f"{tempdir}/comment_test",
        ],
        "pg_restore with commented out database in map.dat",
    )

    restore_output = slurp_file(f"{tempdir}/comment_test_restore.sql")
    assert not re.search(
        r"CREATE DATABASE comment_test_db", restore_output
    ), "commented out database in map.dat is not restored"

    # Test that --clean implies --if-exists for pg_dumpall archives.
    node.command_ok(
        [
            "pg_restore",
            "-C",
            "--format",
            "custom",
            "--clean",
            "--file",
            f"{tempdir}/clean_test.sql",
            custom,
        ],
        "pg_restore with --clean on pg_dumpall archive",
    )

    clean_output = slurp_file(f"{tempdir}/clean_test.sql")
    assert re.search(
        r"DROP ROLE IF EXISTS", clean_output
    ), "--clean implies --if-exists: DROP ROLE IF EXISTS in output"

    node.stop("fast")
