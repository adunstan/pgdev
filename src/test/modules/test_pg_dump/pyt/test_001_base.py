# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Exercise pg_dump against the test_pg_dump extension.

Exercises pg_dump (and pg_dumpall/pg_restore) against the test_pg_dump
extension using a matrix of named "runs" (pg_dump invocations with different
options) and named "tests" (each with a regexp and like/unlike sets stating
which runs the regexp is expected to match).

pg_dump / pg_restore / pg_dumpall are the binaries under test and are run as
subprocesses through the node's pg_bin (PGHOST/PGPORT point at the server).
The seed SQL the test itself runs is executed in-process via safe_sql.
"""

import os
import re
import subprocess

import pytest

from pypg.util import slurp_file


def _have_pg_config_define(define):
    """Return True if the installed pg_config.h contains the given #define."""
    try:
        out = subprocess.run(
            ["pg_config", "--includedir"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    header = os.path.join(out, "pg_config.h")
    try:
        with open(header, encoding="utf-8", errors="replace") as fh:
            return define in fh.read()
    except OSError:
        return False


def _have_extension(node, extname):
    """Return True if *extname* is installable (present in the install tree)."""
    return (
        node.safe_sql(
            "SELECT count(*) > 0 FROM pg_available_extensions "
            f"WHERE name = '{extname}'"
        )
        == "t"
    )


# Runs that pg_dump considers "full" dumps, but with flags excluding specific
# items (ACLs, LOs, etc.).  The set of runs that produce a full dump.
FULL_RUNS = {
    "binary_upgrade",
    "clean",
    "clean_if_exists",
    "createdb",
    "defaults",
    "exclude_table",
    "no_privs",
    "no_owner",
    "privileged_internals",
    "with_extension",
    "exclude_extension",
    "exclude_extension_filter",
    "without_extension",
}


def _pgdump_runs(tempdir):
    """Definition of the pg_dump runs to make.

    Each run has a dump_cmd; some have a restore_cmd, a test_key (reuse another
    run's like/unlike sets), and/or a compile_option gating it on a build
    feature.  Commands are argv lists; cmd[0] is resolved in the node's bindir.
    """
    return {
        "binary_upgrade": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/binary_upgrade.sql",
                "--schema-only",
                "--sequence-data",
                "--binary-upgrade",
                "--dbname",
                "postgres",
            ],
        },
        "clean": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/clean.sql",
                "--clean",
                "--dbname",
                "postgres",
            ],
        },
        "clean_if_exists": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/clean_if_exists.sql",
                "--clean",
                "--if-exists",
                "--encoding",
                "UTF8",  # no-op, just tests that it is accepted
                "postgres",
            ],
        },
        "createdb": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/createdb.sql",
                "--create",
                "--no-reconnect",  # no-op, just for testing
                "postgres",
            ],
        },
        "data_only": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/data_only.sql",
                "--data-only",
                "--verbose",  # no-op, just make sure it works
                "postgres",
            ],
        },
        "defaults": {
            "dump_cmd": [
                "pg_dump",
                "--file",
                f"{tempdir}/defaults.sql",
                "postgres",
            ],
        },
        "defaults_custom_format": {
            "test_key": "defaults",
            "compile_option": "gzip",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "custom",
                "--compress",
                "6",
                "--file",
                f"{tempdir}/defaults_custom_format.dump",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file",
                f"{tempdir}/defaults_custom_format.sql",
                f"{tempdir}/defaults_custom_format.dump",
            ],
        },
        "defaults_dir_format": {
            "test_key": "defaults",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "directory",
                "--file",
                f"{tempdir}/defaults_dir_format",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file",
                f"{tempdir}/defaults_dir_format.sql",
                f"{tempdir}/defaults_dir_format",
            ],
        },
        "defaults_parallel": {
            "test_key": "defaults",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "directory",
                "--jobs",
                "2",
                "--file",
                f"{tempdir}/defaults_parallel",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file",
                f"{tempdir}/defaults_parallel.sql",
                f"{tempdir}/defaults_parallel",
            ],
        },
        "defaults_tar_format": {
            "test_key": "defaults",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "tar",
                "--file",
                f"{tempdir}/defaults_tar_format.tar",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file",
                f"{tempdir}/defaults_tar_format.sql",
                f"{tempdir}/defaults_tar_format.tar",
            ],
        },
        "exclude_table": {
            "dump_cmd": [
                "pg_dump",
                "--exclude-table",
                "regress_table_dumpable",
                "--file",
                f"{tempdir}/exclude_table.sql",
                "postgres",
            ],
        },
        "extension_schema": {
            "dump_cmd": [
                "pg_dump",
                "--schema",
                "public",
                "--file",
                f"{tempdir}/extension_schema.sql",
                "postgres",
            ],
        },
        "pg_dumpall_globals": {
            "dump_cmd": [
                "pg_dumpall",
                "--no-sync",
                "--file",
                f"{tempdir}/pg_dumpall_globals.sql",
                "--globals-only",
            ],
        },
        "no_privs": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/no_privs.sql",
                "--no-privileges",
                "postgres",
            ],
        },
        "no_owner": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/no_owner.sql",
                "--no-owner",
                "postgres",
            ],
        },
        # regress_dump_login_role shouldn't need SELECT rights on internal
        # (undumped) extension tables
        "privileged_internals": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/privileged_internals.sql",
                # these two tables are irrelevant to the test case
                "--exclude-table",
                "regress_pg_dump_schema.external_tab",
                "--exclude-table",
                "regress_pg_dump_schema.extdependtab",
                "--username",
                "regress_dump_login_role",
                "postgres",
            ],
        },
        "schema_only": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/schema_only.sql",
                "--schema-only",
                "postgres",
            ],
        },
        "section_pre_data": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/section_pre_data.sql",
                "--section",
                "pre-data",
                "postgres",
            ],
        },
        "section_data": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/section_data.sql",
                "--section",
                "data",
                "postgres",
            ],
        },
        "section_post_data": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/section_post_data.sql",
                "--section",
                "post-data",
                "postgres",
            ],
        },
        "with_extension": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/with_extension.sql",
                "--extension",
                "test_pg_dump",
                "postgres",
            ],
        },
        "exclude_extension": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/exclude_extension.sql",
                "--exclude-extension",
                "test_pg_dump",
                "postgres",
            ],
        },
        "exclude_extension_filter": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/exclude_extension_filter.sql",
                "--filter",
                f"{tempdir}/exclude_extension_filter.txt",
                "postgres",
            ],
        },
        # plpgsql in the list blocks the dump of extension test_pg_dump
        "without_extension": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/without_extension.sql",
                "--extension",
                "plpgsql",
                "postgres",
            ],
        },
        # plpgsql in the list of extensions blocks the dump of extension
        # test_pg_dump.  "public" is the schema used by the extension
        # test_pg_dump, but none of its objects should be dumped.
        "without_extension_explicit_schema": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/without_extension_explicit_schema.sql",
                "--extension",
                "plpgsql",
                "--schema",
                "public",
                "postgres",
            ],
        },
        # plpgsql in the list of extensions blocks the dump of extension
        # test_pg_dump, but not the dump of objects not dependent on the
        # extension located on a schema maintained by the extension.
        "without_extension_internal_schema": {
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--file",
                f"{tempdir}/without_extension_internal_schema.sql",
                "--extension",
                "plpgsql",
                "--schema",
                "regress_pg_dump_schema",
                "postgres",
            ],
        },
    }


# The pattern strings are written in verbose form: whitespace and comments are
# stripped via re.VERBOSE, re.MULTILINE is always added, and re.DOTALL is added
# where a pattern must match across newlines.
_XM = re.VERBOSE | re.MULTILINE
_XMS = re.VERBOSE | re.MULTILINE | re.DOTALL


def _tests():
    """Definition of the tests to run.

    Each entry: create_order/create_sql (seed SQL, run before any dump),
    regexp (compiled), and like/unlike sets of run names.  A run listed in
    'like' (and not in 'unlike') must match the regexp; every other run must
    not match it.
    """
    full = set(FULL_RUNS)
    return {
        "ALTER EXTENSION test_pg_dump": {
            "create_order": 9,
            "create_sql": "ALTER EXTENSION test_pg_dump ADD TABLE "
            "regress_pg_dump_table_added;",
            "regexp": re.compile(
                r"^CREATE\ TABLE\ public\.regress_pg_dump_table_added\ \(\n"
                r"\s+col1\ integer\ NOT\ NULL,\n"
                r"\s+col2\ integer\n"
                r"\);\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "CREATE EXTENSION test_pg_dump": {
            "create_order": 2,
            "create_sql": "CREATE EXTENSION test_pg_dump;",
            "regexp": re.compile(
                r"^CREATE\ EXTENSION\ IF\ NOT\ EXISTS\ test_pg_dump\ WITH\ "
                r"SCHEMA\ public;\n",
                _XM,
            ),
            "like": full | {"schema_only", "section_pre_data"},
            "unlike": {
                "binary_upgrade",
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "CREATE ROLE regress_dump_test_role": {
            "create_order": 1,
            "create_sql": "CREATE ROLE regress_dump_test_role;",
            "regexp": re.compile(
                r"^CREATE ROLE regress_dump_test_role;\n", re.MULTILINE
            ),
            "like": {"pg_dumpall_globals"},
        },
        "CREATE ROLE regress_dump_login_role": {
            "create_order": 1,
            "create_sql": "CREATE ROLE regress_dump_login_role LOGIN;",
            "regexp": re.compile(
                r"^CREATE\ ROLE\ regress_dump_login_role;\n"
                r"ALTER\ ROLE\ regress_dump_login_role\ WITH\ .*\ LOGIN\ .*;\n",
                _XM,
            ),
            "like": {"pg_dumpall_globals"},
        },
        "GRANT ALTER SYSTEM ON PARAMETER full_page_writes TO "
        "regress_dump_test_role": {
            "create_order": 2,
            "create_sql": "GRANT ALTER SYSTEM ON PARAMETER full_page_writes TO "
            "regress_dump_test_role;",
            "regexp": re.compile(
                r"^GRANT ALTER SYSTEM ON PARAMETER full_page_writes TO "
                r"regress_dump_test_role;",
                re.MULTILINE,
            ),
            "like": {"pg_dumpall_globals"},
        },
        "GRANT ALL ON PARAMETER Custom.Knob TO regress_dump_test_role WITH "
        "GRANT OPTION": {
            "create_order": 2,
            "create_sql": "GRANT SET, ALTER SYSTEM ON PARAMETER Custom.Knob TO "
            "regress_dump_test_role WITH GRANT OPTION;",
            # "set" plus "alter system" is "all" privileges on parameters
            "regexp": re.compile(
                r'^GRANT ALL ON PARAMETER "custom.knob" TO '
                r"regress_dump_test_role WITH GRANT OPTION;",
                re.MULTILINE,
            ),
            "like": {"pg_dumpall_globals"},
        },
        "GRANT ALL ON PARAMETER DateStyle TO regress_dump_test_role": {
            "create_order": 2,
            "create_sql": 'GRANT ALL ON PARAMETER "DateStyle" TO regress_dump_test_role '
            "WITH GRANT OPTION; REVOKE GRANT OPTION FOR ALL ON PARAMETER "
            "DateStyle FROM regress_dump_test_role;",
            # The revoke simplifies the ultimate grant so as to not include
            # "with grant option"
            "regexp": re.compile(
                r"^GRANT ALL ON PARAMETER datestyle TO regress_dump_test_role;",
                re.MULTILINE,
            ),
            "like": {"pg_dumpall_globals"},
        },
        "CREATE SCHEMA public": {
            "regexp": re.compile(r"^CREATE SCHEMA public;", re.MULTILINE),
            "like": {"extension_schema", "without_extension_explicit_schema"},
        },
        "CREATE SEQUENCE regress_pg_dump_table_col1_seq": {
            "regexp": re.compile(
                r"^CREATE\ SEQUENCE\ public\.regress_pg_dump_table_col1_seq\n"
                r"\s+AS\ integer\n"
                r"\s+START\ WITH\ 1\n"
                r"\s+INCREMENT\ BY\ 1\n"
                r"\s+NO\ MINVALUE\n"
                r"\s+NO\ MAXVALUE\n"
                r"\s+CACHE\ 1;\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "CREATE TABLE regress_pg_dump_table_added": {
            "create_order": 7,
            "create_sql": "CREATE TABLE regress_pg_dump_table_added "
            "(col1 int not null, col2 int);",
            "regexp": re.compile(
                r"^CREATE\ TABLE\ public\.regress_pg_dump_table_added\ \(\n"
                r"\s+col1\ integer\ NOT\ NULL,\n"
                r"\s+col2\ integer\n"
                r"\);\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "CREATE SEQUENCE regress_pg_dump_seq": {
            "regexp": re.compile(
                r"^CREATE\ SEQUENCE\ public\.regress_pg_dump_seq\n"
                r"\s+START\ WITH\ 1\n"
                r"\s+INCREMENT\ BY\ 1\n"
                r"\s+NO\ MINVALUE\n"
                r"\s+NO\ MAXVALUE\n"
                r"\s+CACHE\ 1;\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "SETVAL SEQUENCE regress_seq_dumpable": {
            "create_order": 6,
            "create_sql": "SELECT nextval('regress_seq_dumpable');",
            "regexp": re.compile(
                r"^SELECT\ pg_catalog\.setval\("
                r"'public\.regress_seq_dumpable',\ 1,\ true\);\n",
                _XM,
            ),
            "like": full | {"data_only", "section_data", "extension_schema"},
            "unlike": {
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "CREATE TABLE regress_pg_dump_table": {
            "regexp": re.compile(
                r"^CREATE\ TABLE\ public\.regress_pg_dump_table\ \(\n"
                r"\s+col1\ integer\ NOT\ NULL,\n"
                r"\s+col2\ integer,\n"
                r"\s+CONSTRAINT\ regress_pg_dump_table_col2_check\ "
                r"CHECK\ \(\(col2\ >\ 0\)\)\n"
                r"\);\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "COPY public.regress_table_dumpable (col1)": {
            "regexp": re.compile(
                r"^COPY\ public\.regress_table_dumpable\ \(col1\)\ FROM\ stdin;\n",
                _XM,
            ),
            "like": full | {"data_only", "section_data", "extension_schema"},
            "unlike": {
                "binary_upgrade",
                "exclude_table",
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "REVOKE ALL ON FUNCTION wgo_then_no_access": {
            "create_order": 3,
            "create_sql": "DO $$BEGIN EXECUTE format(\n"
            "    'REVOKE ALL ON FUNCTION wgo_then_no_access()\n"
            "     FROM pg_signal_backend, public, %I',\n"
            "    (SELECT usename\n"
            "     FROM pg_user JOIN pg_proc ON proowner = usesysid\n"
            "     WHERE proname = 'wgo_then_no_access')); END$$;",
            "regexp": re.compile(
                r"^REVOKE\ ALL\ ON\ FUNCTION\ public\.wgo_then_no_access\(\)\ "
                r"FROM\ PUBLIC;\n"
                r"REVOKE\ ALL\ ON\ FUNCTION\ public\.wgo_then_no_access\(\)\ "
                r"FROM\ .*;\n"
                r"REVOKE\ ALL\ ON\ FUNCTION\ public\.wgo_then_no_access\(\)\ "
                r"FROM\ pg_signal_backend;\n",
                _XM,
            ),
            "like": full | {"schema_only", "section_pre_data"},
            "unlike": {
                "no_privs",
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "REVOKE GRANT OPTION FOR UPDATE ON SEQUENCE wgo_then_regular": {
            "create_order": 3,
            "create_sql": "REVOKE GRANT OPTION FOR UPDATE ON SEQUENCE "
            "wgo_then_regular FROM pg_signal_backend;",
            "regexp": re.compile(
                r"^REVOKE\ ALL\ ON\ SEQUENCE\ public\.wgo_then_regular\ "
                r"FROM\ pg_signal_backend;\n"
                r"GRANT\ SELECT,UPDATE\ ON\ SEQUENCE\ "
                r"public\.wgo_then_regular\ TO\ pg_signal_backend;\n"
                r"GRANT\ USAGE\ ON\ SEQUENCE\ public\.wgo_then_regular\ "
                r"TO\ pg_signal_backend\ WITH\ GRANT\ OPTION;\n",
                _XM,
            ),
            "like": full | {"schema_only", "section_pre_data"},
            "unlike": {
                "no_privs",
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "CREATE ACCESS METHOD regress_test_am": {
            "regexp": re.compile(
                r"^CREATE\ ACCESS\ METHOD\ regress_test_am\ TYPE\ INDEX\ "
                r"HANDLER\ bthandler;\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "COMMENT ON EXTENSION test_pg_dump": {
            "regexp": re.compile(
                r"^COMMENT\ ON\ EXTENSION\ test_pg_dump\ "
                r"IS\ 'Test\ pg_dump\ with\ an\ extension';\n",
                _XM,
            ),
            "like": full | {"schema_only", "section_pre_data"},
            "unlike": {
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "GRANT SELECT regress_pg_dump_table_added pre-ALTER EXTENSION": {
            "create_order": 8,
            "create_sql": "GRANT SELECT ON regress_pg_dump_table_added TO "
            "regress_dump_test_role;",
            "regexp": re.compile(
                r"^GRANT\ SELECT\ ON\ TABLE\ "
                r"public\.regress_pg_dump_table_added\ "
                r"TO\ regress_dump_test_role;\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "REVOKE SELECT regress_pg_dump_table_added post-ALTER EXTENSION": {
            "create_order": 10,
            "create_sql": "REVOKE SELECT ON regress_pg_dump_table_added FROM "
            "regress_dump_test_role;",
            "regexp": re.compile(
                r"^REVOKE\ SELECT\ ON\ TABLE\ "
                r"public\.regress_pg_dump_table_added\ "
                r"FROM\ regress_dump_test_role;\n",
                _XM,
            ),
            "like": full | {"schema_only", "section_pre_data"},
            "unlike": {
                "no_privs",
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "GRANT SELECT ON TABLE regress_pg_dump_table": {
            "regexp": re.compile(
                r"^SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(true\);\n"
                r"GRANT\ SELECT\ ON\ TABLE\ public\.regress_pg_dump_table\ "
                r"TO\ regress_dump_test_role;\n"
                r"SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(false\);\n",
                _XMS,
            ),
            "like": {"binary_upgrade"},
        },
        "GRANT SELECT(col1) ON regress_pg_dump_table": {
            "regexp": re.compile(
                r"^SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(true\);\n"
                r"GRANT\ SELECT\(col1\)\ ON\ TABLE\ "
                r"public\.regress_pg_dump_table\ TO\ PUBLIC;\n"
                r"SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(false\);\n",
                _XMS,
            ),
            "like": {"binary_upgrade"},
        },
        "GRANT SELECT(col2) ON regress_pg_dump_table TO "
        "regress_dump_test_role": {
            "create_order": 4,
            "create_sql": "GRANT SELECT(col2) ON regress_pg_dump_table\n"
            "               TO regress_dump_test_role;",
            "regexp": re.compile(
                r"^GRANT\ SELECT\(col2\)\ ON\ TABLE\ "
                r"public\.regress_pg_dump_table\ TO\ regress_dump_test_role;\n",
                _XM,
            ),
            "like": full | {"schema_only", "section_pre_data"},
            "unlike": {
                "no_privs",
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "GRANT USAGE ON regress_pg_dump_table_col1_seq TO "
        "regress_dump_test_role": {
            "create_order": 5,
            "create_sql": "GRANT USAGE ON SEQUENCE regress_pg_dump_table_col1_seq\n"
            "                   TO regress_dump_test_role;",
            "regexp": re.compile(
                r"^GRANT\ USAGE\ ON\ SEQUENCE\ "
                r"public\.regress_pg_dump_table_col1_seq\ "
                r"TO\ regress_dump_test_role;\n",
                _XM,
            ),
            "like": full | {"schema_only", "section_pre_data"},
            "unlike": {
                "no_privs",
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        "GRANT USAGE ON regress_pg_dump_seq TO regress_dump_test_role": {
            "regexp": re.compile(
                r"^GRANT\ USAGE\ ON\ SEQUENCE\ public\.regress_pg_dump_seq\ "
                r"TO\ regress_dump_test_role;\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "REVOKE SELECT(col1) ON regress_pg_dump_table": {
            "create_order": 3,
            "create_sql": "REVOKE SELECT(col1) ON regress_pg_dump_table\n"
            "               FROM PUBLIC;",
            "regexp": re.compile(
                r"^REVOKE\ SELECT\(col1\)\ ON\ TABLE\ "
                r"public\.regress_pg_dump_table\ FROM\ PUBLIC;\n",
                _XM,
            ),
            "like": full | {"schema_only", "section_pre_data"},
            "unlike": {
                "no_privs",
                "exclude_extension",
                "exclude_extension_filter",
                "without_extension",
            },
        },
        # Objects included in extension part of a schema created by this
        # extension
        "CREATE TABLE regress_pg_dump_schema.test_table": {
            "regexp": re.compile(
                r"^CREATE\ TABLE\ regress_pg_dump_schema\.test_table\ \(\n"
                r"\s+col1\ integer,\n"
                r"\s+col2\ integer,\n"
                r"\s+CONSTRAINT\ test_table_col2_check\ "
                r"CHECK\ \(\(col2\ >\ 0\)\)\n"
                r"\);\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "GRANT SELECT ON regress_pg_dump_schema.test_table": {
            "regexp": re.compile(
                r"^SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(true\);\n"
                r"GRANT\ SELECT\ ON\ TABLE\ "
                r"regress_pg_dump_schema\.test_table\ "
                r"TO\ regress_dump_test_role;\n"
                r"SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(false\);\n",
                _XMS,
            ),
            "like": {"binary_upgrade"},
        },
        "CREATE SEQUENCE regress_pg_dump_schema.test_seq": {
            "regexp": re.compile(
                r"^CREATE\ SEQUENCE\ regress_pg_dump_schema\.test_seq\n"
                r"\s+START\ WITH\ 1\n"
                r"\s+INCREMENT\ BY\ 1\n"
                r"\s+NO\ MINVALUE\n"
                r"\s+NO\ MAXVALUE\n"
                r"\s+CACHE\ 1;\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "GRANT USAGE ON regress_pg_dump_schema.test_seq": {
            "regexp": re.compile(
                r"^SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(true\);\n"
                r"GRANT\ USAGE\ ON\ SEQUENCE\ "
                r"regress_pg_dump_schema\.test_seq\ "
                r"TO\ regress_dump_test_role;\n"
                r"SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(false\);\n",
                _XMS,
            ),
            "like": {"binary_upgrade"},
        },
        "CREATE TYPE regress_pg_dump_schema.test_type": {
            "regexp": re.compile(
                r"^CREATE\ TYPE\ regress_pg_dump_schema\.test_type\ AS\ \(\n"
                r"\s+col1\ integer\n"
                r"\);\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "GRANT USAGE ON regress_pg_dump_schema.test_type": {
            "regexp": re.compile(
                r"^SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(true\);\n"
                r"GRANT\ ALL\ ON\ TYPE\ regress_pg_dump_schema\.test_type\ "
                r"TO\ regress_dump_test_role;\n"
                r"SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(false\);\n",
                _XMS,
            ),
            "like": {"binary_upgrade"},
        },
        "CREATE FUNCTION regress_pg_dump_schema.test_func": {
            "regexp": re.compile(
                r"^CREATE\ FUNCTION\ regress_pg_dump_schema\.test_func\(\)\ "
                r"RETURNS\ integer\n"
                r"\s+LANGUAGE\ sql\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "GRANT ALL ON regress_pg_dump_schema.test_func": {
            "regexp": re.compile(
                r"^SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(true\);\n"
                r"GRANT\ ALL\ ON\ FUNCTION\ "
                r"regress_pg_dump_schema\.test_func\(\)\ "
                r"TO\ regress_dump_test_role;\n"
                r"SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(false\);\n",
                _XMS,
            ),
            "like": {"binary_upgrade"},
        },
        "CREATE AGGREGATE regress_pg_dump_schema.test_agg": {
            "regexp": re.compile(
                r"^CREATE\ AGGREGATE\ "
                r"regress_pg_dump_schema\.test_agg\(smallint\)\ \(\n"
                r"\s+SFUNC\ =\ int2_sum,\n"
                r"\s+STYPE\ =\ bigint\n"
                r"\);\n",
                _XM,
            ),
            "like": {"binary_upgrade"},
        },
        "GRANT ALL ON regress_pg_dump_schema.test_agg": {
            "regexp": re.compile(
                r"^SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(true\);\n"
                r"GRANT\ ALL\ ON\ FUNCTION\ "
                r"regress_pg_dump_schema\.test_agg\(smallint\)\ "
                r"TO\ regress_dump_test_role;\n"
                r"SELECT\ pg_catalog\.binary_upgrade_set_record_init_privs"
                r"\(false\);\n",
                _XMS,
            ),
            "like": {"binary_upgrade"},
        },
        "ALTER INDEX pkey DEPENDS ON extension": {
            "create_order": 11,
            "create_sql": "CREATE TABLE regress_pg_dump_schema.extdependtab "
            "(col1 integer primary key, col2 int);\n"
            "CREATE INDEX ON regress_pg_dump_schema.extdependtab (col2);\n"
            "ALTER INDEX regress_pg_dump_schema.extdependtab_col2_idx "
            "DEPENDS ON EXTENSION test_pg_dump;\n"
            "ALTER INDEX regress_pg_dump_schema.extdependtab_pkey "
            "DEPENDS ON EXTENSION test_pg_dump;",
            "regexp": re.compile(
                r"^ALTER\ INDEX\ "
                r"regress_pg_dump_schema\.extdependtab_pkey\ "
                r"DEPENDS\ ON\ EXTENSION\ test_pg_dump;\n",
                _XMS,
            ),
            "like": "ALL",
            "unlike": {
                "data_only",
                "extension_schema",
                "pg_dumpall_globals",
                "privileged_internals",
                "section_data",
                "section_pre_data",
                # Excludes this schema as extension is not listed.
                "without_extension_explicit_schema",
            },
        },
        "ALTER INDEX idx DEPENDS ON extension": {
            "regexp": re.compile(
                r"^ALTER\ INDEX\ "
                r"regress_pg_dump_schema\.extdependtab_col2_idx\ "
                r"DEPENDS\ ON\ EXTENSION\ test_pg_dump;\n",
                _XMS,
            ),
            "like": "ALL",
            "unlike": {
                "data_only",
                "extension_schema",
                "pg_dumpall_globals",
                "privileged_internals",
                "section_data",
                "section_pre_data",
                # Excludes this schema as extension is not listed.
                "without_extension_explicit_schema",
            },
        },
        # Objects not included in extension, part of schema created by extension
        "CREATE TABLE regress_pg_dump_schema.external_tab": {
            "create_order": 4,
            "create_sql": "CREATE TABLE regress_pg_dump_schema.external_tab\n"
            "               (col1 int);",
            "regexp": re.compile(
                r"^CREATE\ TABLE\ regress_pg_dump_schema\.external_tab\ \(\n"
                r"\s+col1\ integer\n"
                r"\);\n",
                _XM,
            ),
            "like": full
            | {
                "schema_only",
                "section_pre_data",
                # Excludes the extension and keeps the schema's data.
                "without_extension_internal_schema",
            },
            "unlike": {"privileged_internals"},
        },
    }


def _create_order_key(item):
    """Sort key controlling the order in which objects are created.

    Tests with a create_order sort by it (ascending) and before tests without
    one; among tests without a create_order, order is irrelevant for the
    concatenated seed SQL but we keep it stable by name.
    """
    _name, spec = item
    order = spec.get("create_order")
    return (0, order) if order is not None else (1, 0)


def test_pg_dump_base(pg, tmp_path):
    node = pg

    if not _have_extension(node, "test_pg_dump"):
        pytest.skip("test_pg_dump extension not installed")

    tempdir = str(tmp_path)
    pgdump_runs = _pgdump_runs(tempdir)
    tests = _tests()

    supports_gzip = _have_pg_config_define("#define HAVE_LIBZ 1")

    # Build up the combined create statements in create_order, then seed.
    create_sql = ""
    for _name, spec in sorted(tests.items(), key=_create_order_key):
        if spec.get("create_sql"):
            create_sql += spec["create_sql"]
    node.safe_sql(create_sql)

    # Create filter file for exclude_extension_filter test.
    with open(
        os.path.join(tempdir, "exclude_extension_filter.txt"), "w", encoding="utf-8"
    ) as fh:
        fh.write("exclude extension test_pg_dump\n")

    all_runs = set(pgdump_runs)

    # Run all runs, sorted by name.
    for run in sorted(pgdump_runs):
        spec = pgdump_runs[run]
        test_key = run

        # Skip command-level tests for gzip if there is no support for it.
        if spec.get("compile_option") == "gzip" and not supports_gzip:
            print(f"# {run}: skipped due to no gzip support")
            continue

        node.command_ok(spec["dump_cmd"], f"{run}: pg_dump runs")

        if "restore_cmd" in spec:
            node.command_ok(spec["restore_cmd"], f"{run}: pg_restore runs")

        if "test_key" in spec:
            test_key = spec["test_key"]

        output_file = slurp_file(os.path.join(tempdir, f"{run}.sql"))

        # Run all tests where this run is included as a 'like' or 'unlike'.
        for test_name in sorted(tests):
            tspec = tests[test_name]

            like = tspec["like"]
            unlike = tspec.get("unlike", set())

            # "ALL" means the regexp is expected to match every run.
            like_set = all_runs if like == "ALL" else like

            # Check for useless entries in "unlike": runs not in "like" do not
            # need excluding.
            assert not (
                test_key in unlike and test_key not in like_set
            ), f'useless "unlike" entry "{test_key}" in test "{test_name}"'

            if test_key in like_set and test_key not in unlike:
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
