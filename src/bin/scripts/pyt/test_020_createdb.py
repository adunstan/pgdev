# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for createdb option handling, templates, locales, and error cases."""

import os
import re


def _with_icu(node):
    """Return True if this build has ICU support.

    Honor the with_icu environment variable (set by the build harness) if
    present; otherwise detect ICU at runtime by checking for ICU-provider
    collations in the catalog, which only exist when the server was compiled
    with ICU.
    """
    env = os.environ.get("with_icu")
    if env is not None:
        return env == "yes"
    return (
        node.safe_sql("SELECT count(*) > 0 FROM pg_collation WHERE collprovider = 'i'")
        == "t"
    )


def test_createdb(pg_bin, create_pg):
    pg_bin.program_help_ok("createdb")
    pg_bin.program_version_ok("createdb")
    pg_bin.program_options_handling_ok("createdb")

    node = create_pg("main", start=False)
    node.append_conf(
        "log_statement = 'all'\n"
        "log_min_messages = 'debug1'\n"
        "log_min_duration_statement = -1\n"
    )
    node.start()

    node.issues_sql_like(
        ["createdb", "foobar1"],
        re.compile(r"statement: CREATE DATABASE foobar1"),
        "SQL CREATE DATABASE run",
    )
    node.issues_sql_like(
        [
            "createdb",
            "--locale",
            "C",
            "--encoding",
            "LATIN1",
            "--template",
            "template0",
            "foobar2",
        ],
        re.compile(r"statement: CREATE DATABASE foobar2 ENCODING 'LATIN1'"),
        "create database with encoding",
    )

    if _with_icu(node):
        # This fails because template0 uses libc provider and has no ICU
        # locale set.  It would succeed if template0 used the icu provider.
        node.command_fails(
            [
                "createdb",
                "--template",
                "template0",
                "--encoding",
                "UTF8",
                "--locale-provider",
                "icu",
                "foobar4",
            ],
            "create database with ICU fails without ICU locale specified",
        )

        node.issues_sql_like(
            [
                "createdb",
                "--template",
                "template0",
                "--encoding",
                "UTF8",
                "--locale-provider",
                "icu",
                "--locale",
                "C",
                "--icu-locale",
                "en",
                "foobar5",
            ],
            re.compile(
                r"statement: CREATE DATABASE foobar5 .* "
                r"LOCALE_PROVIDER icu ICU_LOCALE 'en'"
            ),
            "create database with ICU locale specified",
        )

        node.command_fails(
            [
                "createdb",
                "--template",
                "template0",
                "--encoding",
                "UTF8",
                "--locale-provider",
                "icu",
                "--icu-locale",
                "@colNumeric=lower",
                "foobarX",
            ],
            "fails for invalid ICU locale",
        )

        node.command_fails_like(
            [
                "createdb",
                "--template",
                "template0",
                "--locale-provider",
                "icu",
                "--encoding",
                "SQL_ASCII",
                "foobarX",
            ],
            re.compile(
                r'ERROR:  encoding "SQL_ASCII" is not supported with ICU provider'
            ),
            "fails for encoding not supported by ICU",
        )

        # additional node, which uses the icu provider
        node2 = create_pg(
            "icu",
            start=False,
            initdb_extra=["--locale-provider=icu", "--icu-locale=en"],
        )
        node2.start()

        node2.command_ok(
            [
                "createdb",
                "--template",
                "template0",
                "--locale-provider",
                "libc",
                "foobar55",
            ],
            "create database with libc provider from template database "
            "with icu provider",
        )

        node2.command_ok(
            [
                "createdb",
                "--template",
                "template0",
                "--icu-locale",
                "en-US",
                "foobar56",
            ],
            "create database with icu locale from template database "
            "with icu provider",
        )

        node2.command_ok(
            [
                "createdb",
                "--template",
                "template0",
                "--locale-provider",
                "icu",
                "--locale",
                "en",
                "--lc-collate",
                "C",
                "--lc-ctype",
                "C",
                "foobar57",
            ],
            "create database with locale as ICU locale",
        )
    else:
        node.command_fails(
            [
                "createdb",
                "--template",
                "template0",
                "--locale-provider",
                "icu",
                "foobar4",
            ],
            "create database with ICU fails since no ICU support",
        )

    node.command_fails(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "tbuiltin1",
        ],
        'create database with provider "builtin" fails without --locale',
    )

    node.command_ok(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "tbuiltin2",
        ],
        'create database with provider "builtin" and locale "C"',
    )

    node.command_ok(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "--lc-collate",
            "C",
            "tbuiltin3",
        ],
        'create database with provider "builtin" and LC_COLLATE=C',
    )

    node.command_ok(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "--lc-ctype",
            "C",
            "tbuiltin4",
        ],
        'create database with provider "builtin" and LC_CTYPE=C',
    )

    node.command_ok(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--lc-collate",
            "C",
            "--lc-ctype",
            "C",
            "--encoding",
            "UTF-8",
            "--builtin-locale",
            "C.UTF8",
            "tbuiltin5",
        ],
        "create database with --builtin-locale C.UTF-8 and -E UTF-8",
    )

    node.command_fails(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--lc-collate",
            "C",
            "--lc-ctype",
            "C",
            "--encoding",
            "LATIN1",
            "--builtin-locale",
            "C.UTF-8",
            "tbuiltin6",
        ],
        "create database with --builtin-locale C.UTF-8 and -E LATIN1",
    )

    node.command_fails(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "--icu-locale",
            "en",
            "tbuiltin7",
        ],
        'create database with provider "builtin" and ICU_LOCALE="en"',
    )

    node.command_fails(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "--icu-rules",
            '""',
            "tbuiltin8",
        ],
        'create database with provider "builtin" and ICU_RULES=""',
    )

    node.command_fails(
        [
            "createdb",
            "--template",
            "template1",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "tbuiltin9",
        ],
        'create database with provider "builtin" not matching template',
    )

    node.command_fails(
        ["createdb", "foobar1"],
        "fails if database already exists",
    )

    node.command_fails(
        [
            "createdb",
            "--template",
            "template0",
            "--locale-provider",
            "xyz",
            "foobarX",
        ],
        "fails for invalid locale provider",
    )

    node.command_fails_like(
        ["createdb", "invalid \n dbname"],
        re.compile(r"contains a newline or carriage return character"),
        "fails if database name contains a newline character in name",
    )

    node.command_fails_like(
        ["createdb", "invalid \r dbname"],
        re.compile(r"contains a newline or carriage return character"),
        "fails if database name contains a carriage return character in name",
    )

    # Check use of templates with shared dependencies copied from the template.
    # Use fresh connections that are closed promptly: a lingering connection to
    # foobar2 would block its later use as a CREATE DATABASE template.
    with node.connect(dbname="foobar2") as foobar2:
        foobar2.query(
            "CREATE ROLE role_foobar;\n"
            "CREATE TABLE tab_foobar (id int);\n"
            "ALTER TABLE tab_foobar owner to role_foobar;\n"
            "CREATE POLICY pol_foobar ON tab_foobar FOR ALL TO role_foobar;"
        )
    node.issues_sql_like(
        ["createdb", "--locale", "C", "--template", "foobar2", "foobar3"],
        re.compile(r"statement: CREATE DATABASE foobar3 TEMPLATE foobar2 LOCALE 'C'"),
        "create database with template",
    )
    with node.connect(dbname="foobar3") as foobar3:
        stdout = foobar3.query_safe(
            "SELECT pg_describe_object(classid, objid, objsubid) AS obj,\n"
            "       pg_describe_object(refclassid, refobjid, 0) AS refobj\n"
            "   FROM pg_shdepend s JOIN pg_database d ON (d.oid = s.dbid)\n"
            "   WHERE d.datname = 'foobar3' ORDER BY obj;"
        )
    assert re.search(
        r"^policy pol_foobar on table tab_foobar\|role role_foobar\n"
        r"table tab_foobar\|role role_foobar$",
        stdout,
    ), f"shared dependencies copied over to target database\n{stdout}"

    # Check quote handling with incorrect option values.
    node.pg_bin.command_checks_all(
        ["createdb", "--encoding", "foo'; SELECT '1", "foobar2"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r"^createdb: error: \"foo'; SELECT '1\" is not a valid "
                r"encoding name",
                re.S,
            )
        ],
        "createdb with incorrect --encoding",
    )
    node.pg_bin.command_checks_all(
        ["createdb", "--lc-collate", "foo'; SELECT '1", "foobar2"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r"^createdb: error: database creation failed: ERROR:  invalid "
                r"LC_COLLATE locale name"
                r"|^createdb: error: database creation failed: ERROR:  new "
                r"collation \(foo'; SELECT '1\) is incompatible with the collation "
                r"of the template database",
                re.S,
            )
        ],
        "createdb with incorrect --lc-collate",
    )
    node.pg_bin.command_checks_all(
        ["createdb", "--lc-ctype", "foo'; SELECT '1", "foobar2"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r"^createdb: error: database creation failed: ERROR:  invalid "
                r"LC_CTYPE locale name"
                r"|^createdb: error: database creation failed: ERROR:  new "
                r"LC_CTYPE \(foo'; SELECT '1\) is incompatible with the LC_CTYPE "
                r"of the template database",
                re.S,
            )
        ],
        "createdb with incorrect --lc-ctype",
    )

    node.pg_bin.command_checks_all(
        ["createdb", "--strategy", "foo", "foobar2"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r"^createdb: error: database creation failed: ERROR:  invalid "
                r'create database strategy "foo"',
                re.S,
            )
        ],
        "createdb with incorrect --strategy",
    )

    # Check database creation strategy
    node.issues_sql_like(
        [
            "createdb",
            "--template",
            "foobar2",
            "--strategy",
            "wal_log",
            "foobar6",
        ],
        re.compile(
            "statement: CREATE DATABASE foobar6 STRATEGY wal_log TEMPLATE foobar2"
        ),
        "create database with WAL_LOG strategy",
    )

    node.issues_sql_like(
        [
            "createdb",
            "--template",
            "foobar2",
            "--strategy",
            "WAL_LOG",
            "foobar6s",
        ],
        re.compile(
            r'statement: CREATE DATABASE foobar6s STRATEGY "WAL_LOG" '
            r"TEMPLATE foobar2"
        ),
        "create database with WAL_LOG strategy",
    )

    node.issues_sql_like(
        [
            "createdb",
            "--template",
            "foobar2",
            "--strategy",
            "file_copy",
            "foobar7",
        ],
        re.compile(
            r"statement: CREATE DATABASE foobar7 STRATEGY file_copy "
            r"TEMPLATE foobar2"
        ),
        "create database with FILE_COPY strategy",
    )

    node.issues_sql_like(
        [
            "createdb",
            "--template",
            "foobar2",
            "--strategy",
            "FILE_COPY",
            "foobar7s",
        ],
        re.compile(
            r'statement: CREATE DATABASE foobar7s STRATEGY "FILE_COPY" '
            r"TEMPLATE foobar2"
        ),
        "create database with FILE_COPY strategy",
    )

    # Create database owned by role_foobar.
    node.issues_sql_like(
        [
            "createdb",
            "--template",
            "foobar2",
            "--owner",
            "role_foobar",
            "foobar8",
        ],
        re.compile(
            "statement: CREATE DATABASE foobar8 OWNER role_foobar TEMPLATE foobar2"
        ),
        "create database with owner role_foobar",
    )
    # query_safe raises on error, asserting the queries succeed.
    with node.connect(dbname="foobar2") as foobar2:
        foobar2.query_safe("DROP OWNED BY role_foobar;")
        foobar2.query_safe("DROP DATABASE foobar8;")
