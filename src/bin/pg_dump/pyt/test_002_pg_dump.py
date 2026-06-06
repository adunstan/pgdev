# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""The big shared pg_dump test matrix.

The big shared tests / pgdump_runs matrix.  Exercises pg_dump (plus
pg_dumpall / pg_restore) using a set of named "runs" (pg_dump invocations with
different options) and named "tests" (each with a regexp and like/unlike sets
of run names stating which runs the regexp is expected to match).

For each run the dump output file is slurped; for every test, a run listed in
the test's "like" (and not in its "unlike") must match the regexp, and every
other run must not match it.  A handful of standalone command_ok /
command_fails_like cases run before the matrix loop.

pg_dump / pg_restore / pg_dumpall are the binaries under test and are run as
subprocesses through the node's pg_bin (PGHOST/PGPORT point at the server).
The seed SQL the test itself runs is executed in-process via safe_sql.

The regexp bodies are compiled by _qr(), which supports \\Q..\\E literal spans
(escaped via re.escape) and verbose/multiline/dotall flags.
"""

import os
import re
import subprocess

import pytest

from pypg.util import TIMEOUT_DEFAULT, slurp_file


# --------------------------------------------------------------------------
# Regex pattern compilation helpers.
# --------------------------------------------------------------------------

# Verbose, multiline and dotall flag combinations used by the patterns below.
# Most patterns are verbose+multiline; some add dotall; a few are multiline
# only.
_XM = re.VERBOSE | re.MULTILINE
_XMS = re.VERBOSE | re.MULTILINE | re.DOTALL
_M = re.MULTILINE
_S = re.DOTALL


def _qr(pattern, flags=0):
    r"""Compile a regex body (which may contain \Q..\E) into a Python regex.

    Text inside \Q..\E spans is taken literally (re.escape); text outside is
    passed through unchanged (already valid Python regex syntax under the same
    flags).  An unterminated \Q runs to the end of the pattern.
    """
    # The pattern bodies treat "\/" as an escaped "/" delimiter standing for a
    # literal "/".  Normalize it away (both inside and outside \Q..\E spans,
    # where it would otherwise survive re.escape as a literal backslash-slash).
    pattern = pattern.replace(r"\/", "/")

    out = []
    i = 0
    n = len(pattern)
    while i < n:
        q = pattern.find(r"\Q", i)
        if q < 0:
            out.append(pattern[i:])
            break
        out.append(pattern[i:q])
        e = pattern.find(r"\E", q + 2)
        if e < 0:
            out.append(re.escape(pattern[q + 2:]))
            i = n
        else:
            out.append(re.escape(pattern[q + 2:e]))
            i = e + 2
    return re.compile("".join(out), flags)


def _have_pg_config_define(define):
    """Return True if the installed pg_config.h contains the given #define."""
    try:
        out = subprocess.run(
            ["pg_config", "--includedir"],
            stdout=subprocess.PIPE, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return False
    header = os.path.join(out, "pg_config.h")
    try:
        with open(header, encoding="utf-8", errors="replace") as fh:
            return define in fh.read()
    except OSError:
        return False


def _have_icu_configured():
    """Return True if the build was configured --with-icu.

    Determined by probing pg_config --configure for the --with-icu flag.
    """
    try:
        out = subprocess.run(
            ["pg_config", "--configure"],
            stdout=subprocess.PIPE, text=True, check=True,
        ).stdout
    except Exception:
        return False
    return "--with-icu" in out


# Lock-wait timeout used by a couple of runs (ms): 1000 * the default
# test timeout.
_LOCK_WAIT_TIMEOUT = str(1000 * TIMEOUT_DEFAULT)


# --------------------------------------------------------------------------
# Convenience run-name sets (dump_test_schema_runs and full_runs).
# --------------------------------------------------------------------------

# Tests which target the 'dump_test' schema, specifically.
DUMP_TEST_SCHEMA_RUNS = {
    "only_dump_test_schema",
    "only_dump_measurement",
    "test_schema_plus_large_objects",
}

# Runs which are considered 'full' dumps by pg_dump, but with flags used to
# exclude specific items (ACLs, LOs, etc).  Note schema_only_with_statistics
# is referenced here and in many like/unlike sets but is not a defined run, so
# it is a harmless no-op (it never appears as an actual run).
FULL_RUNS = {
    "binary_upgrade",
    "clean",
    "clean_if_exists",
    "createdb",
    "defaults",
    "exclude_dump_test_schema",
    "exclude_test_table",
    "exclude_test_table_data",
    "exclude_measurement",
    "exclude_measurement_data",
    "no_toast_compression",
    "no_large_objects",
    "no_owner",
    "no_policies",
    "no_policies_restore",
    "no_privs",
    "no_statistics",
    "no_subscriptions",
    "no_subscriptions_restore",
    "no_table_access_method",
    "pg_dumpall_dbprivs",
    "pg_dumpall_exclude",
    "schema_only",
    "schema_only_with_statistics",
}


def _pgdump_runs(tempdir, supports_gzip):
    """Definition of the pg_dump runs to make.

    Each run has a dump_cmd (argv list; cmd[0] resolved in the node's bindir).
    Optional keys: restore_cmd, test_key (reuse another run's like/unlike
    sets), database (run against a non-default database), command_like (run a
    command and check its stdout), glob_patterns (files that must exist after
    the dump).
    """
    return {
        "binary_upgrade": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--format", "custom",
                "--file", f"{tempdir}/binary_upgrade.dump",
                "--no-password",
                "--no-data",
                "--sequence-data",
                "--binary-upgrade",
                "--statistics",
                "--dbname", "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--format", "custom",
                "--verbose",
                "--file", f"{tempdir}/binary_upgrade.sql",
                "--statistics",
                f"{tempdir}/binary_upgrade.dump",
            ],
        },
        "clean": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/clean.sql",
                "--clean",
                "--statistics",
                "--dbname", "postgres",
            ],
        },
        "clean_if_exists": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/clean_if_exists.sql",
                "--clean",
                "--if-exists",
                "--encoding", "UTF8",
                "--statistics",
                "postgres",
            ],
        },
        "column_inserts": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/column_inserts.sql",
                "--data-only",
                "--column-inserts", "postgres",
            ],
        },
        "createdb": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/createdb.sql",
                "--create",
                "--no-reconnect",
                "--verbose",
                "--statistics",
                "postgres",
            ],
        },
        "data_only": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/data_only.sql",
                "--data-only",
                "--superuser", "test_superuser",
                "--disable-triggers",
                "--verbose",
                "postgres",
            ],
        },
        "defaults": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/defaults.sql",
                "--statistics",
                "postgres",
            ],
        },
        "defaults_no_public": {
            "database": "regress_pg_dump_test",
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/defaults_no_public.sql",
                "--statistics",
                "regress_pg_dump_test",
            ],
        },
        "defaults_no_public_clean": {
            "database": "regress_pg_dump_test",
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--clean",
                "--file", f"{tempdir}/defaults_no_public_clean.sql",
                "--statistics",
                "regress_pg_dump_test",
            ],
        },
        "defaults_public_owner": {
            "database": "regress_public_owner",
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/defaults_public_owner.sql",
                "--statistics",
                "regress_public_owner",
            ],
        },

        # Do not use --no-sync to give test coverage for data sync.  By
        # default, the custom format compresses its data file when compiled
        # with gzip support, and leaves them uncompressed when not.
        "defaults_custom_format": {
            "test_key": "defaults",
            "dump_cmd": [
                "pg_dump",
                "--format", "custom",
                "--file", f"{tempdir}/defaults_custom_format.dump",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--format", "custom",
                "--file", f"{tempdir}/defaults_custom_format.sql",
                "--statistics",
                f"{tempdir}/defaults_custom_format.dump",
            ],
            "command_like": {
                "command": [
                    "pg_restore", "--list",
                    f"{tempdir}/defaults_custom_format.dump",
                ],
                "expected": re.compile(
                    r"Compression: gzip" if supports_gzip
                    else r"Compression: none"
                ),
                "name": "data content is gzip-compressed by default if available",
            },
        },

        # By default, the directory format compresses its data files when
        # compiled with gzip support, and leaves them uncompressed when not.
        "defaults_dir_format": {
            "test_key": "defaults",
            "dump_cmd": [
                "pg_dump",
                "--format", "directory",
                "--file", f"{tempdir}/defaults_dir_format",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--format", "directory",
                "--file", f"{tempdir}/defaults_dir_format.sql",
                "--statistics",
                f"{tempdir}/defaults_dir_format",
            ],
            "command_like": {
                "command": [
                    "pg_restore", "--list", f"{tempdir}/defaults_dir_format",
                ],
                "expected": re.compile(
                    r"Compression: gzip" if supports_gzip
                    else r"Compression: none"
                ),
                "name": "data content is gzip-compressed by default",
            },
            "glob_patterns": [
                f"{tempdir}/defaults_dir_format/toc.dat",
                f"{tempdir}/defaults_dir_format/blobs_*.toc",
                (f"{tempdir}/defaults_dir_format/*.dat.gz" if supports_gzip
                 else f"{tempdir}/defaults_dir_format/*.dat"),
            ],
        },

        "defaults_parallel": {
            "test_key": "defaults",
            "dump_cmd": [
                "pg_dump",
                "--format", "directory",
                "--jobs", "2",
                "--file", f"{tempdir}/defaults_parallel",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file", f"{tempdir}/defaults_parallel.sql",
                "--statistics",
                f"{tempdir}/defaults_parallel",
            ],
        },

        "defaults_tar_format": {
            "test_key": "defaults",
            "dump_cmd": [
                "pg_dump",
                "--format", "tar",
                "--file", f"{tempdir}/defaults_tar_format.tar",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--format", "tar",
                "--file", f"{tempdir}/defaults_tar_format.sql",
                "--statistics",
                f"{tempdir}/defaults_tar_format.tar",
            ],
        },
        "exclude_dump_test_schema": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/exclude_dump_test_schema.sql",
                "--exclude-schema", "dump_test",
                "--statistics",
                "postgres",
            ],
        },
        "exclude_test_table": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/exclude_test_table.sql",
                "--exclude-table", "dump_test.test_table",
                "--statistics",
                "postgres",
            ],
        },
        "exclude_measurement": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/exclude_measurement.sql",
                "--exclude-table-and-children", "dump_test.measurement",
                "--statistics",
                "postgres",
            ],
        },
        "exclude_measurement_data": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/exclude_measurement_data.sql",
                "--exclude-table-data-and-children", "dump_test.measurement",
                "--no-unlogged-table-data",
                "--statistics",
                "postgres",
            ],
        },
        "exclude_test_table_data": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/exclude_test_table_data.sql",
                "--exclude-table-data", "dump_test.test_table",
                "--no-unlogged-table-data",
                "--statistics",
                "postgres",
            ],
        },
        "inserts": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/inserts.sql",
                "--data-only",
                "--inserts", "postgres",
            ],
        },
        "pg_dumpall_globals": {
            "dump_cmd": [
                "pg_dumpall",
                "--verbose",
                "--file", f"{tempdir}/pg_dumpall_globals.sql",
                "--globals-only",
                "--no-sync",
            ],
        },
        "pg_dumpall_globals_clean": {
            "dump_cmd": [
                "pg_dumpall",
                "--file", f"{tempdir}/pg_dumpall_globals_clean.sql",
                "--globals-only",
                "--clean",
                "--no-sync",
            ],
        },
        "pg_dumpall_dbprivs": {
            "dump_cmd": [
                "pg_dumpall", "--no-sync",
                "--file", f"{tempdir}/pg_dumpall_dbprivs.sql",
                "--statistics",
            ],
        },
        "pg_dumpall_exclude": {
            "dump_cmd": [
                "pg_dumpall",
                "--verbose",
                "--file", f"{tempdir}/pg_dumpall_exclude.sql",
                "--exclude-database", "*dump_test*",
                "--no-sync",
                "--statistics",
            ],
        },
        "no_toast_compression": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/no_toast_compression.sql",
                "--no-toast-compression",
                "--statistics",
                "postgres",
            ],
        },
        "no_large_objects": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/no_large_objects.sql",
                "--no-large-objects",
                "--statistics",
                "postgres",
            ],
        },
        "no_policies": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/no_policies.sql",
                "--no-policies",
                "--statistics",
                "postgres",
            ],
        },
        "no_policies_restore": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--format", "custom",
                "--file", f"{tempdir}/no_policies_restore.dump",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--format", "custom",
                "--file", f"{tempdir}/no_policies_restore.sql",
                "--no-policies",
                "--statistics",
                f"{tempdir}/no_policies_restore.dump",
            ],
        },
        "no_privs": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/no_privs.sql",
                "--no-privileges",
                "--statistics",
                "postgres",
            ],
        },
        "no_owner": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/no_owner.sql",
                "--no-owner",
                "--statistics",
                "postgres",
            ],
        },
        "no_subscriptions": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/no_subscriptions.sql",
                "--no-subscriptions",
                "--statistics",
                "postgres",
            ],
        },
        "no_subscriptions_restore": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--format", "custom",
                "--file", f"{tempdir}/no_subscriptions_restore.dump",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--format", "custom",
                "--file", f"{tempdir}/no_subscriptions_restore.sql",
                "--no-subscriptions",
                "--statistics",
                f"{tempdir}/no_subscriptions_restore.dump",
            ],
        },
        "no_table_access_method": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/no_table_access_method.sql",
                "--no-table-access-method",
                "--statistics",
                "postgres",
            ],
        },
        "only_dump_test_schema": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/only_dump_test_schema.sql",
                "--schema", "dump_test",
                "--statistics",
                "postgres",
            ],
        },
        "only_dump_test_table": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/only_dump_test_table.sql",
                "--table", "dump_test.test_table",
                "--lock-wait-timeout", _LOCK_WAIT_TIMEOUT,
                "--statistics",
                "postgres",
            ],
        },
        "only_dump_measurement": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/only_dump_measurement.sql",
                "--table-and-children", "dump_test.measurement",
                "--lock-wait-timeout", _LOCK_WAIT_TIMEOUT,
                "--statistics",
                "postgres",
            ],
        },
        "role": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/role.sql",
                "--role", "regress_dump_test_role",
                "--schema", "dump_test_second_schema",
                "--statistics",
                "postgres",
            ],
        },
        "role_parallel": {
            "test_key": "role",
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--format", "directory",
                "--jobs", "2",
                "--file", f"{tempdir}/role_parallel",
                "--role", "regress_dump_test_role",
                "--schema", "dump_test_second_schema",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file", f"{tempdir}/role_parallel.sql",
                "--statistics",
                f"{tempdir}/role_parallel",
            ],
        },
        "rows_per_insert": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/rows_per_insert.sql",
                "--data-only",
                "--rows-per-insert", "4",
                "--table", "dump_test.test_table",
                "--table", "dump_test.test_fourth_table",
                "postgres",
            ],
        },
        "schema_only": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--format", "plain",
                "--file", f"{tempdir}/schema_only.sql",
                "--schema-only",
                "postgres",
            ],
        },
        "section_pre_data": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/section_pre_data.sql",
                "--section", "pre-data",
                "--statistics",
                "postgres",
            ],
        },
        "section_data": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/section_data.sql",
                "--section", "data",
                "--statistics",
                "postgres",
            ],
        },
        "section_post_data": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/section_post_data.sql",
                "--section", "post-data",
                "--statistics",
                "postgres",
            ],
        },
        "test_schema_plus_large_objects": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                "--file", f"{tempdir}/test_schema_plus_large_objects.sql",
                "--schema", "dump_test",
                "--large-objects",
                "--no-large-objects",
                "--statistics",
                "postgres",
            ],
        },
        "no_statistics": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                f"--file={tempdir}/no_statistics.sql", "--no-statistics",
                "postgres",
            ],
        },
        "no_data_no_schema": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                f"--file={tempdir}/no_data_no_schema.sql", "--no-data",
                "--no-schema", "postgres",
                "--statistics",
            ],
        },
        "statistics_only": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                f"--file={tempdir}/statistics_only.sql", "--statistics-only",
                "postgres",
            ],
        },
        "no_schema": {
            "dump_cmd": [
                "pg_dump", "--no-sync",
                f"--file={tempdir}/no_schema.sql", "--no-schema",
                "--statistics", "postgres",
            ],
        },
    }


def _tests(full, dts):
    """Definition of the tests to run.

    *full* and *dts* are the FULL_RUNS and DUMP_TEST_SCHEMA_RUNS sets, passed
    in so callers can pre-copy them.  Each entry may have: create_order,
    create_sql (seed SQL run before any dump), regexp (compiled), like/unlike
    sets of run names, all_runs (matches every run), database (run against a
    non-default db), collation / icu (gate on build feature), catch_all (a
    documentation-only key, ignored here).

    A run listed in 'like' (or all_runs) and not in 'unlike' must match the
    regexp; every other run must not.  The definitions are assembled from
    several part functions purely to keep each function a manageable size.
    """
    tests = {}
    tests.update(_tests_part1(full, dts))
    tests.update(_tests_part2(full, dts))
    tests.update(_tests_part3(full, dts))
    return tests


def _tests_part1(full, dts):
    return {
        "restrict": {
            "all_runs": True,
            "regexp": _qr(r"^\\restrict [a-zA-Z0-9]+$", _M),
        },
        "unrestrict": {
            "all_runs": True,
            "regexp": _qr(r"^\\unrestrict [a-zA-Z0-9]+$", _M),
        },
        "ALTER DEFAULT PRIVILEGES FOR ROLE regress_dump_test_role GRANT": {
            "create_order": 14,
            "create_sql":
                "ALTER DEFAULT PRIVILEGES\n"
                "FOR ROLE regress_dump_test_role IN SCHEMA dump_test\n"
                "GRANT SELECT ON TABLES TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QALTER DEFAULT PRIVILEGES \E
                \QFOR ROLE regress_dump_test_role IN SCHEMA dump_test \E
                \QGRANT SELECT ON TABLES TO regress_dump_test_role;\E
                """, _XM),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "ALTER DEFAULT PRIVILEGES FOR ROLE regress_dump_test_role GRANT EXECUTE ON FUNCTIONS": {
            "create_order": 15,
            "create_sql":
                "ALTER DEFAULT PRIVILEGES\n"
                "FOR ROLE regress_dump_test_role IN SCHEMA dump_test\n"
                "GRANT EXECUTE ON FUNCTIONS TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QALTER DEFAULT PRIVILEGES \E
                \QFOR ROLE regress_dump_test_role IN SCHEMA dump_test \E
                \QGRANT ALL ON FUNCTIONS TO regress_dump_test_role;\E
                """, _XM),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "ALTER DEFAULT PRIVILEGES FOR ROLE regress_dump_test_role REVOKE": {
            "create_order": 55,
            "create_sql":
                "ALTER DEFAULT PRIVILEGES\n"
                "FOR ROLE regress_dump_test_role\n"
                "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;",
            "regexp": _qr(
                r"""^
                \QALTER DEFAULT PRIVILEGES \E
                \QFOR ROLE regress_dump_test_role \E
                \QREVOKE ALL ON FUNCTIONS FROM PUBLIC;\E
                """, _XM),
            "like": full | {"section_post_data"},
            "unlike": {"no_privs"},
        },
        "ALTER DEFAULT PRIVILEGES FOR ROLE regress_dump_test_role REVOKE SELECT": {
            "create_order": 56,
            "create_sql":
                "ALTER DEFAULT PRIVILEGES\n"
                "FOR ROLE regress_dump_test_role\n"
                "REVOKE SELECT ON TABLES FROM regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QALTER DEFAULT PRIVILEGES \E
                \QFOR ROLE regress_dump_test_role \E
                \QREVOKE ALL ON TABLES FROM regress_dump_test_role;\E\n
                \QALTER DEFAULT PRIVILEGES \E
                \QFOR ROLE regress_dump_test_role \E
                \QGRANT INSERT,REFERENCES,DELETE,TRIGGER,TRUNCATE,MAINTAIN,UPDATE ON TABLES TO regress_dump_test_role;\E
                """, _XM),
            "like": full | {"section_post_data"},
            "unlike": {"no_privs"},
        },
        "ALTER ROLE regress_dump_test_role": {
            "regexp": _qr(
                r"""^
                \QALTER ROLE regress_dump_test_role WITH \E
                \QNOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB NOLOGIN \E
                \QNOREPLICATION NOBYPASSRLS;\E
                """, _XM),
            "like": {
                "pg_dumpall_dbprivs",
                "pg_dumpall_globals",
                "pg_dumpall_globals_clean",
                "pg_dumpall_exclude",
            },
        },
        "ALTER COLLATION test0 OWNER TO": {
            "regexp": _qr(r"^\QALTER COLLATION public.test0 OWNER TO \E.+;", _M),
            "collation": True,
            "like": full | {"section_pre_data"},
            "unlike": {"no_owner"},
        },
        "ALTER FOREIGN DATA WRAPPER dummy OWNER TO": {
            "regexp": _qr(r"^ALTER FOREIGN DATA WRAPPER dummy OWNER TO .+;", _M),
            "like": full | {"section_pre_data"},
            "unlike": {"no_owner"},
        },
        "ALTER SERVER s1 OWNER TO": {
            "regexp": _qr(r"^ALTER SERVER s1 OWNER TO .+;", _M),
            "like": full | {"section_pre_data"},
            "unlike": {"no_owner"},
        },
        "ALTER FUNCTION dump_test.pltestlang_call_handler() OWNER TO": {
            "regexp": _qr(
                r"""^
                \QALTER FUNCTION dump_test.pltestlang_call_handler() \E
                \QOWNER TO \E
                .+;""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "only_dump_measurement",
            },
        },
        "ALTER OPERATOR FAMILY dump_test.op_family OWNER TO": {
            "regexp": _qr(
                r"""^
                \QALTER OPERATOR FAMILY dump_test.op_family USING btree \E
                \QOWNER TO \E
                .+;""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "only_dump_measurement",
            },
        },
        "ALTER OPERATOR FAMILY dump_test.op_family USING btree": {
            "create_order": 75,
            "create_sql":
                "ALTER OPERATOR FAMILY dump_test.op_family USING btree ADD\n"
                "               OPERATOR 1 <(bigint,int4),\n"
                "               OPERATOR 2 <=(bigint,int4),\n"
                "               OPERATOR 3 =(bigint,int4),\n"
                "               OPERATOR 4 >=(bigint,int4),\n"
                "               OPERATOR 5 >(bigint,int4),\n"
                "               FUNCTION 1 (int4, int4) btint4cmp(int4,int4),\n"
                "               FUNCTION 2 (int4, int4) btint4sortsupport(internal),\n"
                "               FUNCTION 4 (int4, int4) btequalimage(oid);",
            "regexp": _qr(
                r"""^
                \QALTER OPERATOR FAMILY dump_test.op_family USING btree ADD\E\n\s+
                \QOPERATOR 1 <(bigint,integer) ,\E\n\s+
                \QOPERATOR 2 <=(bigint,integer) ,\E\n\s+
                \QOPERATOR 3 =(bigint,integer) ,\E\n\s+
                \QOPERATOR 4 >=(bigint,integer) ,\E\n\s+
                \QOPERATOR 5 >(bigint,integer) ,\E\n\s+
                \QFUNCTION 1 (integer, integer) btint4cmp(integer,integer) ,\E\n\s+
                \QFUNCTION 2 (bigint, bigint) btint8sortsupport(internal) ,\E\n\s+
                \QFUNCTION 2 (integer, integer) btint4sortsupport(internal) ,\E\n\s+
                \QFUNCTION 4 (bigint, bigint) btequalimage(oid) ,\E\n\s+
                \QFUNCTION 4 (integer, integer) btequalimage(oid);\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "ALTER OPERATOR CLASS dump_test.op_class OWNER TO": {
            "regexp": _qr(
                r"""^
                \QALTER OPERATOR CLASS dump_test.op_class USING btree \E
                \QOWNER TO \E
                .+;""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "only_dump_measurement",
            },
        },
        "ALTER PUBLICATION pub1 OWNER TO": {
            "regexp": _qr(r"^ALTER PUBLICATION pub1 OWNER TO .+;", _M),
            "like": full | {"section_post_data"},
            "unlike": {"no_owner"},
        },
        "ALTER LARGE OBJECT ... OWNER TO": {
            "regexp": _qr(r"^ALTER LARGE OBJECT \d+ OWNER TO .+;", _M),
            "like": full | {
                "column_inserts",
                "data_only",
                "inserts",
                "no_schema",
                "section_data",
                "test_schema_plus_large_objects",
            },
            "unlike": {
                "binary_upgrade",
                "no_large_objects",
                "no_owner",
                "schema_only",
                "schema_only_with_statistics",
            },
        },
        "ALTER PROCEDURAL LANGUAGE pltestlang OWNER TO": {
            "regexp": _qr(r"^ALTER PROCEDURAL LANGUAGE pltestlang OWNER TO .+;", _M),
            "like": full | {"section_pre_data"},
            "unlike": {"no_owner"},
        },
        "ALTER SCHEMA dump_test OWNER TO": {
            "regexp": _qr(r"^ALTER SCHEMA dump_test OWNER TO .+;", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "only_dump_measurement",
            },
        },
        "ALTER SCHEMA dump_test_second_schema OWNER TO": {
            "regexp": _qr(r"^ALTER SCHEMA dump_test_second_schema OWNER TO .+;", _M),
            "like": full | {"role", "section_pre_data"},
            "unlike": {"no_owner"},
        },
        "ALTER SCHEMA public OWNER TO": {
            "create_order": 15,
            "create_sql":
                'ALTER SCHEMA public OWNER TO "regress_quoted  \\"" role";',
            "regexp": _qr(r"^ALTER SCHEMA public OWNER TO .+;", _M),
            "like": full | {"section_pre_data"},
            "unlike": {"no_owner"},
        },
        "ALTER SCHEMA public OWNER TO (w/o ACL changes)": {
            "database": "regress_public_owner",
            "create_order": 100,
            "create_sql":
                'ALTER SCHEMA public OWNER TO "regress_quoted  \\"" role";',
            "regexp": _qr(r"^(GRANT|REVOKE)", _M),
            "like": set(),
        },
        "ALTER SEQUENCE test_table_col1_seq": {
            "regexp": _qr(
                r"""^
                \QALTER SEQUENCE dump_test.test_table_col1_seq OWNED BY dump_test.test_table.col1;\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE ONLY test_table ADD CONSTRAINT ... PRIMARY KEY": {
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.test_table\E \n^\s+
                \QADD CONSTRAINT test_table_pkey PRIMARY KEY (col1);\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "CONSTRAINT NOT NULL / NOT VALID": {
            "create_sql":
                "CREATE TABLE dump_test.test_table_nn (\n"
                "    col1 int);\n"
                "CREATE TABLE dump_test.test_table_nn_2 (\n"
                "    col1 int NOT NULL);\n"
                "CREATE TABLE dump_test.test_table_nn_chld1 (\n"
                ") INHERITS (dump_test.test_table_nn);\n"
                "CREATE TABLE dump_test.test_table_nn_chld2 (\n"
                "    col1 int\n"
                ") INHERITS (dump_test.test_table_nn);\n"
                "CREATE TABLE dump_test.test_table_nn_chld3 (\n"
                ") INHERITS (dump_test.test_table_nn, dump_test.test_table_nn_2);\n"
                "ALTER TABLE dump_test.test_table_nn ADD CONSTRAINT nn NOT NULL col1 NOT VALID;\n"
                "ALTER TABLE dump_test.test_table_nn_chld1 VALIDATE CONSTRAINT nn;\n"
                "ALTER TABLE dump_test.test_table_nn_chld2 VALIDATE CONSTRAINT nn;\n"
                "COMMENT ON CONSTRAINT nn ON dump_test.test_table_nn IS 'nn comment is valid';\n"
                "COMMENT ON CONSTRAINT nn ON dump_test.test_table_nn_chld2 IS 'nn_chld2 comment is valid';",
            "regexp": _qr(
                r"""^
                \QALTER TABLE dump_test.test_table_nn\E \n^\s+
                \QADD CONSTRAINT nn NOT NULL col1 NOT VALID;\E
                """, _XM),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON CONSTRAINT ON test_table_nn": {
            "regexp": _qr(
                r"""^
                \QCOMMENT ON CONSTRAINT nn ON dump_test.test_table_nn IS\E
                """, _XM),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON CONSTRAINT ON test_table_chld2": {
            "regexp": _qr(
                r"""^
                \QCOMMENT ON CONSTRAINT nn ON dump_test.test_table_nn_chld2 IS\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CONSTRAINT NOT NULL / NOT VALID (child1)": {
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_nn_chld1 (\E\n
                ^\s+\QCONSTRAINT nn NOT NULL col1\E$
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
                "binary_upgrade",
            },
        },
        "CONSTRAINT NOT NULL / NOT VALID (child2)": {
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_nn_chld2 (\E\n
                ^\s+\Qcol1 integer CONSTRAINT nn NOT NULL\E$
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CONSTRAINT NOT NULL / NOT VALID (child3)": {
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_nn_chld3 (\E\n
                ^\Q)\E$
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
                "binary_upgrade",
            },
        },
        "CONSTRAINT NOT NULL / NO INHERIT": {
            "create_sql":
                "CREATE TABLE dump_test.test_table_nonn (\n"
                "col1 int NOT NULL NO INHERIT,\n"
                "col2 int);\n"
                "CREATE TABLE dump_test.test_table_nonn_chld1 (\n"
                "   CONSTRAINT nn NOT NULL col2 NO INHERIT)\n"
                "INHERITS (dump_test.test_table_nonn); ",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_nonn (\E \n^\s+
                \Qcol1 integer NOT NULL NO INHERIT\E
                """, _XM),
            "like": full | dts | {
                "section_pre_data",
                "binary_upgrade",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CONSTRAINT NOT NULL / NO INHERIT (child1)": {
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_nonn_chld1 (\E \n^\s+
                \QCONSTRAINT nn NOT NULL col2 NO INHERIT\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
                "binary_upgrade",
            },
        },
        "CONSTRAINT PRIMARY KEY / WITHOUT OVERLAPS": {
            "create_sql":
                "CREATE TABLE dump_test.test_table_tpk (\n"
                "    col1 int4range,\n"
                "    col2 tstzrange,\n"
                "    CONSTRAINT test_table_tpk_pkey PRIMARY KEY (col1, col2 WITHOUT OVERLAPS));",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.test_table_tpk\E \n^\s+
                \QADD CONSTRAINT test_table_tpk_pkey PRIMARY KEY (col1, col2 WITHOUT OVERLAPS);\E
                """, _XM),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CONSTRAINT UNIQUE / WITHOUT OVERLAPS": {
            "create_sql":
                "CREATE TABLE dump_test.test_table_tuq (\n"
                "    col1 int4range,\n"
                "    col2 tstzrange,\n"
                "    CONSTRAINT test_table_tuq_uq UNIQUE (col1, col2 WITHOUT OVERLAPS));",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.test_table_tuq\E \n^\s+
                \QADD CONSTRAINT test_table_tuq_uq UNIQUE (col1, col2 WITHOUT OVERLAPS);\E
                """, _XM),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE (partitioned) ADD CONSTRAINT ... FOREIGN KEY": {
            "create_order": 4,
            "create_sql":
                "CREATE TABLE dump_test.test_table_fk (\n"
                "    col1 int references dump_test.test_table)\n"
                "    PARTITION BY RANGE (col1);\n"
                "    CREATE TABLE dump_test.test_table_fk_1\n"
                "    PARTITION OF dump_test.test_table_fk\n"
                "    FOR VALUES FROM (0) TO (10);",
            "regexp": _qr(
                r"""
                \QADD CONSTRAINT test_table_fk_col1_fkey FOREIGN KEY (col1) REFERENCES dump_test.test_table\E
                """, _XM),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE ONLY test_table ALTER COLUMN col1 SET STATISTICS 90": {
            "create_order": 93,
            "create_sql":
                "ALTER TABLE dump_test.test_table ALTER COLUMN col1 SET STATISTICS 90;",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.test_table ALTER COLUMN col1 SET STATISTICS 90;\E\n
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE ONLY test_table ALTER COLUMN col2 SET STORAGE": {
            "create_order": 94,
            "create_sql":
                "ALTER TABLE dump_test.test_table ALTER COLUMN col2 SET STORAGE EXTERNAL;",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.test_table ALTER COLUMN col2 SET STORAGE EXTERNAL;\E\n
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE ONLY test_table ALTER COLUMN col3 SET STORAGE": {
            "create_order": 95,
            "create_sql":
                "ALTER TABLE dump_test.test_table ALTER COLUMN col3 SET STORAGE MAIN;",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.test_table ALTER COLUMN col3 SET STORAGE MAIN;\E\n
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE ONLY test_table ALTER COLUMN col4 SET n_distinct": {
            "create_order": 95,
            "create_sql":
                "ALTER TABLE dump_test.test_table ALTER COLUMN col4 SET (n_distinct = 10);",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.test_table ALTER COLUMN col4 SET (n_distinct=10);\E\n
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE ONLY dump_test.measurement ATTACH PARTITION measurement_y2006m2": {
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.measurement ATTACH PARTITION dump_test_second_schema.measurement_y2006m2 \E
                \QFOR VALUES FROM ('2006-02-01') TO ('2006-03-01');\E\n
                """, _XM),
            "like": full | {
                "role",
                "section_pre_data",
                "binary_upgrade",
                "only_dump_measurement",
            },
            "unlike": {"exclude_measurement"},
        },
        "ALTER TABLE test_table CLUSTER ON test_table_pkey": {
            "create_order": 96,
            "create_sql":
                "ALTER TABLE dump_test.test_table CLUSTER ON test_table_pkey",
            "regexp": _qr(
                r"""^
                \QALTER TABLE dump_test.test_table CLUSTER ON test_table_pkey;\E\n
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE test_table DISABLE TRIGGER ALL": {
            "regexp": _qr(
                r"""^
                \QSET SESSION AUTHORIZATION 'test_superuser';\E\n\n
                \QALTER TABLE dump_test.test_table DISABLE TRIGGER ALL;\E\n\n
                \QCOPY dump_test.test_table (col1, col2, col3, col4) FROM stdin;\E
                \n(?:\d\t\\N\t\\N\t\\N\n){9}\\\.\n\n\n
                \QALTER TABLE dump_test.test_table ENABLE TRIGGER ALL;\E""", _XM),
            "like": {"data_only"},
        },
        "ALTER FOREIGN TABLE foreign_table ALTER COLUMN c1 OPTIONS": {
            "regexp": _qr(
                r"""^
                \QALTER FOREIGN TABLE ONLY dump_test.foreign_table ALTER COLUMN c1 OPTIONS (\E\n
                \s+\Qcolumn_name 'col1'\E\n
                \Q);\E\n
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE test_table OWNER TO": {
            "regexp": _qr(r"^\QALTER TABLE dump_test.test_table OWNER TO \E.+;", _M),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
                "no_owner",
            },
        },
        "ALTER TABLE test_table ENABLE ROW LEVEL SECURITY": {
            "create_order": 23,
            "create_sql":
                "ALTER TABLE dump_test.test_table\n"
                "ENABLE ROW LEVEL SECURITY;",
            "regexp": _qr(
                r"^\QALTER TABLE dump_test.test_table ENABLE ROW LEVEL SECURITY;\E", _M),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_policies",
                "no_policies_restore",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE test_second_table OWNER TO": {
            "regexp": _qr(r"^\QALTER TABLE dump_test.test_second_table OWNER TO \E.+;", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE measurement OWNER TO": {
            "regexp": _qr(r"^\QALTER TABLE dump_test.measurement OWNER TO \E.+;", _M),
            "like": full | dts | {
                "section_pre_data",
                "only_dump_measurement",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "exclude_measurement",
            },
        },
        "ALTER TABLE measurement_y2006m2 OWNER TO": {
            "regexp": _qr(
                r"^\QALTER TABLE dump_test_second_schema.measurement_y2006m2 OWNER TO \E.+;", _M),
            "like": full | {
                "role",
                "section_pre_data",
                "only_dump_measurement",
            },
            "unlike": {
                "no_owner",
                "exclude_measurement",
            },
        },
        "ALTER FOREIGN TABLE foreign_table OWNER TO": {
            "regexp": _qr(r"^\QALTER FOREIGN TABLE dump_test.foreign_table OWNER TO \E.+;", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "only_dump_measurement",
            },
        },
        "ALTER TEXT SEARCH CONFIGURATION alt_ts_conf1 OWNER TO": {
            "regexp": _qr(
                r"^\QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1 OWNER TO \E.+;", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "only_dump_measurement",
            },
        },
        "ALTER TEXT SEARCH DICTIONARY alt_ts_dict1 OWNER TO": {
            "regexp": _qr(
                r"^\QALTER TEXT SEARCH DICTIONARY dump_test.alt_ts_dict1 OWNER TO \E.+;", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_owner",
                "only_dump_measurement",
            },
        },
        "LO create (using lo_from_bytea)": {
            "create_order": 50,
            "create_sql":
                "SELECT pg_catalog.lo_from_bytea(0, "
                "'\\x310a320a330a340a350a360a370a380a390a');",
            "regexp": _qr(r"^SELECT pg_catalog\.lo_create\('\d+'\);", _M),
            "like": full | {
                "column_inserts",
                "data_only",
                "inserts",
                "no_schema",
                "section_data",
                "test_schema_plus_large_objects",
            },
            "unlike": {
                "binary_upgrade",
                "schema_only",
                "schema_only_with_statistics",
                "no_large_objects",
            },
        },
        "LO load (using lo_from_bytea)": {
            "regexp": _qr(
                r"""^
                \QSELECT pg_catalog.lo_open\E \('\d+',\ \d+\);\n
                \QSELECT pg_catalog.lowrite(0, \E
                \Q'\x310a320a330a340a350a360a370a380a390a');\E\n
                \QSELECT pg_catalog.lo_close(0);\E
                """, _XM),
            "like": full | {
                "column_inserts",
                "data_only",
                "inserts",
                "no_schema",
                "section_data",
                "test_schema_plus_large_objects",
            },
            "unlike": {
                "binary_upgrade",
                "no_large_objects",
                "schema_only",
                "schema_only_with_statistics",
            },
        },
        "LO create (with no data)": {
            "create_sql": "SELECT pg_catalog.lo_create(0);",
            "regexp": _qr(
                r"""^
                \QSELECT pg_catalog.lo_open\E \('\d+',\ \d+\);\n
                \QSELECT pg_catalog.lo_close(0);\E
                """, _XM),
            "like": full | {
                "column_inserts",
                "data_only",
                "inserts",
                "no_schema",
                "section_data",
                "test_schema_plus_large_objects",
            },
            "unlike": {
                "binary_upgrade",
                "no_large_objects",
                "schema_only",
                "schema_only_with_statistics",
            },
        },
        "COMMENT ON DATABASE postgres": {
            "regexp": _qr(r"^COMMENT ON DATABASE postgres IS .+;", _M),
            "like": {"createdb"},
        },
        "COMMENT ON EXTENSION plpgsql": {
            "regexp": _qr(r"^COMMENT ON EXTENSION plpgsql IS .+;", _M),
            "like": set(),
        },
        "COMMENT ON SCHEMA public": {
            "regexp": _qr(r"^COMMENT ON SCHEMA public IS .+;", _M),
            "like": {
                "pg_dumpall_dbprivs",
                "pg_dumpall_exclude",
            },
        },
        "COMMENT ON SCHEMA public IS NULL": {
            "database": "regress_public_owner",
            "create_order": 100,
            "create_sql": "COMMENT ON SCHEMA public IS NULL;",
            "regexp": _qr(r"^COMMENT ON SCHEMA public IS '';", _M),
            "like": {"defaults_public_owner"},
        },
    }


def _tests_part2(full, dts):
    return {
        "COMMENT ON TABLE dump_test.test_table": {
            "create_order": 36,
            "create_sql":
                "COMMENT ON TABLE dump_test.test_table\n"
                "IS 'comment on table';",
            "regexp": _qr(
                r"^\QCOMMENT ON TABLE dump_test.test_table IS 'comment on table';\E", _M),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "COMMENT ON COLUMN dump_test.test_table.col1": {
            "create_order": 36,
            "create_sql":
                "COMMENT ON COLUMN dump_test.test_table.col1\n"
                "IS 'comment on column';",
            "regexp": _qr(
                r"""^
                \QCOMMENT ON COLUMN dump_test.test_table.col1 IS 'comment on column';\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "COMMENT ON COLUMN dump_test.composite.f1": {
            "create_order": 44,
            "create_sql":
                "COMMENT ON COLUMN dump_test.composite.f1\n"
                "IS 'comment on column of type';",
            "regexp": _qr(
                r"""^
                \QCOMMENT ON COLUMN dump_test.composite.f1 IS 'comment on column of type';\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON COLUMN dump_test.test_second_table.col1": {
            "create_order": 63,
            "create_sql":
                "COMMENT ON COLUMN dump_test.test_second_table.col1\n"
                "IS 'comment on column col1';",
            "regexp": _qr(
                r"""^
                \QCOMMENT ON COLUMN dump_test.test_second_table.col1 IS 'comment on column col1';\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON COLUMN dump_test.test_second_table.col2": {
            "create_order": 64,
            "create_sql":
                "COMMENT ON COLUMN dump_test.test_second_table.col2\n"
                "IS 'comment on column col2';",
            "regexp": _qr(
                r"""^
                \QCOMMENT ON COLUMN dump_test.test_second_table.col2 IS 'comment on column col2';\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON CONVERSION dump_test.test_conversion": {
            "create_order": 79,
            "create_sql":
                "COMMENT ON CONVERSION dump_test.test_conversion\n"
                "IS 'comment on test conversion';",
            "regexp": _qr(
                r"^\QCOMMENT ON CONVERSION dump_test.test_conversion IS 'comment on test conversion';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON COLLATION test0": {
            "create_order": 77,
            "create_sql":
                "COMMENT ON COLLATION test0\n"
                "IS 'comment on test0 collation';",
            "regexp": _qr(
                r"^\QCOMMENT ON COLLATION public.test0 IS 'comment on test0 collation';\E", _M),
            "collation": True,
            "like": full | {"section_pre_data"},
        },
        "COMMENT ON LARGE OBJECT ...": {
            "create_order": 65,
            "create_sql":
                "DO $$\n"
                " DECLARE myoid oid;\n"
                " BEGIN\n"
                "    SELECT loid FROM pg_largeobject INTO myoid;\n"
                "    EXECUTE 'COMMENT ON LARGE OBJECT ' || myoid || ' IS ''comment on large object'';';\n"
                " END;\n"
                " $$;",
            "regexp": _qr(
                r"""^
                \QCOMMENT ON LARGE OBJECT \E[0-9]+\Q IS 'comment on large object';\E
                """, _XM),
            "like": full | {
                "column_inserts",
                "data_only",
                "inserts",
                "no_schema",
                "section_data",
                "test_schema_plus_large_objects",
            },
            "unlike": {
                "no_large_objects",
                "schema_only",
                "schema_only_with_statistics",
            },
        },
        "COMMENT ON POLICY p1": {
            "create_order": 55,
            "create_sql":
                "COMMENT ON POLICY p1 ON dump_test.test_table\n"
                "IS 'comment on policy';",
            "regexp": _qr(
                r"^COMMENT ON POLICY p1 ON dump_test.test_table IS 'comment on policy';", _M),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_policies",
                "no_policies_restore",
                "only_dump_measurement",
            },
        },
        "COMMENT ON PUBLICATION pub1": {
            "create_order": 55,
            "create_sql":
                "COMMENT ON PUBLICATION pub1\n"
                "IS 'comment on publication';",
            "regexp": _qr(
                r"^COMMENT ON PUBLICATION pub1 IS 'comment on publication';", _M),
            "like": full | {"section_post_data"},
        },
        "COMMENT ON SUBSCRIPTION sub1": {
            "create_order": 55,
            "create_sql":
                "COMMENT ON SUBSCRIPTION sub1\n"
                "IS 'comment on subscription';",
            "regexp": _qr(
                r"^COMMENT ON SUBSCRIPTION sub1 IS 'comment on subscription';", _M),
            "like": full | {"section_post_data"},
            "unlike": {
                "no_subscriptions",
                "no_subscriptions_restore",
            },
        },
        "COMMENT ON TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1": {
            "create_order": 84,
            "create_sql":
                "COMMENT ON TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\n"
                "IS 'comment on text search configuration';",
            "regexp": _qr(
                r"^\QCOMMENT ON TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1 IS 'comment on text search configuration';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON TEXT SEARCH DICTIONARY dump_test.alt_ts_dict1": {
            "create_order": 84,
            "create_sql":
                "COMMENT ON TEXT SEARCH DICTIONARY dump_test.alt_ts_dict1\n"
                "IS 'comment on text search dictionary';",
            "regexp": _qr(
                r"^\QCOMMENT ON TEXT SEARCH DICTIONARY dump_test.alt_ts_dict1 IS 'comment on text search dictionary';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON TEXT SEARCH PARSER dump_test.alt_ts_prs1": {
            "create_order": 84,
            "create_sql":
                "COMMENT ON TEXT SEARCH PARSER dump_test.alt_ts_prs1\n"
                "IS 'comment on text search parser';",
            "regexp": _qr(
                r"^\QCOMMENT ON TEXT SEARCH PARSER dump_test.alt_ts_prs1 IS 'comment on text search parser';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON TEXT SEARCH TEMPLATE dump_test.alt_ts_temp1": {
            "create_order": 84,
            "create_sql":
                "COMMENT ON TEXT SEARCH TEMPLATE dump_test.alt_ts_temp1\n"
                "IS 'comment on text search template';",
            "regexp": _qr(
                r"^\QCOMMENT ON TEXT SEARCH TEMPLATE dump_test.alt_ts_temp1 IS 'comment on text search template';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON TYPE dump_test.planets - ENUM": {
            "create_order": 68,
            "create_sql":
                "COMMENT ON TYPE dump_test.planets\n"
                "IS 'comment on enum type';",
            "regexp": _qr(
                r"^\QCOMMENT ON TYPE dump_test.planets IS 'comment on enum type';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON TYPE dump_test.textrange - RANGE": {
            "create_order": 69,
            "create_sql":
                "COMMENT ON TYPE dump_test.textrange\n"
                "IS 'comment on range type';",
            "regexp": _qr(
                r"^\QCOMMENT ON TYPE dump_test.textrange IS 'comment on range type';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON TYPE dump_test.int42 - Regular": {
            "create_order": 70,
            "create_sql":
                "COMMENT ON TYPE dump_test.int42\n"
                "IS 'comment on regular type';",
            "regexp": _qr(
                r"^\QCOMMENT ON TYPE dump_test.int42 IS 'comment on regular type';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON TYPE dump_test.undefined - Undefined": {
            "create_order": 71,
            "create_sql":
                "COMMENT ON TYPE dump_test.undefined\n"
                "IS 'comment on undefined type';",
            "regexp": _qr(
                r"^\QCOMMENT ON TYPE dump_test.undefined IS 'comment on undefined type';\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COPY test_table": {
            "create_order": 4,
            "create_sql":
                "INSERT INTO dump_test.test_table (col1) "
                "SELECT generate_series FROM generate_series(1,9);",
            "regexp": _qr(
                r"""^
                \QCOPY dump_test.test_table (col1, col2, col3, col4) FROM stdin;\E
                \n(?:\d\t\\N\t\\N\t\\N\n){9}\\\.\n
                """, _XM),
            "like": full | dts | {
                "data_only",
                "no_schema",
                "only_dump_test_table",
                "section_data",
            },
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "exclude_test_table",
                "exclude_test_table_data",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "COPY fk_reference_test_table": {
            "create_order": 22,
            "create_sql":
                "INSERT INTO dump_test.fk_reference_test_table (col1) "
                "SELECT generate_series FROM generate_series(1,5);",
            "regexp": _qr(
                r"""^
                \QCOPY dump_test.fk_reference_test_table (col1) FROM stdin;\E
                \n(?:\d\n){5}\\\.\n
                """, _XM),
            "like": full | dts | {
                "data_only",
                "exclude_test_table",
                "exclude_test_table_data",
                "no_schema",
                "section_data",
            },
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "COPY fk_reference_test_table second": {
            "regexp": _qr(
                r"""^
                \QCOPY dump_test.test_table (col1, col2, col3, col4) FROM stdin;\E
                \n(?:\d\t\\N\t\\N\t\\N\n){9}\\\.\n.*
                \QCOPY dump_test.fk_reference_test_table (col1) FROM stdin;\E
                \n(?:\d\n){5}\\\.\n
                """, _XMS),
            "like": {
                "data_only",
                "no_schema",
            },
        },
        "COPY test_second_table": {
            "create_order": 7,
            "create_sql":
                "INSERT INTO dump_test.test_second_table (col1, col2) "
                "SELECT generate_series, generate_series::text "
                "FROM generate_series(1,9);",
            "regexp": _qr(
                r"""^
                \QCOPY dump_test.test_second_table (col1, col2) FROM stdin;\E
                \n(?:\d\t\d\n){9}\\\.\n
                """, _XM),
            "like": full | dts | {
                "data_only",
                "no_schema",
                "section_data",
            },
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "COPY test_third_table": {
            "create_order": 7,
            "create_sql":
                "INSERT INTO dump_test.test_third_table VALUES (123, DEFAULT, 456);",
            "regexp": _qr(
                r"""^
                \QCOPY dump_test.test_third_table (f1, "F3") FROM stdin;\E
                \n123\t456\n\\\.\n
                """, _XM),
            "like": full | dts | {
                "data_only",
                "no_schema",
                "section_data",
            },
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "COPY test_fourth_table": {
            "create_order": 7,
            "create_sql":
                "INSERT INTO dump_test.test_fourth_table DEFAULT VALUES;"
                "INSERT INTO dump_test.test_fourth_table DEFAULT VALUES;",
            "regexp": _qr(
                r"""^
                \QCOPY dump_test.test_fourth_table  FROM stdin;\E
                \n\n\n\\\.\n
                """, _XM),
            "like": full | dts | {
                "data_only",
                "no_schema",
                "section_data",
            },
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "COPY test_fifth_table": {
            "create_order": 54,
            "create_sql":
                "INSERT INTO dump_test.test_fifth_table VALUES (NULL, true, false, '11001'::bit(5), 'NaN');",
            "regexp": _qr(
                r"""^
                \QCOPY dump_test.test_fifth_table (col1, col2, col3, col4, col5) FROM stdin;\E
                \n\\N\tt\tf\t11001\tNaN\n\\\.\n
                """, _XM),
            "like": full | dts | {
                "data_only",
                "no_schema",
                "section_data",
            },
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "COPY test_table_identity": {
            "create_order": 54,
            "create_sql":
                "INSERT INTO dump_test.test_table_identity (col2) VALUES ('test');",
            "regexp": _qr(
                r"""^
                \QCOPY dump_test.test_table_identity (col1, col2) FROM stdin;\E
                \n1\ttest\n\\\.\n
                """, _XM),
            "like": full | dts | {
                "data_only",
                "no_schema",
                "section_data",
            },
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "INSERT INTO test_table": {
            "regexp": _qr(
                r"""^
                (?:INSERT\ INTO\ dump_test\.test_table\ \(col1,\ col2,\ col3,\ col4\)\ VALUES\ \(\d,\ NULL,\ NULL,\ NULL\);\n){9}
                """, _XM),
            "like": {"column_inserts"},
        },
        "test_table with 4-row INSERTs": {
            "regexp": _qr(
                r"""^
                (?:
                    INSERT\ INTO\ dump_test\.test_table\ VALUES\n
                    (?:\t\(\d,\ NULL,\ NULL,\ NULL\),\n){3}
                    \t\(\d,\ NULL,\ NULL,\ NULL\);\n
                ){2}
                INSERT\ INTO\ dump_test\.test_table\ VALUES\n
                \t\(\d,\ NULL,\ NULL,\ NULL\);
                """, _XM),
            "like": {"rows_per_insert"},
        },
        "INSERT INTO test_second_table": {
            "regexp": _qr(
                r"""^
                (?:INSERT\ INTO\ dump_test\.test_second_table\ \(col1,\ col2\)
                   \ VALUES\ \(\d,\ '\d'\);\n){9}""", _XM),
            "like": {"column_inserts"},
        },
        "INSERT INTO test_third_table (colnames)": {
            "regexp": _qr(
                r'^INSERT INTO dump_test\.test_third_table \(f1, "F3"\) VALUES \(123, 456\);\n', _M),
            "like": {"column_inserts"},
        },
        "INSERT INTO test_third_table": {
            "regexp": _qr(
                r"^INSERT INTO dump_test\.test_third_table VALUES \(123, DEFAULT, 456, DEFAULT\);\n", _M),
            "like": {"inserts"},
        },
        "INSERT INTO test_fourth_table": {
            "regexp": _qr(
                r"^(?:INSERT INTO dump_test\.test_fourth_table DEFAULT VALUES;\n){2}", _M),
            "like": {"column_inserts", "inserts", "rows_per_insert"},
        },
        "INSERT INTO test_fifth_table": {
            "regexp": _qr(
                r"^\QINSERT INTO dump_test.test_fifth_table (col1, col2, col3, col4, col5) VALUES (NULL, true, false, B'11001', 'NaN');\E", _M),
            "like": {"column_inserts"},
        },
        "INSERT INTO test_table_identity": {
            "regexp": _qr(
                r"^\QINSERT INTO dump_test.test_table_identity (col1, col2) OVERRIDING SYSTEM VALUE VALUES (1, 'test');\E", _M),
            "like": {"column_inserts"},
        },
        "CREATE ROLE regress_dump_test_role": {
            "create_order": 1,
            "create_sql": "CREATE ROLE regress_dump_test_role;",
            "regexp": _qr(r"^CREATE ROLE regress_dump_test_role;", _M),
            "like": {
                "pg_dumpall_dbprivs",
                "pg_dumpall_exclude",
                "pg_dumpall_globals",
                "pg_dumpall_globals_clean",
            },
        },
        "CREATE ROLE regress_quoted...": {
            "create_order": 1,
            "create_sql": 'CREATE ROLE "regress_quoted  \\"" role";',
            "regexp": _qr(r'^CREATE ROLE "regress_quoted  \\"" role";', _M),
            "like": {
                "pg_dumpall_dbprivs",
                "pg_dumpall_exclude",
                "pg_dumpall_globals",
                "pg_dumpall_globals_clean",
            },
        },
        "newline of table name in comment": {
            "create_sql":
                '-- meet getPartitioningInfo() "unsafe" condition\n'
                " CREATE TYPE pp_colors AS\n"
                "    ENUM ('green', 'blue', 'black');\n"
                " CREATE TABLE pp_enumpart (a pp_colors)\n"
                "    PARTITION BY HASH (a);\n"
                " CREATE TABLE pp_enumpart1 PARTITION OF pp_enumpart\n"
                "    FOR VALUES WITH (MODULUS 2, REMAINDER 0);\n"
                " CREATE TABLE pp_enumpart2 PARTITION OF pp_enumpart\n"
                "    FOR VALUES WITH (MODULUS 2, REMAINDER 1);\n"
                " ALTER TABLE pp_enumpart\n"
                '    RENAME TO "pp_enumpart\nattack";',
            "regexp": _qr(r"\n--[^\n]*\nattack", _S),
            "like": set(),
        },
        "CREATE TABLESPACE regress_dump_tablespace": {
            "create_order": 2,
            "create_sql":
                "\n"
                "    SET allow_in_place_tablespaces = on;\n"
                "CREATE TABLESPACE regress_dump_tablespace\n"
                "OWNER regress_dump_test_role LOCATION ''",
            "regexp": _qr(
                r"^CREATE TABLESPACE regress_dump_tablespace OWNER regress_dump_test_role LOCATION '';", _M),
            "like": {
                "pg_dumpall_dbprivs",
                "pg_dumpall_exclude",
                "pg_dumpall_globals",
                "pg_dumpall_globals_clean",
            },
        },
        "CREATE DATABASE regression_invalid...": {
            "create_order": 1,
            "create_sql":
                "\n"
                "    CREATE DATABASE regression_invalid;\n"
                "UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'regression_invalid'",
            "regexp": _qr(r"^CREATE DATABASE regression_invalid", _M),
            "like": set(),
        },
        "CREATE ACCESS METHOD gist2": {
            "create_order": 52,
            "create_sql":
                "CREATE ACCESS METHOD gist2 TYPE INDEX HANDLER gisthandler;",
            "regexp": _qr(
                r"CREATE ACCESS METHOD gist2 TYPE INDEX HANDLER gisthandler;", _M),
            "like": full | {"section_pre_data"},
        },
        "CREATE COLLATION test0 FROM \"C\"": {
            "create_order": 76,
            "create_sql": 'CREATE COLLATION test0 FROM "C";',
            "regexp": _qr(
                r"CREATE COLLATION public.test0 \(provider = libc, locale = 'C'(, version = '[^']*')?\);", _M),
            "collation": True,
            "like": full | {"section_pre_data"},
        },
        "CREATE COLLATION icu_collation": {
            "create_order": 76,
            "create_sql":
                "CREATE COLLATION icu_collation (PROVIDER = icu, LOCALE = 'en-US-u-va-posix');",
            "regexp": _qr(
                r"CREATE COLLATION public.icu_collation \(provider = icu, locale = 'en-US-u-va-posix'(, version = '[^']*')?\);", _M),
            "icu": True,
            "like": full | {"section_pre_data"},
        },
        "CREATE CAST FOR timestamptz": {
            "create_order": 51,
            "create_sql":
                "CREATE CAST (timestamptz AS interval) WITH FUNCTION age(timestamptz) AS ASSIGNMENT;",
            "regexp": _qr(
                r"CREATE CAST \(timestamp with time zone AS interval\) WITH FUNCTION pg_catalog\.age\(timestamp with time zone\) AS ASSIGNMENT;", _M),
            "like": full | {"section_pre_data"},
        },
        "CREATE DATABASE postgres": {
            "regexp": _qr(
                r"""^
                \QCREATE DATABASE postgres WITH TEMPLATE = template0 \E
                .+;""", _XM),
            "like": {"createdb"},
        },
        "CREATE DATABASE dump_test": {
            "create_order": 47,
            "create_sql": "CREATE DATABASE dump_test;",
            "regexp": _qr(
                r"""^
                \QCREATE DATABASE dump_test WITH TEMPLATE = template0 \E
                .+;""", _XM),
            "like": {"pg_dumpall_dbprivs"},
        },
        "CREATE DATABASE dump_test2 LOCALE = 'C'": {
            "create_order": 47,
            "create_sql":
                "CREATE DATABASE dump_test2 LOCALE = 'C' TEMPLATE = template0;",
            "regexp": _qr(
                r"""^
                \QCREATE DATABASE dump_test2 \E.*\QLOCALE = 'C';\E
                """, _XM),
            "like": {"pg_dumpall_dbprivs"},
        },
        "CREATE EXTENSION ... plpgsql": {
            "regexp": _qr(
                r"""^
                \QCREATE EXTENSION IF NOT EXISTS plpgsql WITH SCHEMA pg_catalog;\E
                """, _XM),
            "like": set(),
        },
        "CREATE AGGREGATE dump_test.newavg": {
            "create_order": 25,
            "create_sql":
                "CREATE AGGREGATE dump_test.newavg (\n"
                "  sfunc = int4_avg_accum,\n"
                "  basetype = int4,\n"
                "  stype = _int8,\n"
                "  finalfunc = int8_avg,\n"
                "  finalfunc_modify = shareable,\n"
                "  initcond1 = '{0,0}'\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE AGGREGATE dump_test.newavg(integer) (\E
                \n\s+\QSFUNC = int4_avg_accum,\E
                \n\s+\QSTYPE = bigint[],\E
                \n\s+\QINITCOND = '{0,0}',\E
                \n\s+\QFINALFUNC = int8_avg,\E
                \n\s+\QFINALFUNC_MODIFY = SHAREABLE\E
                \n\);""", _XM),
            "like": full | dts | {
                "exclude_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE CONVERSION dump_test.test_conversion": {
            "create_order": 78,
            "create_sql":
                "CREATE DEFAULT CONVERSION dump_test.test_conversion FOR 'LATIN1' TO 'UTF8' FROM iso8859_1_to_utf8;",
            "regexp": _qr(
                r"^\QCREATE DEFAULT CONVERSION dump_test.test_conversion FOR 'LATIN1' TO 'UTF8' FROM iso8859_1_to_utf8;\E", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE DOMAIN dump_test.us_postal_code": {
            "create_order": 29,
            "create_sql":
                "CREATE DOMAIN dump_test.us_postal_code AS TEXT\n"
                "               COLLATE \"C\"\n"
                "DEFAULT '10014'\n"
                "CONSTRAINT nn NOT NULL\n"
                "CHECK(VALUE ~ '^\\d{5}$' OR\n"
                "      VALUE ~ '^\\d{5}-\\d{4}$');\n"
                "COMMENT ON CONSTRAINT nn\n"
                "  ON DOMAIN dump_test.us_postal_code IS 'not null';\n"
                "COMMENT ON CONSTRAINT us_postal_code_check\n"
                "  ON DOMAIN dump_test.us_postal_code IS 'check it';",
            "regexp": _qr(
                r"""^
                \QCREATE DOMAIN dump_test.us_postal_code AS text COLLATE pg_catalog."C" CONSTRAINT nn NOT NULL DEFAULT '10014'::text\E\n\s+
                \QCONSTRAINT us_postal_code_check CHECK \E
                \Q(((VALUE ~ '^\d{5}\E
                \$\Q'::text) OR (VALUE ~ '^\d{5}-\d{4}\E\$
                \Q'::text)));\E(.|\n)*
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON CONSTRAINT ON DOMAIN (1)": {
            "regexp": _qr(
                r"""^
                \QCOMMENT ON CONSTRAINT nn ON DOMAIN dump_test.us_postal_code IS 'not null';\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "COMMENT ON CONSTRAINT ON DOMAIN (2)": {
            "regexp": _qr(
                r"""^
                \QCOMMENT ON CONSTRAINT us_postal_code_check ON DOMAIN dump_test.us_postal_code IS 'check it';\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE FUNCTION dump_test.pltestlang_call_handler": {
            "create_order": 17,
            "create_sql":
                "CREATE FUNCTION dump_test.pltestlang_call_handler()\n"
                "RETURNS LANGUAGE_HANDLER AS '$libdir/plpgsql',\n"
                "'plpgsql_call_handler' LANGUAGE C;",
            "regexp": _qr(
                r"""^
                \QCREATE FUNCTION dump_test.pltestlang_call_handler() \E
                \QRETURNS language_handler\E
                \n\s+\QLANGUAGE c\E
                \n\s+AS\ \'\$
                \Qlibdir\/plpgsql', 'plpgsql_call_handler';\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE FUNCTION dump_test.trigger_func": {
            "create_order": 30,
            "create_sql":
                "CREATE FUNCTION dump_test.trigger_func()\n"
                "RETURNS trigger LANGUAGE plpgsql\n"
                "AS $$ BEGIN RETURN NULL; END;$$;",
            "regexp": _qr(
                r"""^
                \QCREATE FUNCTION dump_test.trigger_func() RETURNS trigger\E
                \n\s+\QLANGUAGE plpgsql\E
                \n\s+AS\ \$\$
                \Q BEGIN RETURN NULL; END;\E
                \$\$;""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE FUNCTION dump_test.event_trigger_func": {
            "create_order": 32,
            "create_sql":
                "CREATE FUNCTION dump_test.event_trigger_func()\n"
                "RETURNS event_trigger LANGUAGE plpgsql\n"
                "AS $$ BEGIN RETURN; END;$$;",
            "regexp": _qr(
                r"""^
                \QCREATE FUNCTION dump_test.event_trigger_func() RETURNS event_trigger\E
                \n\s+\QLANGUAGE plpgsql\E
                \n\s+AS\ \$\$
                \Q BEGIN RETURN; END;\E
                \$\$;""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE OPERATOR FAMILY dump_test.op_family": {
            "create_order": 73,
            "create_sql":
                "CREATE OPERATOR FAMILY dump_test.op_family USING btree;",
            "regexp": _qr(
                r"""^
                \QCREATE OPERATOR FAMILY dump_test.op_family USING btree;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE OPERATOR CLASS dump_test.op_class": {
            "create_order": 74,
            "create_sql":
                "CREATE OPERATOR CLASS dump_test.op_class\n"
                "                 FOR TYPE bigint USING btree FAMILY dump_test.op_family\n"
                "AS STORAGE bigint,\n"
                "OPERATOR 1 <(bigint,bigint),\n"
                "OPERATOR 2 <=(bigint,bigint),\n"
                "OPERATOR 3 =(bigint,bigint),\n"
                "OPERATOR 4 >=(bigint,bigint),\n"
                "OPERATOR 5 >(bigint,bigint),\n"
                "FUNCTION 1 btint8cmp(bigint,bigint),\n"
                "FUNCTION 2 btint8sortsupport(internal),\n"
                "FUNCTION 4 btequalimage(oid);",
            "regexp": _qr(
                r"""^
                \QCREATE OPERATOR CLASS dump_test.op_class\E\n\s+
                \QFOR TYPE bigint USING btree FAMILY dump_test.op_family AS\E\n\s+
                \QOPERATOR 1 <(bigint,bigint) ,\E\n\s+
                \QOPERATOR 2 <=(bigint,bigint) ,\E\n\s+
                \QOPERATOR 3 =(bigint,bigint) ,\E\n\s+
                \QOPERATOR 4 >=(bigint,bigint) ,\E\n\s+
                \QOPERATOR 5 >(bigint,bigint) ,\E\n\s+
                \QFUNCTION 1 (bigint, bigint) btint8cmp(bigint,bigint);\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE OPERATOR CLASS dump_test.op_class_custom": {
            "create_order": 74,
            "create_sql":
                "CREATE OPERATOR dump_test.~~ (\n"
                "     PROCEDURE = int4eq,\n"
                "     LEFTARG = int,\n"
                "     RIGHTARG = int);\n"
                " CREATE OPERATOR CLASS dump_test.op_class_custom\n"
                "     FOR TYPE int USING btree AS\n"
                "     OPERATOR 3 dump_test.~~;\n"
                " CREATE TYPE dump_test.range_type_custom AS RANGE (\n"
                "     subtype = int,\n"
                "     subtype_opclass = dump_test.op_class_custom);",
            "regexp": _qr(
                r"""^
                \QCREATE OPERATOR dump_test.~~ (\E\n.+
                \QCREATE OPERATOR FAMILY dump_test.op_class_custom USING btree;\E\n.+
                \QCREATE OPERATOR CLASS dump_test.op_class_custom\E\n\s+
                \QFOR TYPE integer USING btree FAMILY dump_test.op_class_custom AS\E\n\s+
                \QOPERATOR 3 dump_test.~~(integer,integer);\E\n.+
                \QCREATE TYPE dump_test.range_type_custom AS RANGE (\E\n\s+
                \Qsubtype = integer,\E\n\s+
                \Qmultirange_type_name = dump_test.multirange_type_custom,\E\n\s+
                \Qsubtype_opclass = dump_test.op_class_custom\E\n
                \Q);\E
                """, _XMS),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE OPERATOR CLASS dump_test.op_class_empty": {
            "create_order": 89,
            "create_sql":
                "CREATE OPERATOR CLASS dump_test.op_class_empty\n"
                "                 FOR TYPE bigint USING btree FAMILY dump_test.op_family\n"
                "AS STORAGE bigint;",
            "regexp": _qr(
                r"""^
                \QCREATE OPERATOR CLASS dump_test.op_class_empty\E\n\s+
                \QFOR TYPE bigint USING btree FAMILY dump_test.op_family AS\E\n\s+
                \QSTORAGE bigint;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE EVENT TRIGGER test_event_trigger": {
            "create_order": 33,
            "create_sql":
                "CREATE EVENT TRIGGER test_event_trigger\n"
                "ON ddl_command_start\n"
                "EXECUTE FUNCTION dump_test.event_trigger_func();",
            "regexp": _qr(
                r"""^
                \QCREATE EVENT TRIGGER test_event_trigger \E
                \QON ddl_command_start\E
                \n\s+\QEXECUTE FUNCTION dump_test.event_trigger_func();\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE TRIGGER test_trigger": {
            "create_order": 31,
            "create_sql":
                "CREATE TRIGGER test_trigger\n"
                "BEFORE INSERT ON dump_test.test_table\n"
                "FOR EACH ROW WHEN (NEW.col1 > 10)\n"
                "EXECUTE FUNCTION dump_test.trigger_func();",
            "regexp": _qr(
                r"""^
                \QCREATE TRIGGER test_trigger BEFORE INSERT ON dump_test.test_table \E
                \QFOR EACH ROW WHEN ((new.col1 > 10)) \E
                \QEXECUTE FUNCTION dump_test.trigger_func();\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_test_table",
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TYPE dump_test.planets AS ENUM": {
            "create_order": 37,
            "create_sql":
                "CREATE TYPE dump_test.planets\n"
                "AS ENUM ( 'venus', 'earth', 'mars' );",
            "regexp": _qr(
                r"""^
                \QCREATE TYPE dump_test.planets AS ENUM (\E
                \n\s+'venus',
                \n\s+'earth',
                \n\s+'mars'
                \n\);""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TYPE dump_test.planets AS ENUM pg_upgrade": {
            "regexp": _qr(
                r"""^
                \QCREATE TYPE dump_test.planets AS ENUM (\E
                \n\);.*^
                \QALTER TYPE dump_test.planets ADD VALUE 'venus';\E
                \n.*^
                \QALTER TYPE dump_test.planets ADD VALUE 'earth';\E
                \n.*^
                \QALTER TYPE dump_test.planets ADD VALUE 'mars';\E
                \n""", _XMS),
            "like": {"binary_upgrade"},
        },
        "CREATE TYPE dump_test.textrange AS RANGE": {
            "create_order": 38,
            "create_sql":
                "CREATE TYPE dump_test.textrange\n"
                "AS RANGE (subtype=text, collation=\"C\");",
            "regexp": _qr(
                r"""^
                \QCREATE TYPE dump_test.textrange AS RANGE (\E
                \n\s+\Qsubtype = text,\E
                \n\s+\Qmultirange_type_name = dump_test.textmultirange,\E
                \n\s+\Qcollation = pg_catalog."C"\E
                \n\);""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TYPE dump_test.int42": {
            "create_order": 39,
            "create_sql": "CREATE TYPE dump_test.int42;",
            "regexp": _qr(r"^\QCREATE TYPE dump_test.int42;\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1": {
            "create_order": 80,
            "create_sql":
                "CREATE TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1 (copy=english);",
            "regexp": _qr(
                r"""^
                \QCREATE TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1 (\E\n
                \s+\QPARSER = pg_catalog."default" );\E""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "ALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1 ...": {
            "regexp": _qr(
                r"""^
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR asciiword WITH english_stem;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR word WITH english_stem;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR numword WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR email WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR url WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR host WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR sfloat WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR version WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR hword_numpart WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR hword_part WITH english_stem;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR hword_asciipart WITH english_stem;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR numhword WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR asciihword WITH english_stem;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR hword WITH english_stem;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR url_path WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR file WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR "float" WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR "int" WITH simple;\E\n
                \n
                \QALTER TEXT SEARCH CONFIGURATION dump_test.alt_ts_conf1\E\n
                \s+\QADD MAPPING FOR uint WITH simple;\E\n
                \n
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TEXT SEARCH TEMPLATE dump_test.alt_ts_temp1": {
            "create_order": 81,
            "create_sql":
                "CREATE TEXT SEARCH TEMPLATE dump_test.alt_ts_temp1 (lexize=dsimple_lexize);",
            "regexp": _qr(
                r"""^
                \QCREATE TEXT SEARCH TEMPLATE dump_test.alt_ts_temp1 (\E\n
                \s+\QLEXIZE = dsimple_lexize );\E""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TEXT SEARCH PARSER dump_test.alt_ts_prs1": {
            "create_order": 82,
            "create_sql":
                "CREATE TEXT SEARCH PARSER dump_test.alt_ts_prs1\n"
                "(start = prsd_start, gettoken = prsd_nexttoken, end = prsd_end, lextypes = prsd_lextype);",
            "regexp": _qr(
                r"""^
                \QCREATE TEXT SEARCH PARSER dump_test.alt_ts_prs1 (\E\n
                \s+\QSTART = prsd_start,\E\n
                \s+\QGETTOKEN = prsd_nexttoken,\E\n
                \s+\QEND = prsd_end,\E\n
                \s+\QLEXTYPES = prsd_lextype );\E\n
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TEXT SEARCH DICTIONARY dump_test.alt_ts_dict1": {
            "create_order": 83,
            "create_sql":
                "CREATE TEXT SEARCH DICTIONARY dump_test.alt_ts_dict1 (template=simple);",
            "regexp": _qr(
                r"""^
                \QCREATE TEXT SEARCH DICTIONARY dump_test.alt_ts_dict1 (\E\n
                \s+\QTEMPLATE = pg_catalog.simple );\E\n
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE FUNCTION dump_test.int42_in": {
            "create_order": 40,
            "create_sql":
                "CREATE FUNCTION dump_test.int42_in(cstring)\n"
                "RETURNS dump_test.int42 AS 'int4in'\n"
                "LANGUAGE internal STRICT IMMUTABLE;",
            "regexp": _qr(
                r"""^
                \QCREATE FUNCTION dump_test.int42_in(cstring) RETURNS dump_test.int42\E
                \n\s+\QLANGUAGE internal IMMUTABLE STRICT\E
                \n\s+AS\ \$\$int4in\$\$;
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE FUNCTION dump_test.int42_out": {
            "create_order": 41,
            "create_sql":
                "CREATE FUNCTION dump_test.int42_out(dump_test.int42)\n"
                "RETURNS cstring AS 'int4out'\n"
                "LANGUAGE internal STRICT IMMUTABLE;",
            "regexp": _qr(
                r"""^
                \QCREATE FUNCTION dump_test.int42_out(dump_test.int42) RETURNS cstring\E
                \n\s+\QLANGUAGE internal IMMUTABLE STRICT\E
                \n\s+AS\ \$\$int4out\$\$;
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE FUNCTION ... SUPPORT": {
            "create_order": 41,
            "create_sql":
                "CREATE FUNCTION dump_test.func_with_support() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$ SUPPORT varchar_support;",
            "regexp": _qr(
                r"""^
                \QCREATE FUNCTION dump_test.func_with_support() RETURNS integer\E
                \n\s+\QLANGUAGE sql SUPPORT varchar_support\E
                \n\s+AS\ \$\$\Q SELECT 1 \E\$\$;
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "Check ordering of a function that depends on a primary key": {
            "create_order": 41,
            "create_sql":
                "\n"
                "CREATE TABLE dump_test.ordering_table (id int primary key, data int);\n"
                "CREATE FUNCTION dump_test.ordering_func ()\n"
                "RETURNS SETOF dump_test.ordering_table\n"
                "LANGUAGE sql BEGIN ATOMIC\n"
                "SELECT * FROM dump_test.ordering_table GROUP BY id; END;",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.ordering_table\E
                \n\s+\QADD CONSTRAINT ordering_table_pkey PRIMARY KEY (id);\E
                .*^
                \QCREATE FUNCTION dump_test.ordering_func\E""", _XMS),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE PROCEDURE dump_test.ptest1": {
            "create_order": 41,
            "create_sql":
                "CREATE PROCEDURE dump_test.ptest1(a int)\n"
                "LANGUAGE SQL AS $$ INSERT INTO dump_test.test_table (col1) VALUES (a) $$;",
            "regexp": _qr(
                r"""^
                \QCREATE PROCEDURE dump_test.ptest1(IN a integer)\E
                \n\s+\QLANGUAGE sql\E
                \n\s+AS\ \$\$\Q INSERT INTO dump_test.test_table (col1) VALUES (a) \E\$\$;
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TYPE dump_test.int42 populated": {
            "create_order": 42,
            "create_sql":
                "CREATE TYPE dump_test.int42 (\n"
                "   internallength = 4,\n"
                "   input = dump_test.int42_in,\n"
                "   output = dump_test.int42_out,\n"
                "   alignment = int4,\n"
                "   default = 42,\n"
                "   passedbyvalue);",
            "regexp": _qr(
                r"""^
                \QCREATE TYPE dump_test.int42 (\E
                \n\s+\QINTERNALLENGTH = 4,\E
                \n\s+\QINPUT = dump_test.int42_in,\E
                \n\s+\QOUTPUT = dump_test.int42_out,\E
                \n\s+\QDEFAULT = '42',\E
                \n\s+\QALIGNMENT = int4,\E
                \n\s+\QSTORAGE = plain,\E
                \n\s+PASSEDBYVALUE\n\);
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TYPE dump_test.composite": {
            "create_order": 43,
            "create_sql":
                "CREATE TYPE dump_test.composite AS (\n"
                "   f1 int,\n"
                "   f2 dump_test.int42\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TYPE dump_test.composite AS (\E
                \n\s+\Qf1 integer,\E
                \n\s+\Qf2 dump_test.int42\E
                \n\);
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TYPE dump_test.undefined": {
            "create_order": 39,
            "create_sql": "CREATE TYPE dump_test.undefined;",
            "regexp": _qr(r"^\QCREATE TYPE dump_test.undefined;\E", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE FOREIGN DATA WRAPPER dummy": {
            "create_order": 35,
            "create_sql": "CREATE FOREIGN DATA WRAPPER dummy;",
            "regexp": _qr(r"CREATE FOREIGN DATA WRAPPER dummy;", _M),
            "like": full | {"section_pre_data"},
        },
        "CREATE SERVER s1 FOREIGN DATA WRAPPER dummy": {
            "create_order": 36,
            "create_sql": "CREATE SERVER s1 FOREIGN DATA WRAPPER dummy;",
            "regexp": _qr(r"CREATE SERVER s1 FOREIGN DATA WRAPPER dummy;", _M),
            "like": full | {"section_pre_data"},
        },
        "CREATE FOREIGN TABLE dump_test.foreign_table SERVER s1": {
            "create_order": 88,
            "create_sql":
                "CREATE FOREIGN TABLE dump_test.foreign_table (c1 int options (column_name 'col1'))\n"
                "               SERVER s1 OPTIONS (schema_name 'x1');",
            "regexp": _qr(
                r"""
                \QCREATE FOREIGN TABLE dump_test.foreign_table (\E\n
                \s+\Qc1 integer\E\n
                \Q)\E\n
                \QSERVER s1\E\n
                \QOPTIONS (\E\n
                \s+\Qschema_name 'x1'\E\n
                \Q);\E\n
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE USER MAPPING FOR regress_dump_test_role SERVER s1": {
            "create_order": 86,
            "create_sql":
                "CREATE USER MAPPING FOR regress_dump_test_role SERVER s1;",
            "regexp": _qr(
                r"CREATE USER MAPPING FOR regress_dump_test_role SERVER s1;", _M),
            "like": full | {"section_pre_data"},
        },
        "CREATE TRANSFORM FOR int": {
            "create_order": 34,
            "create_sql":
                "CREATE TRANSFORM FOR int LANGUAGE SQL (FROM SQL WITH FUNCTION prsd_lextype(internal), TO SQL WITH FUNCTION int4recv(internal));",
            "regexp": _qr(
                r"CREATE TRANSFORM FOR integer LANGUAGE sql \(FROM SQL WITH FUNCTION pg_catalog\.prsd_lextype\(internal\), TO SQL WITH FUNCTION pg_catalog\.int4recv\(internal\)\);", _M),
            "like": full | {"section_pre_data"},
        },
        "CREATE LANGUAGE pltestlang": {
            "create_order": 18,
            "create_sql":
                "CREATE LANGUAGE pltestlang\n"
                "HANDLER dump_test.pltestlang_call_handler;",
            "regexp": _qr(
                r"""^
                \QCREATE PROCEDURAL LANGUAGE pltestlang \E
                \QHANDLER dump_test.pltestlang_call_handler;\E
                """, _XM),
            "like": full | {"section_pre_data"},
            "unlike": {"exclude_dump_test_schema"},
        },
        "CREATE MATERIALIZED VIEW matview": {
            "create_order": 20,
            "create_sql":
                "CREATE MATERIALIZED VIEW dump_test.matview (col1) AS\n"
                "SELECT col1 FROM dump_test.test_table;",
            "regexp": _qr(
                r"""^
                \QCREATE MATERIALIZED VIEW dump_test.matview AS\E
                \n\s+\QSELECT col1\E
                \n\s+\QFROM dump_test.test_table\E
                \n\s+\QWITH NO DATA;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE MATERIALIZED VIEW matview_second": {
            "create_order": 21,
            "create_sql":
                "CREATE MATERIALIZED VIEW\n"
                "   dump_test.matview_second (col1) AS\n"
                "   SELECT * FROM dump_test.matview;",
            "regexp": _qr(
                r"""^
                \QCREATE MATERIALIZED VIEW dump_test.matview_second AS\E
                \n\s+\QSELECT col1\E
                \n\s+\QFROM dump_test.matview\E
                \n\s+\QWITH NO DATA;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE MATERIALIZED VIEW matview_third": {
            "create_order": 58,
            "create_sql":
                "CREATE MATERIALIZED VIEW\n"
                "   dump_test.matview_third (col1) AS\n"
                "   SELECT * FROM dump_test.matview_second WITH NO DATA;",
            "regexp": _qr(
                r"""^
                \QCREATE MATERIALIZED VIEW dump_test.matview_third AS\E
                \n\s+\QSELECT col1\E
                \n\s+\QFROM dump_test.matview_second\E
                \n\s+\QWITH NO DATA;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE MATERIALIZED VIEW matview_fourth": {
            "create_order": 59,
            "create_sql":
                "CREATE MATERIALIZED VIEW\n"
                "   dump_test.matview_fourth (col1) AS\n"
                "   SELECT * FROM dump_test.matview_third WITH NO DATA;",
            "regexp": _qr(
                r"""^
                \QCREATE MATERIALIZED VIEW dump_test.matview_fourth AS\E
                \n\s+\QSELECT col1\E
                \n\s+\QFROM dump_test.matview_third\E
                \n\s+\QWITH NO DATA;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "Check ordering of a matview that depends on a primary key": {
            "create_order": 42,
            "create_sql":
                "\n"
                "CREATE MATERIALIZED VIEW dump_test.ordering_view AS\n"
                "    SELECT * FROM dump_test.ordering_table GROUP BY id;",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.ordering_table\E
                \n\s+\QADD CONSTRAINT ordering_table_pkey PRIMARY KEY (id);\E
                .*^
                \QCREATE MATERIALIZED VIEW dump_test.ordering_view AS\E
                \n\s+\QSELECT id,\E""", _XMS),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE POLICY p1 ON test_table": {
            "create_order": 22,
            "create_sql":
                "CREATE POLICY p1 ON dump_test.test_table\n"
                "   USING (true)\n"
                "   WITH CHECK (true);",
            "regexp": _qr(
                r"""^
                \QCREATE POLICY p1 ON dump_test.test_table \E
                \QUSING (true) WITH CHECK (true);\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_policies",
                "no_policies_restore",
                "only_dump_measurement",
            },
        },
        "CREATE POLICY p2 ON test_table FOR SELECT": {
            "create_order": 24,
            "create_sql":
                "CREATE POLICY p2 ON dump_test.test_table\n"
                "   FOR SELECT TO regress_dump_test_role USING (true);",
            "regexp": _qr(
                r"""^
                \QCREATE POLICY p2 ON dump_test.test_table FOR SELECT TO regress_dump_test_role \E
                \QUSING (true);\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_policies",
                "no_policies_restore",
                "only_dump_measurement",
            },
        },
        "CREATE POLICY p3 ON test_table FOR INSERT": {
            "create_order": 25,
            "create_sql":
                "CREATE POLICY p3 ON dump_test.test_table\n"
                "   FOR INSERT TO regress_dump_test_role WITH CHECK (true);",
            "regexp": _qr(
                r"""^
                \QCREATE POLICY p3 ON dump_test.test_table FOR INSERT \E
                \QTO regress_dump_test_role WITH CHECK (true);\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_policies",
                "no_policies_restore",
                "only_dump_measurement",
            },
        },
        "CREATE POLICY p4 ON test_table FOR UPDATE": {
            "create_order": 26,
            "create_sql":
                "CREATE POLICY p4 ON dump_test.test_table FOR UPDATE\n"
                "   TO regress_dump_test_role USING (true) WITH CHECK (true);",
            "regexp": _qr(
                r"""^
                \QCREATE POLICY p4 ON dump_test.test_table FOR UPDATE TO regress_dump_test_role \E
                \QUSING (true) WITH CHECK (true);\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_policies",
                "no_policies_restore",
                "only_dump_measurement",
            },
        },
        "CREATE POLICY p5 ON test_table FOR DELETE": {
            "create_order": 27,
            "create_sql":
                "CREATE POLICY p5 ON dump_test.test_table\n"
                "   FOR DELETE TO regress_dump_test_role USING (true);",
            "regexp": _qr(
                r"""^
                \QCREATE POLICY p5 ON dump_test.test_table FOR DELETE \E
                \QTO regress_dump_test_role USING (true);\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_policies",
                "no_policies_restore",
                "only_dump_measurement",
            },
        },
        "CREATE POLICY p6 ON test_table AS RESTRICTIVE": {
            "create_order": 27,
            "create_sql":
                "CREATE POLICY p6 ON dump_test.test_table AS RESTRICTIVE\n"
                "   USING (false);",
            "regexp": _qr(
                r"""^
                \QCREATE POLICY p6 ON dump_test.test_table AS RESTRICTIVE \E
                \QUSING (false);\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_post_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_policies",
                "no_policies_restore",
                "only_dump_measurement",
            },
        },
        "CREATE PROPERTY GRAPH propgraph": {
            "create_order": 20,
            "create_sql": "CREATE PROPERTY GRAPH dump_test.propgraph;",
            "regexp": _qr(
                r"""^
                \QCREATE PROPERTY GRAPH dump_test.propgraph\E;
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE PUBLICATION pub1": {
            "create_order": 50,
            "create_sql": "CREATE PUBLICATION pub1;",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub1 WITH (publish = 'insert, update, delete, truncate');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub2": {
            "create_order": 50,
            "create_sql":
                "CREATE PUBLICATION pub2\n"
                " FOR ALL TABLES\n"
                " WITH (publish = '');",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub2 FOR ALL TABLES WITH (publish = '');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub3": {
            "create_order": 50,
            "create_sql": "CREATE PUBLICATION pub3;",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub3 WITH (publish = 'insert, update, delete, truncate');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub4": {
            "create_order": 50,
            "create_sql": "CREATE PUBLICATION pub4;",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub4 WITH (publish = 'insert, update, delete, truncate');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub5": {
            "create_order": 50,
            "create_sql":
                "CREATE PUBLICATION pub5 WITH (publish_generated_columns = stored);",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub5 WITH (publish = 'insert, update, delete, truncate', publish_generated_columns = stored);\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub6": {
            "create_order": 50,
            "create_sql":
                "CREATE PUBLICATION pub6\n"
                " FOR ALL SEQUENCES;",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub6 FOR ALL SEQUENCES WITH (publish = 'insert, update, delete, truncate');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub7": {
            "create_order": 50,
            "create_sql":
                "CREATE PUBLICATION pub7\n"
                " FOR ALL SEQUENCES, ALL TABLES\n"
                " WITH (publish = '');",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub7 FOR ALL TABLES, ALL SEQUENCES WITH (publish = '');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub8": {
            "create_order": 50,
            "create_sql":
                "CREATE PUBLICATION pub8 FOR ALL TABLES EXCEPT (TABLE dump_test.test_table);",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub8 FOR ALL TABLES EXCEPT (TABLE ONLY dump_test.test_table) WITH (publish = 'insert, update, delete, truncate');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub9": {
            "create_order": 50,
            "create_sql":
                "CREATE PUBLICATION pub9 FOR ALL TABLES EXCEPT (TABLE dump_test.test_table, dump_test.test_second_table);",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub9 FOR ALL TABLES EXCEPT (TABLE ONLY dump_test.test_table, TABLE ONLY dump_test.test_second_table) WITH (publish = 'insert, update, delete, truncate');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE PUBLICATION pub10": {
            "create_order": 92,
            "create_sql":
                "CREATE PUBLICATION pub10 FOR ALL TABLES EXCEPT (TABLE dump_test.test_inheritance_parent);",
            "regexp": _qr(
                r"""^
                \QCREATE PUBLICATION pub10 FOR ALL TABLES EXCEPT (TABLE ONLY dump_test.test_inheritance_parent, TABLE ONLY dump_test.test_inheritance_child) WITH (publish = 'insert, update, delete, truncate');\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE SUBSCRIPTION sub1": {
            "create_order": 50,
            "create_sql":
                "CREATE SUBSCRIPTION sub1\n"
                " CONNECTION 'dbname=doesnotexist' PUBLICATION pub1\n"
                " WITH (connect = false);",
            "regexp": _qr(
                r"""^
                \QCREATE SUBSCRIPTION sub1 CONNECTION 'dbname=doesnotexist' PUBLICATION pub1 WITH (connect = false, slot_name = 'sub1', streaming = parallel);\E
                """, _XM),
            "like": full | {"section_post_data"},
            "unlike": {
                "no_subscriptions",
                "no_subscriptions_restore",
            },
        },
        "CREATE SUBSCRIPTION sub2": {
            "create_order": 50,
            "create_sql":
                "CREATE SUBSCRIPTION sub2\n"
                " CONNECTION 'dbname=doesnotexist' PUBLICATION pub1\n"
                " WITH (connect = false, origin = none, streaming = off);",
            "regexp": _qr(
                r"""^
                \QCREATE SUBSCRIPTION sub2 CONNECTION 'dbname=doesnotexist' PUBLICATION pub1 WITH (connect = false, slot_name = 'sub2', streaming = off, origin = none);\E
                """, _XM),
            "like": full | {"section_post_data"},
            "unlike": {
                "no_subscriptions",
                "no_subscriptions_restore",
            },
        },
        "CREATE SUBSCRIPTION sub3": {
            "create_order": 50,
            "create_sql":
                "CREATE SUBSCRIPTION sub3\n"
                " CONNECTION 'dbname=doesnotexist' PUBLICATION pub1\n"
                " WITH (connect = false, origin = any, streaming = on);",
            "regexp": _qr(
                r"""^
                \QCREATE SUBSCRIPTION sub3 CONNECTION 'dbname=doesnotexist' PUBLICATION pub1 WITH (connect = false, slot_name = 'sub3', streaming = on);\E
                """, _XM),
            "like": full | {"section_post_data"},
            "unlike": {
                "no_subscriptions",
                "no_subscriptions_restore",
            },
        },
        "ALTER PUBLICATION pub1 ADD TABLE test_table": {
            "create_order": 51,
            "create_sql":
                "ALTER PUBLICATION pub1 ADD TABLE dump_test.test_table;",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub1 ADD TABLE ONLY dump_test.test_table;\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "ALTER PUBLICATION pub1 ADD TABLE test_second_table": {
            "create_order": 52,
            "create_sql":
                "ALTER PUBLICATION pub1 ADD TABLE dump_test.test_second_table;",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub1 ADD TABLE ONLY dump_test.test_second_table;\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "ALTER PUBLICATION pub1 ADD TABLE test_sixth_table (col3, col2)": {
            "create_order": 52,
            "create_sql":
                "ALTER PUBLICATION pub1 ADD TABLE dump_test.test_sixth_table (col3, col2);",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub1 ADD TABLE ONLY dump_test.test_sixth_table (col2, col3);\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "ALTER PUBLICATION pub1 ADD TABLE test_seventh_table (col3, col2) WHERE (col1 = 1)": {
            "create_order": 52,
            "create_sql":
                "ALTER PUBLICATION pub1 ADD TABLE dump_test.test_seventh_table (col3, col2) WHERE (col1 = 1);",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub1 ADD TABLE ONLY dump_test.test_seventh_table (col2, col3) WHERE ((col1 = 1));\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "ALTER PUBLICATION pub3 ADD TABLES IN SCHEMA dump_test": {
            "create_order": 51,
            "create_sql":
                "ALTER PUBLICATION pub3 ADD TABLES IN SCHEMA dump_test;",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub3 ADD TABLES IN SCHEMA dump_test;\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "ALTER PUBLICATION pub3 ADD TABLES IN SCHEMA public": {
            "create_order": 52,
            "create_sql": "ALTER PUBLICATION pub3 ADD TABLES IN SCHEMA public;",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub3 ADD TABLES IN SCHEMA public;\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "ALTER PUBLICATION pub3 ADD TABLE test_table": {
            "create_order": 51,
            "create_sql":
                "ALTER PUBLICATION pub3 ADD TABLE dump_test.test_table;",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub3 ADD TABLE ONLY dump_test.test_table;\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "ALTER PUBLICATION pub4 ADD TABLE test_table WHERE (col1 > 0);": {
            "create_order": 51,
            "create_sql":
                "ALTER PUBLICATION pub4 ADD TABLE dump_test.test_table WHERE (col1 > 0);",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub4 ADD TABLE ONLY dump_test.test_table WHERE ((col1 > 0));\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "ALTER PUBLICATION pub4 ADD TABLE test_second_table WHERE (col2 = 'test');": {
            "create_order": 52,
            "create_sql":
                "ALTER PUBLICATION pub4 ADD TABLE dump_test.test_second_table WHERE (col2 = 'test');",
            "regexp": _qr(
                r"""^
                \QALTER PUBLICATION pub4 ADD TABLE ONLY dump_test.test_second_table WHERE ((col2 = 'test'::text));\E
                """, _XM),
            "like": full | {"section_post_data"},
        },
        "CREATE SCHEMA public": {
            "regexp": _qr(r"^CREATE SCHEMA public;", _M),
            "like": set(),
        },
        "CREATE SCHEMA dump_test": {
            "create_order": 2,
            "create_sql": "CREATE SCHEMA dump_test;",
            "regexp": _qr(r"^CREATE SCHEMA dump_test;", _M),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE SCHEMA dump_test_second_schema": {
            "create_order": 9,
            "create_sql": "CREATE SCHEMA dump_test_second_schema;",
            "regexp": _qr(r"^CREATE SCHEMA dump_test_second_schema;", _M),
            "like": full | {
                "role",
                "section_pre_data",
            },
        },
    }


def _tests_part3(full, dts):
    return {
        "CREATE TABLE test_table": {
            "create_order": 3,
            "create_sql":
                "CREATE TABLE dump_test.test_table (\n"
                "   col1 serial primary key,\n"
                "   col2 text COMPRESSION pglz,\n"
                "   col3 text,\n"
                "   col4 text,\n"
                "   CHECK (col1 <= 1000)\n"
                ") WITH (autovacuum_enabled = false, fillfactor=80);\n"
                "COMMENT ON CONSTRAINT test_table_col1_check\n"
                "  ON dump_test.test_table IS 'bounds check';",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table (\E\n
                \s+\Qcol1 integer NOT NULL,\E\n
                \s+\Qcol2 text,\E\n
                \s+\Qcol3 text,\E\n
                \s+\Qcol4 text,\E\n
                \s+\QCONSTRAINT test_table_col1_check CHECK ((col1 <= 1000))\E\n
                \Q)\E\n
                \QWITH (autovacuum_enabled='false', fillfactor='80');\E\n(.|\n)*
                \QCOMMENT ON CONSTRAINT test_table_col1_check ON dump_test.test_table IS 'bounds check';\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE fk_reference_test_table": {
            "create_order": 21,
            "create_sql":
                "CREATE TABLE dump_test.fk_reference_test_table (\n"
                "   col1 int primary key references dump_test.test_table\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.fk_reference_test_table (\E
                \n\s+\Qcol1 integer NOT NULL\E
                \n\);
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_second_table": {
            "create_order": 6,
            "create_sql":
                "CREATE TABLE dump_test.test_second_table (\n"
                "   col1 int,\n"
                "   col2 text\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_second_table (\E
                \n\s+\Qcol1 integer,\E
                \n\s+\Qcol2 text\E
                \n\);
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE measurement PARTITIONED BY": {
            "create_order": 90,
            "create_sql":
                "CREATE TABLE dump_test.measurement (\n"
                "city_id serial not null,\n"
                "logdate date not null,\n"
                "peaktemp int CHECK (peaktemp >= -460),\n"
                "unitsales int\n"
                ") PARTITION BY RANGE (logdate);",
            "regexp": _qr(
                r"""^
                \Q-- Name: measurement;\E.*\n
                \Q--\E\n\n
                \QCREATE TABLE dump_test.measurement (\E\n
                \s+\Qcity_id integer NOT NULL,\E\n
                \s+\Qlogdate date NOT NULL,\E\n
                \s+\Qpeaktemp integer,\E\n
                \s+\Qunitsales integer,\E\n
                \s+\QCONSTRAINT measurement_peaktemp_check CHECK ((peaktemp >= '-460'::integer))\E\n
                \)\n
                \QPARTITION BY RANGE (logdate);\E\n
                """, _XM),
            "like": full | dts | {
                "section_pre_data",
                "only_dump_measurement",
            },
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "exclude_measurement",
            },
        },
        "Partition measurement_y2006m2 creation": {
            "create_order": 91,
            "create_sql":
                "CREATE TABLE dump_test_second_schema.measurement_y2006m2\n"
                "PARTITION OF dump_test.measurement (\n"
                "    unitsales DEFAULT 0 CHECK (unitsales >= 0)\n"
                ")\n"
                "FOR VALUES FROM ('2006-02-01') TO ('2006-03-01');",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test_second_schema.measurement_y2006m2 (\E\n
                \s+\Qcity_id integer DEFAULT nextval('dump_test.measurement_city_id_seq'::regclass) CONSTRAINT measurement_city_id_not_null NOT NULL,\E\n
                \s+\Qlogdate date CONSTRAINT measurement_logdate_not_null NOT NULL,\E\n
                \s+\Qpeaktemp integer,\E\n
                \s+\Qunitsales integer DEFAULT 0,\E\n
                \s+\QCONSTRAINT measurement_peaktemp_check CHECK ((peaktemp >= '-460'::integer)),\E\n
                \s+\QCONSTRAINT measurement_y2006m2_unitsales_check CHECK ((unitsales >= 0))\E\n
                \);\n
                """, _XM),
            "like": full | {
                "section_pre_data",
                "role",
                "binary_upgrade",
                "only_dump_measurement",
            },
            "unlike": {"exclude_measurement"},
        },
        "Creation of row-level trigger in partitioned table": {
            "create_order": 92,
            "create_sql":
                "CREATE TRIGGER test_trigger\n"
                "   AFTER INSERT ON dump_test.measurement\n"
                "   FOR EACH ROW EXECUTE PROCEDURE dump_test.trigger_func()",
            "regexp": _qr(
                r"""^
                \QCREATE TRIGGER test_trigger AFTER INSERT ON dump_test.measurement \E
                \QFOR EACH ROW \E
                \QEXECUTE FUNCTION dump_test.trigger_func();\E
                """, _XM),
            "like": full | dts | {
                "section_post_data",
                "only_dump_measurement",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_measurement",
            },
        },
        "COPY measurement": {
            "create_order": 93,
            "create_sql":
                "INSERT INTO dump_test.measurement (city_id, logdate, peaktemp, unitsales) "
                "VALUES (1, '2006-02-12', 35, 1);",
            "regexp": _qr(
                r"""^
                \QCOPY dump_test_second_schema.measurement_y2006m2 (city_id, logdate, peaktemp, unitsales) FROM stdin;\E
                \n(?:1\t2006-02-12\t35\t1\n)\\\.\n
                """, _XM),
            "like": full | dts | {
                "data_only",
                "no_schema",
                "only_dump_measurement",
                "section_data",
                "only_dump_test_schema",
                "role_parallel",
                "role",
            },
            "unlike": {
                "binary_upgrade",
                "schema_only",
                "schema_only_with_statistics",
                "exclude_measurement",
                "only_dump_test_schema",
                "test_schema_plus_large_objects",
                "exclude_measurement_data",
            },
        },
        "Disabled trigger on partition is altered": {
            "create_order": 93,
            "create_sql":
                "CREATE TABLE dump_test_second_schema.measurement_y2006m3\n"
                "PARTITION OF dump_test.measurement\n"
                "FOR VALUES FROM ('2006-03-01') TO ('2006-04-01');\n"
                "ALTER TABLE dump_test_second_schema.measurement_y2006m3 DISABLE TRIGGER test_trigger;\n"
                "CREATE TABLE dump_test_second_schema.measurement_y2006m4\n"
                "PARTITION OF dump_test.measurement\n"
                "FOR VALUES FROM ('2006-04-01') TO ('2006-05-01');\n"
                "ALTER TABLE dump_test_second_schema.measurement_y2006m4 ENABLE REPLICA TRIGGER test_trigger;\n"
                "CREATE TABLE dump_test_second_schema.measurement_y2006m5\n"
                "PARTITION OF dump_test.measurement\n"
                "FOR VALUES FROM ('2006-05-01') TO ('2006-06-01');\n"
                "ALTER TABLE dump_test_second_schema.measurement_y2006m5 ENABLE ALWAYS TRIGGER test_trigger;\n",
            "regexp": _qr(
                r"""^
                \QALTER TABLE dump_test_second_schema.measurement_y2006m3 DISABLE TRIGGER test_trigger;\E
                """, _XM),
            "like": full | {
                "section_post_data",
                "role",
                "binary_upgrade",
                "only_dump_measurement",
            },
            "unlike": {"exclude_measurement"},
        },
        "Replica trigger on partition is altered": {
            "regexp": _qr(
                r"""^
                \QALTER TABLE dump_test_second_schema.measurement_y2006m4 ENABLE REPLICA TRIGGER test_trigger;\E
                """, _XM),
            "like": full | {
                "section_post_data",
                "role",
                "binary_upgrade",
                "only_dump_measurement",
            },
            "unlike": {"exclude_measurement"},
        },
        "Always trigger on partition is altered": {
            "regexp": _qr(
                r"""^
                \QALTER TABLE dump_test_second_schema.measurement_y2006m5 ENABLE ALWAYS TRIGGER test_trigger;\E
                """, _XM),
            "like": full | {
                "section_post_data",
                "role",
                "binary_upgrade",
                "only_dump_measurement",
            },
            "unlike": {"exclude_measurement"},
        },
        "Disabled trigger on partition is not created": {
            "regexp": _qr(r"CREATE TRIGGER test_trigger.*ON dump_test_second_schema"),
            "like": set(),
        },
        "Triggers on partitions are not dropped": {
            "regexp": _qr(r"DROP TRIGGER test_trigger.*ON dump_test_second_schema"),
            "like": set(),
        },
        "CREATE TABLE test_third_table_generated_cols": {
            "create_order": 6,
            "create_sql":
                "CREATE TABLE dump_test.test_third_table (\n"
                "f1 int, junk int,\n"
                "g1 int generated always as (f1 * 2) stored,\n"
                "\"F3\" int,\n"
                "g2 int generated always as (\"F3\" * 3) stored\n"
                ");\n"
                "ALTER TABLE dump_test.test_third_table DROP COLUMN junk;",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_third_table (\E\n
                \s+\Qf1 integer,\E\n
                \s+\Qg1 integer GENERATED ALWAYS AS ((f1 * 2)) STORED,\E\n
                \s+\Q"F3" integer,\E\n
                \s+\Qg2 integer GENERATED ALWAYS AS (("F3" * 3)) STORED\E\n
                \);\n
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_fourth_table_zero_col": {
            "create_order": 6,
            "create_sql":
                "CREATE TABLE dump_test.test_fourth_table (\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_fourth_table (\E
                \n\);
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_fifth_table": {
            "create_order": 53,
            "create_sql":
                "CREATE TABLE dump_test.test_fifth_table (\n"
                "    col1 integer,\n"
                "    col2 boolean,\n"
                "    col3 boolean,\n"
                "    col4 bit(5),\n"
                "    col5 float8\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_fifth_table (\E
                \n\s+\Qcol1 integer,\E
                \n\s+\Qcol2 boolean,\E
                \n\s+\Qcol3 boolean,\E
                \n\s+\Qcol4 bit(5),\E
                \n\s+\Qcol5 double precision\E
                \n\);
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_sixth_table": {
            "create_order": 6,
            "create_sql":
                "CREATE TABLE dump_test.test_sixth_table (\n"
                "   col1 int,\n"
                "   col2 text,\n"
                "   col3 bytea\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_sixth_table (\E
                \n\s+\Qcol1 integer,\E
                \n\s+\Qcol2 text,\E
                \n\s+\Qcol3 bytea\E
                \n\);
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_seventh_table": {
            "create_order": 6,
            "create_sql":
                "CREATE TABLE dump_test.test_seventh_table (\n"
                "   col1 int,\n"
                "   col2 text,\n"
                "   col3 bytea\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_seventh_table (\E
                \n\s+\Qcol1 integer,\E
                \n\s+\Qcol2 text,\E
                \n\s+\Qcol3 bytea\E
                \n\);
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_table_identity": {
            "create_order": 3,
            "create_sql":
                "CREATE TABLE dump_test.test_table_identity (\n"
                "   col1 int generated always as identity primary key,\n"
                "   col2 text\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_identity (\E\n
                \s+\Qcol1 integer NOT NULL,\E\n
                \s+\Qcol2 text\E\n
                \);
                .*
                \QALTER TABLE dump_test.test_table_identity ALTER COLUMN col1 ADD GENERATED ALWAYS AS IDENTITY (\E\n
                \s+\QSEQUENCE NAME dump_test.test_table_identity_col1_seq\E\n
                \s+\QSTART WITH 1\E\n
                \s+\QINCREMENT BY 1\E\n
                \s+\QNO MINVALUE\E\n
                \s+\QNO MAXVALUE\E\n
                \s+\QCACHE 1\E\n
                \);
                """, _XMS),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_table_generated": {
            "create_order": 3,
            "create_sql":
                "CREATE TABLE dump_test.test_table_generated (\n"
                "   col1 int primary key,\n"
                "   col2 int generated always as (col1 * 2) stored,\n"
                "   col3 int generated always as (col1 * 3) virtual\n"
                ");",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_generated (\E\n
                \s+\Qcol1 integer NOT NULL,\E\n
                \s+\Qcol2 integer GENERATED ALWAYS AS ((col1 * 2)) STORED,\E\n
                \s+\Qcol3 integer GENERATED ALWAYS AS ((col1 * 3))\E\n
                \);
                """, _XMS),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_table_generated_child1 (without local columns)": {
            "create_order": 4,
            "create_sql":
                "CREATE TABLE dump_test.test_table_generated_child1 ()\n"
                " INHERITS (dump_test.test_table_generated);",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_generated_child1 (\E\n
                \)\n
                \QINHERITS (dump_test.test_table_generated);\E\n
                """, _XMS),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "ALTER TABLE test_table_generated_child1": {
            "regexp": _qr(
                r"^\QALTER TABLE ONLY dump_test.test_table_generated_child1 ALTER COLUMN col2 \E", _M),
            "like": set(),
        },
        "CREATE TABLE test_table_generated_child2 (with local columns)": {
            "create_order": 4,
            "create_sql":
                "CREATE TABLE dump_test.test_table_generated_child2 (\n"
                "   col1 int,\n"
                "   col2 int\n"
                " ) INHERITS (dump_test.test_table_generated);",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_table_generated_child2 (\E\n
                \s+\Qcol1 integer,\E\n
                \s+\Qcol2 integer\E\n
                \)\n
                \QINHERITS (dump_test.test_table_generated);\E\n
                """, _XMS),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE table_with_stats": {
            "create_order": 98,
            "create_sql":
                "CREATE TABLE dump_test.table_index_stats (\n"
                "   col1 int,\n"
                "   col2 int,\n"
                "   col3 int);\n"
                " CREATE INDEX index_with_stats\n"
                "  ON dump_test.table_index_stats\n"
                "  ((col1 + 1), col1, (col2 + 1), (col3 + 1));\n"
                " ALTER INDEX dump_test.index_with_stats\n"
                "   ALTER COLUMN 1 SET STATISTICS 400;\n"
                " ALTER INDEX dump_test.index_with_stats\n"
                "   ALTER COLUMN 3 SET STATISTICS 500;",
            "regexp": _qr(
                r"""^
                \QALTER INDEX dump_test.index_with_stats ALTER COLUMN 1 SET STATISTICS 400;\E\n
                \QALTER INDEX dump_test.index_with_stats ALTER COLUMN 3 SET STATISTICS 500;\E\n
                """, _XMS),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_inheritance_parent": {
            "create_order": 90,
            "create_sql":
                "CREATE TABLE dump_test.test_inheritance_parent (\n"
                "   col1 int NOT NULL,\n"
                "   col2 int CHECK (col2 >= 42)\n"
                " );",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_inheritance_parent (\E\n
                \s+\Qcol1 integer NOT NULL,\E\n
                \s+\Qcol2 integer,\E\n
                \s+\QCONSTRAINT test_inheritance_parent_col2_check CHECK ((col2 >= 42))\E\n
                \Q);\E\n
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE TABLE test_inheritance_child": {
            "create_order": 91,
            "create_sql":
                "CREATE TABLE dump_test.test_inheritance_child (\n"
                "    col1 int NOT NULL,\n"
                "    CONSTRAINT test_inheritance_child CHECK (col2 >= 142857)\n"
                ") INHERITS (dump_test.test_inheritance_parent);",
            "regexp": _qr(
                r"""^
                \QCREATE TABLE dump_test.test_inheritance_child (\E\n
                \s+\Qcol1 integer NOT NULL,\E\n
                \s+\QCONSTRAINT test_inheritance_child CHECK ((col2 >= 142857))\E\n
                \)\n
                \QINHERITS (dump_test.test_inheritance_parent);\E\n
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE STATISTICS extended_stats_no_options": {
            "create_order": 97,
            "create_sql":
                "CREATE STATISTICS dump_test.test_ext_stats_no_options\n"
                "    ON col1, col2 FROM dump_test.test_table",
            "regexp": _qr(
                r"""^
                \QCREATE STATISTICS dump_test.test_ext_stats_no_options ON col1, col2 FROM dump_test.test_table;\E
                """, _XMS),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "only_dump_measurement",
            },
        },
        "CREATE STATISTICS extended_stats_options": {
            "create_order": 97,
            "create_sql":
                "CREATE STATISTICS dump_test.test_ext_stats_opts\n"
                "    (ndistinct) ON col1, col2 FROM dump_test.test_fifth_table",
            "regexp": _qr(
                r"""^
                \QCREATE STATISTICS dump_test.test_ext_stats_opts (ndistinct) ON col1, col2 FROM dump_test.test_fifth_table;\E
                """, _XMS),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "ALTER STATISTICS extended_stats_options": {
            "create_order": 98,
            "create_sql":
                "ALTER STATISTICS dump_test.test_ext_stats_opts SET STATISTICS 1000",
            "regexp": _qr(
                r"""^
                \QALTER STATISTICS dump_test.test_ext_stats_opts SET STATISTICS 1000;\E
                """, _XMS),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE STATISTICS extended_stats_expression": {
            "create_order": 99,
            "create_sql":
                "CREATE STATISTICS dump_test.test_ext_stats_expr\n"
                "    ON (2 * col1) FROM dump_test.test_fifth_table",
            "regexp": _qr(
                r"""^
                \QCREATE STATISTICS dump_test.test_ext_stats_expr ON (2 * col1) FROM dump_test.test_fifth_table;\E
                """, _XMS),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE SEQUENCE test_table_col1_seq": {
            "regexp": _qr(
                r"""^
                \QCREATE SEQUENCE dump_test.test_table_col1_seq\E
                \n\s+\QAS integer\E
                \n\s+\QSTART WITH 1\E
                \n\s+\QINCREMENT BY 1\E
                \n\s+\QNO MINVALUE\E
                \n\s+\QNO MAXVALUE\E
                \n\s+\QCACHE 1;\E
                """, _XM),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "CREATE INDEX ON ONLY measurement": {
            "create_order": 92,
            "create_sql":
                "CREATE INDEX ON dump_test.measurement (city_id, logdate);",
            "regexp": _qr(
                r"""^
                \QCREATE INDEX measurement_city_id_logdate_idx ON ONLY dump_test.measurement USING\E
                """, _XM),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_measurement",
            },
        },
        "ALTER TABLE measurement PRIMARY KEY": {
            "catch_all": "CREATE ... commands",
            "create_order": 93,
            "create_sql":
                "ALTER TABLE dump_test.measurement ADD PRIMARY KEY (city_id, logdate);",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.measurement\E \n^\s+
                \QADD CONSTRAINT measurement_pkey PRIMARY KEY (city_id, logdate);\E
                """, _XM),
            "like": full | dts | {
                "section_post_data",
                "only_dump_measurement",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_measurement",
            },
        },
        "CREATE INDEX ... ON measurement_y2006_m2": {
            "regexp": _qr(
                r"""^
                \QCREATE INDEX measurement_y2006m2_city_id_logdate_idx ON dump_test_second_schema.measurement_y2006m2 \E
                """, _XM),
            "like": full | {
                "role",
                "section_post_data",
                "only_dump_measurement",
            },
            "unlike": {"exclude_measurement"},
        },
        "ALTER INDEX ... ATTACH PARTITION": {
            "regexp": _qr(
                r"""^
                \QALTER INDEX dump_test.measurement_city_id_logdate_idx ATTACH PARTITION dump_test_second_schema.measurement_y2006m2_city_id_logdate_idx\E
                """, _XM),
            "like": full | {
                "role",
                "section_post_data",
                "only_dump_measurement",
            },
            "unlike": {"exclude_measurement"},
        },
        "ALTER INDEX ... ATTACH PARTITION (primary key)": {
            "catch_all": "CREATE ... commands",
            "regexp": _qr(
                r"""^
                \QALTER INDEX dump_test.measurement_pkey ATTACH PARTITION dump_test_second_schema.measurement_y2006m2_pkey\E
                """, _XM),
            "like": full | {
                "role",
                "section_post_data",
                "only_dump_measurement",
            },
            "unlike": {"exclude_measurement"},
        },
        "CREATE VIEW test_view": {
            "create_order": 61,
            "create_sql":
                "CREATE VIEW dump_test.test_view\n"
                "                   WITH (check_option = 'local', security_barrier = true) AS\n"
                "                   SELECT col1 FROM dump_test.test_table;",
            "regexp": _qr(
                r"""^
                \QCREATE VIEW dump_test.test_view WITH (security_barrier='true') AS\E
                \n\s+\QSELECT col1\E
                \n\s+\QFROM dump_test.test_table\E
                \n\s+\QWITH LOCAL CHECK OPTION;\E""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "ALTER VIEW test_view SET DEFAULT": {
            "create_order": 62,
            "create_sql":
                "ALTER VIEW dump_test.test_view ALTER COLUMN col1 SET DEFAULT 1;",
            "regexp": _qr(
                r"""^
                \QALTER TABLE ONLY dump_test.test_view ALTER COLUMN col1 SET DEFAULT 1;\E""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "only_dump_measurement",
            },
        },
        "DROP SCHEMA public (for testing without public schema)": {
            "database": "regress_pg_dump_test",
            "create_order": 100,
            "create_sql": "DROP SCHEMA public;",
            "regexp": _qr(r"^DROP SCHEMA public;", _M),
            "like": set(),
        },
        "DROP SCHEMA public": {
            "regexp": _qr(r"^DROP SCHEMA public;", _M),
            "like": set(),
        },
        "DROP SCHEMA IF EXISTS public": {
            "regexp": _qr(r"^DROP SCHEMA IF EXISTS public;", _M),
            "like": set(),
        },
        "DROP EXTENSION plpgsql": {
            "regexp": _qr(r"^DROP EXTENSION plpgsql;", _M),
            "like": set(),
        },
        "DROP FUNCTION dump_test.pltestlang_call_handler()": {
            "regexp": _qr(r"^DROP FUNCTION dump_test\.pltestlang_call_handler\(\);", _M),
            "like": {"clean"},
        },
        "DROP LANGUAGE pltestlang": {
            "regexp": _qr(r"^DROP PROCEDURAL LANGUAGE pltestlang;", _M),
            "like": {"clean"},
        },
        "DROP SCHEMA dump_test": {
            "regexp": _qr(r"^DROP SCHEMA dump_test;", _M),
            "like": {"clean"},
        },
        "DROP SCHEMA dump_test_second_schema": {
            "regexp": _qr(r"^DROP SCHEMA dump_test_second_schema;", _M),
            "like": {"clean"},
        },
        "DROP TABLE test_table": {
            "regexp": _qr(r"^DROP TABLE dump_test\.test_table;", _M),
            "like": {"clean"},
        },
        "DROP TABLE fk_reference_test_table": {
            "regexp": _qr(r"^DROP TABLE dump_test\.fk_reference_test_table;", _M),
            "like": {"clean"},
        },
        "DROP TABLE test_second_table": {
            "regexp": _qr(r"^DROP TABLE dump_test\.test_second_table;", _M),
            "like": {"clean"},
        },
        "DROP EXTENSION IF EXISTS plpgsql": {
            "regexp": _qr(r"^DROP EXTENSION IF EXISTS plpgsql;", _M),
            "like": set(),
        },
        "DROP FUNCTION IF EXISTS dump_test.pltestlang_call_handler()": {
            "regexp": _qr(
                r"""^
                \QDROP FUNCTION IF EXISTS dump_test.pltestlang_call_handler();\E
                """, _XM),
            "like": {"clean_if_exists"},
        },
        "DROP LANGUAGE IF EXISTS pltestlang": {
            "regexp": _qr(r"^DROP PROCEDURAL LANGUAGE IF EXISTS pltestlang;", _M),
            "like": {"clean_if_exists"},
        },
        "DROP SCHEMA IF EXISTS dump_test": {
            "regexp": _qr(r"^DROP SCHEMA IF EXISTS dump_test;", _M),
            "like": {"clean_if_exists"},
        },
        "DROP SCHEMA IF EXISTS dump_test_second_schema": {
            "regexp": _qr(r"^DROP SCHEMA IF EXISTS dump_test_second_schema;", _M),
            "like": {"clean_if_exists"},
        },
        "DROP TABLE IF EXISTS test_table": {
            "regexp": _qr(r"^DROP TABLE IF EXISTS dump_test\.test_table;", _M),
            "like": {"clean_if_exists"},
        },
        "DROP TABLE IF EXISTS test_second_table": {
            "regexp": _qr(r"^DROP TABLE IF EXISTS dump_test\.test_second_table;", _M),
            "like": {"clean_if_exists"},
        },
        "DROP ROLE regress_dump_test_role": {
            "regexp": _qr(
                r"""^
                \QDROP ROLE regress_dump_test_role;\E
                """, _XM),
            "like": {"pg_dumpall_globals_clean"},
        },
        "DROP ROLE pg_": {
            "regexp": _qr(
                r"""^
                \QDROP ROLE pg_\E.+;
                """, _XM),
            "like": set(),
        },
        "GRANT USAGE ON SCHEMA dump_test_second_schema": {
            "create_order": 10,
            "create_sql":
                "GRANT USAGE ON SCHEMA dump_test_second_schema\n"
                "   TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT USAGE ON SCHEMA dump_test_second_schema TO regress_dump_test_role;\E
                """, _XM),
            "like": full | {
                "role",
                "section_pre_data",
            },
            "unlike": {"no_privs"},
        },
        "GRANT USAGE ON FOREIGN DATA WRAPPER dummy": {
            "create_order": 85,
            "create_sql":
                "GRANT USAGE ON FOREIGN DATA WRAPPER dummy\n"
                "   TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON FOREIGN DATA WRAPPER dummy TO regress_dump_test_role;\E
                """, _XM),
            "like": full | {"section_pre_data"},
            "unlike": {"no_privs"},
        },
        "GRANT USAGE ON FOREIGN SERVER s1": {
            "create_order": 85,
            "create_sql":
                "GRANT USAGE ON FOREIGN SERVER s1\n"
                "   TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON FOREIGN SERVER s1 TO regress_dump_test_role;\E
                """, _XM),
            "like": full | {"section_pre_data"},
            "unlike": {"no_privs"},
        },
        "GRANT USAGE ON DOMAIN dump_test.us_postal_code": {
            "create_order": 72,
            "create_sql":
                "GRANT USAGE ON DOMAIN dump_test.us_postal_code TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON TYPE dump_test.us_postal_code TO regress_dump_test_role;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "GRANT USAGE ON TYPE dump_test.int42": {
            "create_order": 87,
            "create_sql":
                "GRANT USAGE ON TYPE dump_test.int42 TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON TYPE dump_test.int42 TO regress_dump_test_role;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "GRANT USAGE ON TYPE dump_test.planets - ENUM": {
            "create_order": 66,
            "create_sql":
                "GRANT USAGE ON TYPE dump_test.planets TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON TYPE dump_test.planets TO regress_dump_test_role;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "GRANT USAGE ON TYPE dump_test.textrange - RANGE": {
            "create_order": 67,
            "create_sql":
                "GRANT USAGE ON TYPE dump_test.textrange TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON TYPE dump_test.textrange TO regress_dump_test_role;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "GRANT CREATE ON DATABASE dump_test": {
            "create_order": 48,
            "create_sql":
                "GRANT CREATE ON DATABASE dump_test TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT CREATE ON DATABASE dump_test TO regress_dump_test_role;\E
                """, _XM),
            "like": {"pg_dumpall_dbprivs"},
        },
        "GRANT SELECT ON TABLE test_table": {
            "create_order": 5,
            "create_sql":
                "GRANT SELECT ON TABLE dump_test.test_table\n"
                "   TO regress_dump_test_role;",
            "regexp": _qr(
                r"^\QGRANT SELECT ON TABLE dump_test.test_table TO regress_dump_test_role;\E", _M),
            "like": full | dts | {
                "only_dump_test_table",
                "section_pre_data",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "exclude_test_table",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "GRANT SELECT ON TABLE measurement": {
            "create_order": 91,
            "create_sql":
                "GRANT SELECT ON TABLE dump_test.measurement\n"
                "   TO regress_dump_test_role;\n"
                "GRANT SELECT(city_id) ON TABLE dump_test.measurement\n"
                '   TO "regress_quoted  \\"" role";',
            "regexp": _qr(
                r"""^\QGRANT SELECT ON TABLE dump_test.measurement TO regress_dump_test_role;\E\n.*
                 ^\QGRANT SELECT(city_id) ON TABLE dump_test.measurement TO "regress_quoted  \"" role";\E""", _XMS),
            "like": full | dts | {
                "section_pre_data",
                "only_dump_measurement",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "exclude_measurement",
            },
        },
        "GRANT SELECT ON TABLE measurement_y2006m2": {
            "create_order": 94,
            "create_sql":
                "GRANT SELECT ON TABLE\n"
                "   dump_test_second_schema.measurement_y2006m2,\n"
                "   dump_test_second_schema.measurement_y2006m3,\n"
                "   dump_test_second_schema.measurement_y2006m4,\n"
                "   dump_test_second_schema.measurement_y2006m5\n"
                "   TO regress_dump_test_role;",
            "regexp": _qr(
                r"^\QGRANT SELECT ON TABLE dump_test_second_schema.measurement_y2006m2 TO regress_dump_test_role;\E", _M),
            "like": full | {
                "role",
                "section_pre_data",
                "only_dump_measurement",
            },
            "unlike": {
                "no_privs",
                "exclude_measurement",
            },
        },
        "GRANT ALL ON LARGE OBJECT ...": {
            "create_order": 60,
            "create_sql":
                "DO $$\n"
                " DECLARE myoid oid;\n"
                " BEGIN\n"
                "    SELECT loid FROM pg_largeobject INTO myoid;\n"
                "    EXECUTE 'GRANT ALL ON LARGE OBJECT ' || myoid || ' TO regress_dump_test_role;';\n"
                " END;\n"
                " $$;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON LARGE OBJECT \E[0-9]+\Q TO regress_dump_test_role;\E
                """, _XM),
            "like": full | {
                "column_inserts",
                "data_only",
                "inserts",
                "no_schema",
                "section_data",
                "test_schema_plus_large_objects",
            },
            "unlike": {
                "binary_upgrade",
                "no_large_objects",
                "no_privs",
                "schema_only",
                "schema_only_with_statistics",
            },
        },
        "GRANT INSERT(col1) ON TABLE test_second_table": {
            "create_order": 8,
            "create_sql":
                "GRANT INSERT (col1) ON TABLE dump_test.test_second_table\n"
                "   TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT INSERT(col1) ON TABLE dump_test.test_second_table TO regress_dump_test_role;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "GRANT SELECT ON PROPERTY GRAPH propgraph": {
            "create_order": 21,
            "create_sql":
                "GRANT SELECT ON PROPERTY GRAPH dump_test.propgraph TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON PROPERTY GRAPH dump_test.propgraph TO regress_dump_test_role;\E
                """, _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_privs",
                "only_dump_measurement",
            },
        },
        "GRANT EXECUTE ON FUNCTION pg_sleep() TO regress_dump_test_role": {
            "create_order": 16,
            "create_sql":
                "GRANT EXECUTE ON FUNCTION pg_sleep(float8)\n"
                "   TO regress_dump_test_role;",
            "regexp": _qr(
                r"""^
                \QGRANT ALL ON FUNCTION pg_catalog.pg_sleep(double precision) TO regress_dump_test_role;\E
                """, _XM),
            "like": full | {"section_pre_data"},
            "unlike": {"no_privs"},
        },
        "GRANT SELECT (proname ...) ON TABLE pg_proc TO public": {
            "create_order": 46,
            "create_sql":
                "GRANT SELECT (\n"
                "   tableoid,\n"
                "   oid,\n"
                "   proname,\n"
                "   pronamespace,\n"
                "   proowner,\n"
                "   prolang,\n"
                "   procost,\n"
                "   prorows,\n"
                "   provariadic,\n"
                "   prosupport,\n"
                "   prokind,\n"
                "   prosecdef,\n"
                "   proleakproof,\n"
                "   proisstrict,\n"
                "   proretset,\n"
                "   provolatile,\n"
                "   proparallel,\n"
                "   pronargs,\n"
                "   pronargdefaults,\n"
                "   prorettype,\n"
                "   proargtypes,\n"
                "   proallargtypes,\n"
                "   proargmodes,\n"
                "   proargnames,\n"
                "   proargdefaults,\n"
                "   protrftypes,\n"
                "   prosrc,\n"
                "   probin,\n"
                "   proconfig,\n"
                "   proacl\n"
                ") ON TABLE pg_proc TO public;",
            "regexp": _qr(
                r"""
                \QGRANT SELECT(tableoid) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(oid) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proname) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(pronamespace) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proowner) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(prolang) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(procost) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(prorows) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(provariadic) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(prosupport) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(prokind) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(prosecdef) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proleakproof) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proisstrict) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proretset) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(provolatile) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proparallel) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(pronargs) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(pronargdefaults) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(prorettype) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proargtypes) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proallargtypes) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proargmodes) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proargnames) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proargdefaults) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(protrftypes) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(prosrc) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(probin) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proconfig) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E\n.*
                \QGRANT SELECT(proacl) ON TABLE pg_catalog.pg_proc TO PUBLIC;\E""", _XMS),
            "like": full | {"section_pre_data"},
            "unlike": {"no_privs"},
        },
        "GRANT USAGE ON SCHEMA public TO public": {
            "regexp": _qr(
                r"""^
                \Q--\E\n\n
                \QGRANT USAGE ON SCHEMA public TO PUBLIC;\E
                """, _XM),
            "like": set(),
        },
        "REFRESH MATERIALIZED VIEW matview": {
            "regexp": _qr(r"^\QREFRESH MATERIALIZED VIEW dump_test.matview;\E", _M),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "REFRESH MATERIALIZED VIEW matview_second": {
            "regexp": _qr(
                r"""^
                \QREFRESH MATERIALIZED VIEW dump_test.matview;\E
                \n.*
                \QREFRESH MATERIALIZED VIEW dump_test.matview_second;\E
                """, _XMS),
            "like": full | dts | {"section_post_data"},
            "unlike": {
                "binary_upgrade",
                "exclude_dump_test_schema",
                "schema_only",
                "schema_only_with_statistics",
                "only_dump_measurement",
            },
        },
        "REFRESH MATERIALIZED VIEW matview_third": {
            "regexp": _qr(
                r"""^
                \QREFRESH MATERIALIZED VIEW dump_test.matview_third;\E
                """, _XMS),
            "like": set(),
        },
        "REFRESH MATERIALIZED VIEW matview_fourth": {
            "regexp": _qr(
                r"""^
                \QREFRESH MATERIALIZED VIEW dump_test.matview_fourth;\E
                """, _XMS),
            "like": set(),
        },
        "REVOKE CONNECT ON DATABASE dump_test FROM public": {
            "create_order": 49,
            "create_sql": "REVOKE CONNECT ON DATABASE dump_test FROM public;",
            "regexp": _qr(
                r"""^
                \QREVOKE CONNECT,TEMPORARY ON DATABASE dump_test FROM PUBLIC;\E\n
                \QGRANT TEMPORARY ON DATABASE dump_test TO PUBLIC;\E\n
                \QGRANT CREATE ON DATABASE dump_test TO regress_dump_test_role;\E
                """, _XM),
            "like": {"pg_dumpall_dbprivs"},
        },
        "REVOKE EXECUTE ON FUNCTION pg_sleep() FROM public": {
            "create_order": 15,
            "create_sql":
                "REVOKE EXECUTE ON FUNCTION pg_sleep(float8)\n"
                "   FROM public;",
            "regexp": _qr(
                r"""^
                \QREVOKE ALL ON FUNCTION pg_catalog.pg_sleep(double precision) FROM PUBLIC;\E
                """, _XM),
            "like": full | {"section_pre_data"},
            "unlike": {"no_privs"},
        },
        "REVOKE EXECUTE ON FUNCTION pg_stat_reset FROM regress_dump_test_role": {
            "create_order": 15,
            "create_sql":
                "\n"
                "ALTER FUNCTION pg_stat_reset OWNER TO regress_dump_test_role;\n"
                "REVOKE EXECUTE ON FUNCTION pg_stat_reset\n"
                "  FROM regress_dump_test_role;",
            "regexp": _qr(r"^[^-].*pg_stat_reset.* regress_dump_test_role", _M),
            "like": set(),
        },
        "REVOKE SELECT ON TABLE pg_proc FROM public": {
            "create_order": 45,
            "create_sql": "REVOKE SELECT ON TABLE pg_proc FROM public;",
            "regexp": _qr(
                r"^\QREVOKE SELECT ON TABLE pg_catalog.pg_proc FROM PUBLIC;\E", _M),
            "like": full | {"section_pre_data"},
            "unlike": {"no_privs"},
        },
        "REVOKE ALL ON SCHEMA public": {
            "create_order": 16,
            "create_sql":
                'REVOKE ALL ON SCHEMA public FROM "regress_quoted  \\"" role";',
            "regexp": _qr(
                r'^REVOKE ALL ON SCHEMA public FROM "regress_quoted  \\"" role";', _M),
            "like": full | {"section_pre_data"},
            "unlike": {"no_privs"},
        },
        "REVOKE USAGE ON LANGUAGE plpgsql FROM public": {
            "create_order": 16,
            "create_sql": "REVOKE USAGE ON LANGUAGE plpgsql FROM public;",
            "regexp": _qr(r"^REVOKE ALL ON LANGUAGE plpgsql FROM PUBLIC;", _M),
            "like": full | dts | {
                "only_dump_test_table",
                "role",
                "section_pre_data",
                "only_dump_measurement",
            },
            "unlike": {"no_privs"},
        },
        "CREATE ACCESS METHOD regress_test_table_am": {
            "create_order": 11,
            "create_sql":
                "CREATE ACCESS METHOD regress_table_am TYPE TABLE HANDLER heap_tableam_handler;",
            "regexp": _qr(
                r"""^
                \QCREATE ACCESS METHOD regress_table_am TYPE TABLE HANDLER heap_tableam_handler;\E
                \n""", _XM),
            "like": full | {"section_pre_data"},
        },
        "CREATE TABLE regress_pg_dump_table_am": {
            "create_order": 12,
            "create_sql":
                "\n"
                "CREATE TABLE dump_test.regress_pg_dump_table_am_0() USING heap;\n"
                "CREATE TABLE dump_test.regress_pg_dump_table_am_1 (col1 int) USING regress_table_am;\n"
                "CREATE TABLE dump_test.regress_pg_dump_table_am_2() USING heap;",
            "regexp": _qr(
                r"""^
                \QSET default_table_access_method = regress_table_am;\E
                (\n(?!SET[^;]+;)[^\n]*)*
                \n\QCREATE TABLE dump_test.regress_pg_dump_table_am_1 (\E
                \n\s+\Qcol1 integer\E
                \n\);""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_table_access_method",
                "only_dump_measurement",
            },
        },
        "CREATE MATERIALIZED VIEW regress_pg_dump_matview_am": {
            "create_order": 13,
            "create_sql":
                "\n"
                "CREATE MATERIALIZED VIEW dump_test.regress_pg_dump_matview_am_0 USING heap AS SELECT 1;\n"
                "CREATE MATERIALIZED VIEW dump_test.regress_pg_dump_matview_am_1\n"
                "    USING regress_table_am AS SELECT count(*) FROM pg_class;\n"
                "CREATE MATERIALIZED VIEW dump_test.regress_pg_dump_matview_am_2 USING heap AS SELECT 1;",
            "regexp": _qr(
                r"""^
                \QSET default_table_access_method = regress_table_am;\E
                (\n(?!SET[^;]+;)[^\n]*)*
                \QCREATE MATERIALIZED VIEW dump_test.regress_pg_dump_matview_am_1 AS\E
                \n\s+\QSELECT count(*) AS count\E
                \n\s+\QFROM pg_class\E
                \n\s+\QWITH NO DATA;\E\n""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_table_access_method",
                "only_dump_measurement",
            },
        },
        "statistics_import": {
            "create_sql":
                "\n"
                "CREATE TABLE dump_test.has_stats\n"
                "AS SELECT g.g AS x, g.g / 2 AS y FROM generate_series(1,100) AS g(g);\n"
                "CREATE MATERIALIZED VIEW dump_test.has_stats_mv AS SELECT * FROM dump_test.has_stats;\n"
                'CREATE INDEX """dump_test""\'s post-data index" ON dump_test.has_stats(x, (x - 1));\n'
                "ANALYZE dump_test.has_stats, dump_test.has_stats_mv;",
            "regexp": _qr(
                r"""^
                \QSELECT * FROM pg_catalog.pg_restore_relation_stats(\E\s+
                'version',\s'\d+'::integer,\s+
                'schemaname',\s'dump_test',\s+
                'relname',\s'"dump_test"''s\ post-data\ index',\s+
                'relpages',\s'\d+'::integer,\s+
                'reltuples',\s'\d+'::real,\s+
                'relallvisible',\s'\d+'::integer,\s+
                'relallfrozen',\s'\d+'::integer\s+
                \);\s+
                \QSELECT * FROM pg_catalog.pg_restore_attribute_stats(\E\s+
                'version',\s'\d+'::integer,\s+
                'schemaname',\s'dump_test',\s+
                'relname',\s'"dump_test"''s\ post-data\ index',\s+
                'attnum',\s'2'::smallint,\s+
                'inherited',\s'f'::boolean,\s+
                'null_frac',\s'0'::real,\s+
                'avg_width',\s'4'::integer,\s+
                'n_distinct',\s'-1'::real,\s+
                'histogram_bounds',\s'\{[0-9,]+\}'::text,\s+
                'correlation',\s'1'::real\s+
                \);""", _XM),
            "like": full | dts | {
                "no_data_no_schema",
                "no_schema",
                "section_post_data",
                "statistics_only",
                "schema_only_with_statistics",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "no_statistics",
                "only_dump_measurement",
                "schema_only",
            },
        },
        "extended_statistics_import": {
            "create_sql":
                "\n"
                "CREATE TABLE dump_test.has_ext_stats\n"
                "AS SELECT g.g AS x, g.g / 2 AS y FROM generate_series(1,100) AS g(g);\n"
                "CREATE STATISTICS dump_test.es1 ON x, (y % 2) FROM dump_test.has_ext_stats;\n"
                "ANALYZE dump_test.has_ext_stats;",
            "regexp": _qr(
                r"""^
                \QSELECT * FROM pg_catalog.pg_restore_extended_stats(\E\s+""", _XM),
            "like": full | dts | {
                "no_data_no_schema",
                "no_schema",
                "section_post_data",
                "statistics_only",
                "schema_only_with_statistics",
            },
            "unlike": {
                "exclude_dump_test_schema",
                "no_statistics",
                "only_dump_measurement",
                "schema_only",
            },
        },
        "relstats_on_unanalyzed_tables": {
            "regexp": _qr(r"pg_catalog.pg_restore_relation_stats"),
            "like": full | dts | {
                "no_data_no_schema",
                "no_schema",
                "only_dump_test_table",
                "role",
                "role_parallel",
                "section_data",
                "section_post_data",
                "statistics_only",
                "schema_only_with_statistics",
            },
            "unlike": {
                "no_statistics",
                "schema_only",
            },
        },
        "CREATE TABLE regress_pg_dump_table_part": {
            "create_order": 19,
            "create_sql":
                "\n"
                "CREATE TABLE dump_test.regress_pg_dump_table_am_parent (id int) PARTITION BY LIST (id);\n"
                "ALTER TABLE dump_test.regress_pg_dump_table_am_parent SET ACCESS METHOD regress_table_am;\n"
                "CREATE TABLE dump_test.regress_pg_dump_table_am_child_1\n"
                "  PARTITION OF dump_test.regress_pg_dump_table_am_parent FOR VALUES IN (1);\n"
                "CREATE TABLE dump_test.regress_pg_dump_table_am_child_2\n"
                "  PARTITION OF dump_test.regress_pg_dump_table_am_parent FOR VALUES IN (2) USING heap;",
            "regexp": _qr(
                r"""^
                \n\QCREATE TABLE dump_test.regress_pg_dump_table_am_parent (\E
                (\n(?!SET[^;]+;)[^\n]*)*
                \QALTER TABLE dump_test.regress_pg_dump_table_am_parent SET ACCESS METHOD regress_table_am;\E
                (.*\n)*
                \QSET default_table_access_method = regress_table_am;\E
                (\n(?!SET[^;]+;)[^\n]*)*
                \n\QCREATE TABLE dump_test.regress_pg_dump_table_am_child_1 (\E
                (.*\n)*
                \QSET default_table_access_method = heap;\E
                (\n(?!SET[^;]+;)[^\n]*)*
                \n\QCREATE TABLE dump_test.regress_pg_dump_table_am_child_2 (\E
                (.*\n)*""", _XM),
            "like": full | dts | {"section_pre_data"},
            "unlike": {
                "exclude_dump_test_schema",
                "no_table_access_method",
                "only_dump_measurement",
            },
        },
    }


def _create_order_key(item):
    """Sort key implementing the create_order comparator.

    Tests with a create_order sort by it (ascending) and before tests without
    one.  Two orderless tests have unspecified relative order; we keep it
    stable by name, which is fine because such tests' create_sql is
    independent.
    """
    name, spec = item
    order = spec.get("create_order")
    return (0, order, name) if order is not None else (1, 0, name)


def _split_sql(sql):
    """Split *sql* into top-level statements on unquoted semicolons.

    Respects single-quoted strings, dollar-quoted bodies ($tag$...$tag$) and
    line comments (-- ... \\n) so that semicolons inside them are not treated
    as statement separators.  This mirrors how psql breaks a script into
    statements when the concatenated SQL is sent through a psql pipe.
    """
    stmts = []
    buf = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == "'":
                    # '' is an escaped quote inside the string.
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                buf.append(sql[i])
                i += 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end < 0:
                    end = n
                else:
                    end += len(tag)
                buf.append(sql[i:end])
                i = end
                continue
        if ch == ";":
            stmts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        stmts.append("".join(buf))

    # Re-merge statements split inside a "BEGIN ATOMIC ... END" function body:
    # the semicolons separating the body's statements are not real statement
    # boundaries.  Such a body opens with "BEGIN ATOMIC" and closes with a
    # trailing "END"; accumulate following pieces until that END is seen.
    merged = []
    pending = None
    for s in stmts:
        if pending is not None:
            pending = pending + ";" + s
        elif re.search(r"\bBEGIN\s+ATOMIC\b", s, re.IGNORECASE):
            pending = s
        else:
            merged.append(s)
            continue
        if re.search(r"\bEND\s*\Z", pending.rstrip(), re.IGNORECASE):
            merged.append(pending)
            pending = None
    if pending is not None:
        merged.append(pending)
    return [s for s in merged if s.strip()]


def _seed_database(node, dbname, create_sql):
    """Send a combined create_sql block to *dbname*, statement by statement.

    Piping the concatenated SQL to psql runs each top-level statement
    autonomously (its own transaction).  Sending the whole
    block via one libpq simple query would wrap it in a single transaction,
    which breaks the CREATE DATABASE / CREATE TABLESPACE statements present
    here.  So split into individual statements (honoring quoting/dollar-quotes)
    and run each on its own, matching psql semantics.
    """
    for stmt in _split_sql(create_sql):
        node.safe_sql(stmt, dbname=dbname)


def test_pg_dump(pg, tmp_path):
    node = pg
    port = node.port
    tempdir = str(tmp_path)

    supports_gzip = _have_pg_config_define("#define HAVE_LIBZ 1")
    supports_icu = _have_icu_configured()

    pgdump_runs = _pgdump_runs(tempdir, supports_gzip)
    tests = _tests(set(FULL_RUNS), set(DUMP_TEST_SCHEMA_RUNS))

    # See if this system supports CREATE COLLATION; if not, skip all the
    # COLLATION-related tests.
    res = node.sql("CREATE COLLATION testing FROM \"C\"; DROP COLLATION testing;")
    collation_support = res.error_message is None or "ERROR: " not in res.error_message

    # ICU doesn't work with some encodings.
    encoding = node.safe_sql("show server_encoding").strip()
    if encoding == "SQL_ASCII":
        supports_icu = False

    # Create additional databases for mutations of schema public.
    node.safe_sql("create database regress_pg_dump_test;")
    node.safe_sql("create database regress_public_owner;")

    #########################################
    # Set up schemas, tables, etc, to be dumped.  Build up the create
    # statements per-database in create_order, then send them.
    create_sql = {}
    for name, spec in sorted(tests.items(), key=_create_order_key):
        test_db = spec.get("database", "postgres")

        if spec.get("icu"):
            spec["collation"] = True

        if not spec.get("create_sql"):
            continue

        # Skip collation/icu commands if unsupported.
        if not collation_support and spec.get("collation"):
            continue
        if not supports_icu and spec.get("icu"):
            continue

        # Normalize command ending: strip trailing whitespace/newlines, add a
        # semicolon if missing, then two newlines.
        sql = spec["create_sql"].rstrip("\r\n")
        if not sql.endswith(";"):
            sql += ";"
        create_sql[test_db] = create_sql.get(test_db, "") + sql + "\n\n"

    for db in sorted(create_sql):
        _seed_database(node, db, create_sql[db])

    #########################################
    # Standalone command cases (run before the matrix loop).

    node.command_fails_like(
        ["pg_dump", "--port", str(port), "qqq"],
        r'pg_dump: error: connection to server .* failed: FATAL:  database "qqq" does not exist',
        "connecting to a non-existent database",
    )
    node.command_fails_like(
        ["pg_dump", "--dbname", "regression_invalid"],
        r'pg_dump: error: connection to server .* failed: FATAL:  cannot connect to invalid database "regression_invalid"',
        "connecting to an invalid database",
    )
    node.command_fails_like(
        ["pg_dump", "--port", str(port), "--role", "regress_dump_test_role"],
        re.escape("pg_dump: error: query failed: ERROR:  permission denied for"),
        "connecting with an unprivileged user",
    )
    node.command_fails_like(
        ["pg_dump", "--port", str(port), "--schema", "nonexistent"],
        re.escape("pg_dump: error: no matching schemas were found"),
        "dumping a non-existent schema",
    )
    node.command_fails_like(
        ["pg_dump", "--port", str(port), "--table", "nonexistent"],
        re.escape("pg_dump: error: no matching tables were found"),
        "dumping a non-existent table",
    )
    node.command_fails_like(
        ["pg_dump", "--port", str(port), "--strict-names", "--schema", "nonexistent*"],
        re.escape("pg_dump: error: no matching schemas were found for pattern"),
        "no matching schemas",
    )
    node.command_fails_like(
        ["pg_dump", "--port", str(port), "--strict-names", "--schema-only", "--statistics"],
        re.escape("pg_dump: error: options --statistics and -s/--schema-only cannot be used together"),
        "cannot use --statistics and --schema-only together",
    )
    node.command_fails_like(
        ["pg_dump", "--port", str(port), "--strict-names", "--table", "nonexistent*"],
        re.escape("pg_dump: error: no matching tables were found for pattern"),
        "no matching tables",
    )
    node.command_fails_like(
        ["pg_dumpall", "--exclude-database", "."],
        r"pg_dumpall: error: improper qualified name \(too many dotted names\): \.",
        'pg_dumpall: option --exclude-database rejects multipart pattern "."',
    )
    node.command_fails_like(
        ["pg_dumpall", "--exclude-database", "myhost.mydb"],
        r"pg_dumpall: error: improper qualified name \(too many dotted names\): myhost\.mydb",
        "pg_dumpall: option --exclude-database rejects multipart database names",
    )
    node.command_ok(
        [
            "pg_dump",
            "--port", str(port),
            "--schema", "pg_catalog",
            "--file", f"{tempdir}/pgdump_pgcatalog.dmp",
        ],
        "pg_dump: option -n pg_catalog",
    )
    node.command_ok(
        [
            "pg_dumpall",
            "--port", str(port),
            "--exclude-database", '"myhost.mydb"',
            "--file", f"{tempdir}/pgdumpall.dmp",
        ],
        "pg_dumpall: option --exclude-database handles database names with embedded dots",
    )
    node.command_fails_like(
        ["pg_dump", "--schema", "myhost.mydb.myschema"],
        r"pg_dump: error: improper qualified name \(too many dotted names\): myhost\.mydb\.myschema",
        "pg_dump: option --schema rejects three-part schema names",
    )
    node.command_fails_like(
        ["pg_dump", "--schema", "otherdb.myschema"],
        r"pg_dump: error: cross-database references are not implemented: otherdb\.myschema",
        "pg_dump: option --schema rejects cross-database multipart schema names",
    )
    node.command_fails_like(
        ["pg_dump", "--schema", "."],
        r"pg_dump: error: cross-database references are not implemented: \.",
        'pg_dump: option --schema rejects degenerate two-part schema name: "."',
    )
    node.command_fails_like(
        ["pg_dump", "--schema", '"some.other.db".myschema'],
        r'pg_dump: error: cross-database references are not implemented: "some\.other\.db"\.myschema',
        "pg_dump: option --schema rejects cross-database multipart schema names with embedded dots",
    )
    node.command_fails_like(
        ["pg_dump", "--schema", ".."],
        r"pg_dump: error: improper qualified name \(too many dotted names\): \.\.",
        'pg_dump: option --schema rejects degenerate three-part schema name: ".."',
    )
    node.command_fails_like(
        ["pg_dump", "--table", "myhost.mydb.myschema.mytable"],
        r"pg_dump: error: improper relation name \(too many dotted names\): myhost\.mydb\.myschema\.mytable",
        "pg_dump: option --table rejects four-part table names",
    )
    node.command_fails_like(
        ["pg_dump", "--table", "otherdb.pg_catalog.pg_class"],
        r"pg_dump: error: cross-database references are not implemented: otherdb\.pg_catalog\.pg_class",
        "pg_dump: option --table rejects cross-database three part table names",
    )
    node.command_fails_like(
        ["pg_dump", "--port", str(port), "--table", '"some.other.db".pg_catalog.pg_class'],
        r'pg_dump: error: cross-database references are not implemented: "some\.other\.db"\.pg_catalog\.pg_class',
        "pg_dump: option --table rejects cross-database three part table names with embedded dots",
    )

    #########################################
    # Run all runs (sorted by name).
    all_runs = set(pgdump_runs)
    for run in sorted(pgdump_runs):
        spec = pgdump_runs[run]
        test_key = run
        run_db = spec.get("database", "postgres")

        node.command_ok(spec["dump_cmd"], f"{run}: pg_dump runs")

        for glob_pattern in spec.get("glob_patterns", []):
            import glob as _glob
            matches = _glob.glob(glob_pattern)
            ok = len(matches) > 1 or (len(matches) == 1 and os.path.isfile(matches[0]))
            assert ok, f"{run}: glob check for {glob_pattern}"

        if "command_like" in spec:
            cl = spec["command_like"]
            node.command_like(cl["command"], cl["expected"], f"{run}: {cl['name']}")

        if "restore_cmd" in spec:
            node.command_ok(spec["restore_cmd"], f"{run}: pg_restore runs")

        if "test_key" in spec:
            test_key = spec["test_key"]

        output_file = slurp_file(os.path.join(tempdir, f"{run}.sql"))

        #########################################
        # Run all tests where this run is included as a 'like' or 'unlike'.
        for test_name in sorted(tests):
            tspec = tests[test_name]
            test_db = tspec.get("database", "postgres")

            all_runs_flag = tspec.get("all_runs", False)
            like = tspec.get("like")
            unlike = tspec.get("unlike", set())

            # Either all_runs should be set or there must be a "like" list
            # (even an empty one), to keep the test self-documenting.
            assert all_runs_flag or like is not None, (
                f'missing "like" in test "{test_name}"'
            )
            like_set = like if like is not None else set()

            # Check for useless entries in "unlike": a run not listed in "like"
            # doesn't need excluding.
            assert not (test_key in unlike and test_key not in like_set), (
                f'useless "unlike" entry "{test_key}" in test "{test_name}"'
            )

            # Skip collation/icu commands if unsupported.
            if not collation_support and tspec.get("collation"):
                continue
            if not supports_icu and tspec.get("icu"):
                continue

            # A run only applies to tests targeting the same database.
            if run_db != test_db:
                continue

            if (test_key in like_set or all_runs_flag) and test_key not in unlike:
                assert tspec["regexp"].search(output_file), (
                    f"{run}: should dump {test_name}\n"
                    f"Review {run} results in {tempdir}"
                )
            else:
                assert not tspec["regexp"].search(output_file), (
                    f"{run}: should not dump {test_name}\n"
                    f"Review {run} results in {tempdir}"
                )

    node.stop("fast")
