# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Tests pg_dump / pg_dumpall / pg_restore --filter=FILE handling.

Tests pg_dump / pg_dumpall / pg_restore --filter=FILE: writes filter files
containing include/exclude directives (for tables, schemas, foreign data,
functions, etc.), runs the tools, and checks both the dump output and the
error cases for malformed filter files.

pg_dump / pg_dumpall / pg_restore are the binaries under test and are run as
subprocesses through the node's pg_bin (PGHOST/PGPORT point at the server).
The seed SQL the test itself runs is executed in-process via safe_sql.
"""

import os
import re

from pypg.util import slurp_file


def _write_filter(path, content):
    """Write *content* verbatim to the filter file at *path*."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _seed(node):
    """Create the test objects used by the filter-file cases."""
    node.safe_sql("CREATE FOREIGN DATA WRAPPER dummy;")
    node.safe_sql("CREATE SERVER dummyserver FOREIGN DATA WRAPPER dummy;")

    node.safe_sql("CREATE TABLE table_one(a varchar)")
    node.safe_sql("CREATE TABLE table_two(a varchar)")
    node.safe_sql("CREATE TABLE table_three(a varchar)")
    node.safe_sql("CREATE TABLE table_three_one(a varchar)")
    node.safe_sql("CREATE TABLE footab(a varchar)")
    node.safe_sql("CREATE TABLE bootab() inherits (footab)")
    node.safe_sql('CREATE TABLE "strange aaa\nname"(a varchar)')
    node.safe_sql('CREATE TABLE "\nt\nt\n"(a int)')

    node.safe_sql("INSERT INTO table_one VALUES('*** TABLE ONE ***')")
    node.safe_sql("INSERT INTO table_two VALUES('*** TABLE TWO ***')")
    node.safe_sql("INSERT INTO table_three VALUES('*** TABLE THREE ***')")
    node.safe_sql("INSERT INTO table_three_one VALUES('*** TABLE THREE_ONE ***')")
    node.safe_sql("INSERT INTO bootab VALUES(10)")

    node.safe_sql("CREATE DATABASE sourcedb")
    node.safe_sql("CREATE DATABASE targetdb")

    node.safe_sql(
        "CREATE FUNCTION foo1(a int) RETURNS int AS $$ select $1 $$ LANGUAGE sql",
        "sourcedb",
    )
    node.safe_sql(
        "CREATE FUNCTION foo2(a int) RETURNS int AS $$ select $1 $$ LANGUAGE sql",
        "sourcedb",
    )
    node.safe_sql(
        "CREATE FUNCTION foo3(a double precision, b int) RETURNS "
        "double precision AS $$ select $1 + $2 $$ LANGUAGE sql",
        "sourcedb",
    )
    node.safe_sql(
        "CREATE FUNCTION foo_trg() RETURNS trigger AS $$ BEGIN RETURN NEW; "
        "END $$ LANGUAGE plpgsql",
        "sourcedb",
    )
    node.safe_sql("CREATE SCHEMA s1", "sourcedb")
    node.safe_sql("CREATE SCHEMA s2", "sourcedb")
    node.safe_sql("CREATE TABLE s1.t1(a int)", "sourcedb")
    node.safe_sql("CREATE SEQUENCE s1.s1", "sourcedb")
    node.safe_sql("CREATE TABLE s2.t2(a int)", "sourcedb")
    node.safe_sql("CREATE TABLE t1(a int, b int)", "sourcedb")
    node.safe_sql("CREATE TABLE t2(a int, b int)", "sourcedb")
    node.safe_sql("CREATE INDEX t1_idx1 ON t1(a)", "sourcedb")
    node.safe_sql("CREATE INDEX t1_idx2 ON t1(b)", "sourcedb")
    node.safe_sql(
        "CREATE TRIGGER trg1 BEFORE INSERT ON t1 EXECUTE FUNCTION foo_trg()",
        "sourcedb",
    )
    node.safe_sql(
        "CREATE TRIGGER trg2 BEFORE INSERT ON t1 EXECUTE FUNCTION foo_trg()",
        "sourcedb",
    )


def test_pg_dump_filterfile(pg, tmp_path):
    node = pg
    port = node.port
    tempdir = str(tmp_path)
    inputfile = os.path.join(tempdir, "inputfile.txt")
    inputfile2 = os.path.join(tempdir, "inputfile2.txt")
    plainfile = os.path.join(tempdir, "plain.sql")
    dumpfile = os.path.join(tempdir, "filter_test.dump")

    _seed(node)

    #
    # Test interaction of correctly specified filter file
    #

    # Empty filterfile
    _write_filter(inputfile, "\n # a comment and nothing more\n\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "filter file without patterns",
    )

    dump = slurp_file(plainfile)
    assert re.search(r"^CREATE TABLE public\.table_one", dump, re.M), "table one dumped"
    assert re.search(r"^CREATE TABLE public\.table_two", dump, re.M), "table two dumped"
    assert re.search(
        r"^CREATE TABLE public\.table_three", dump, re.M
    ), "table three dumped"
    assert re.search(
        r"^CREATE TABLE public\.table_three_one", dump, re.M
    ), "table three one dumped"

    # Test various combinations of whitespace, comments and correct filters
    _write_filter(
        inputfile,
        "  include   table table_one    #comment\n"
        "include table table_two\n"
        "# skip this line\n"
        "\n"
        "\t\n"
        "  \t# another comment\n"
        "exclude table_data table_one\n",
    )

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "dump tables with filter patterns as well as comments and whitespace",
    )

    dump = slurp_file(plainfile)
    assert re.search(r"^CREATE TABLE public\.table_one", dump, re.M), "dumped table one"
    assert re.search(r"^CREATE TABLE public\.table_two", dump, re.M), "dumped table two"
    assert not re.search(
        r"^CREATE TABLE public\.table_three", dump, re.M
    ), "table three not dumped"
    assert not re.search(
        r"^CREATE TABLE public\.table_three_one", dump, re.M
    ), "table three_one not dumped"
    assert not re.search(
        r"^COPY public\.table_one", dump, re.M
    ), "content of table one is not included"
    assert re.search(
        r"^COPY public\.table_two", dump, re.M
    ), "content of table two is included"

    # Test dumping tables specified by qualified names
    _write_filter(
        inputfile,
        "include table public.table_one\n"
        'include table "public"."table_two"\n'
        'include table "public". table_three\n',
    )

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "filter file without patterns",
    )

    dump = slurp_file(plainfile)
    assert re.search(r"^CREATE TABLE public\.table_one", dump, re.M), "dumped table one"
    assert re.search(r"^CREATE TABLE public\.table_two", dump, re.M), "dumped table two"
    assert re.search(
        r"^CREATE TABLE public\.table_three", dump, re.M
    ), "dumped table three"

    # Test dumping all tables except one
    _write_filter(inputfile, "exclude table table_one\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "dump tables with exclusion of a single table",
    )

    dump = slurp_file(plainfile)
    assert not re.search(
        r"^CREATE TABLE public\.table_one", dump, re.M
    ), "table one not dumped"
    assert re.search(r"^CREATE TABLE public\.table_two", dump, re.M), "dumped table two"
    assert re.search(
        r"^CREATE TABLE public\.table_three", dump, re.M
    ), "dumped table three"
    assert re.search(
        r"^CREATE TABLE public\.table_three_one", dump, re.M
    ), "dumped table three_one"

    # Test dumping tables with a wildcard pattern
    _write_filter(inputfile, "include table table_thre*\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "dump tables with wildcard in pattern",
    )

    dump = slurp_file(plainfile)
    assert not re.search(
        r"^CREATE TABLE public\.table_one", dump, re.M
    ), "table one not dumped"
    assert not re.search(
        r"^CREATE TABLE public\.table_two", dump, re.M
    ), "table two not dumped"
    assert re.search(
        r"^CREATE TABLE public\.table_three", dump, re.M
    ), "dumped table three"
    assert re.search(
        r"^CREATE TABLE public\.table_three_one", dump, re.M
    ), "dumped table three_one"

    # Test dumping table with multiline quoted tablename
    _write_filter(inputfile, 'include table "strange aaa\nname"')

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "dump tables with multiline names requiring quoting",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE TABLE public.\"strange aaa", dump, re.M
    ), "dump table with new line in name"

    # Test excluding multiline quoted tablename from dump
    _write_filter(inputfile, 'exclude table "strange aaa\\nname"')

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "dump tables with filter",
    )

    dump = slurp_file(plainfile)
    assert not re.search(
        r"^CREATE TABLE public.\"strange aaa", dump, re.M
    ), "dump table with new line in name"

    # Test excluding an entire schema
    _write_filter(inputfile, "exclude schema public\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "exclude the public schema",
    )

    dump = slurp_file(plainfile)
    assert not re.search(r"^CREATE TABLE", dump, re.M), "no table dumped"

    # Test including and excluding an entire schema by multiple filterfiles
    _write_filter(inputfile, "include schema public\n")
    _write_filter(inputfile2, "exclude schema public\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--filter",
            inputfile2,
            "postgres",
        ],
        "exclude the public schema with multiple filters",
    )

    dump = slurp_file(plainfile)
    assert not re.search(r"^CREATE TABLE", dump, re.M), "no table dumped"

    # Test dumping a table with a single leading newline on a row
    _write_filter(inputfile, 'include table "\nt\nt\n"')

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "dump tables with filter",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE TABLE public.\"\nt\nt\n\" \($", dump, re.M | re.S
    ), "dump table with multiline strange name"

    _write_filter(inputfile, 'include table "\\nt\\nt\\n"')

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "dump tables with filter",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE TABLE public.\"\nt\nt\n\" \($", dump, re.M | re.S
    ), "dump table with multiline strange name"

    #########################################
    # Test foreign_data

    _write_filter(inputfile, "include foreign_data doesnt_exists\n")

    node.command_fails_like(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        r"pg_dump: error: no matching foreign servers were found for pattern",
        "dump nonexisting foreign server",
    )

    _write_filter(inputfile, "include foreign_data dummyserver\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "dump foreign_data with filter",
    )

    dump = slurp_file(plainfile)
    assert re.search(r"^CREATE SERVER dummyserver", dump, re.M), "dump foreign server"

    _write_filter(inputfile, "exclude foreign_data dummy*\n")

    node.command_fails_like(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        r'exclude filter for "foreign data" is not allowed',
        "erroneously exclude foreign server",
    )

    #########################################
    # Test broken input format

    # Test invalid filter command
    _write_filter(inputfile, "k")

    node.command_fails_like(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        r"invalid filter command",
        "invalid syntax: incorrect filter command",
    )

    # Test invalid object type.
    #
    # This test also verifies that keywords are correctly recognized as
    # strings of non-whitespace characters.  "table-data" is used here as an
    # intentionally invalid object type.
    _write_filter(inputfile, "exclude table-data one")

    node.command_fails_like(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        r'unsupported filter object type: "table-data"',
        "invalid syntax: invalid object type specified",
    )

    # Test missing object identifier pattern
    _write_filter(inputfile, "include table")

    node.command_fails_like(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        r"missing object name",
        "invalid syntax: missing object identifier pattern",
    )

    # Test adding extra content after the object identifier pattern
    _write_filter(inputfile, "include table table one")

    node.command_fails_like(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        r"no matching tables were found",
        "invalid syntax: extra content after object identifier pattern",
    )

    #########################################
    # Combined with --strict-names

    # First ensure that a matching filter works
    _write_filter(inputfile, "include table table_one\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--strict-names",
            "postgres",
        ],
        "strict names with matching pattern",
    )

    dump = slurp_file(plainfile)
    assert re.search(r"^CREATE TABLE public\.table_one", dump, re.M), "no table dumped"

    # Now append a pattern to the filter file which doesn't resolve
    with open(inputfile, "a", encoding="utf-8") as fh:
        fh.write("include table table_nonexisting_name")

    node.command_fails_like(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--strict-names",
            "postgres",
        ],
        r"no matching tables were found",
        "inclusion of non-existing objects with --strict names",
    )

    #########################################
    # pg_dumpall tests

    ###########################
    # Test dumping all tables except one
    _write_filter(inputfile, "exclude database postgres\n")

    node.command_ok(
        ["pg_dumpall", "--port", str(port), "--file", plainfile, "--filter", inputfile],
        "dump tables with exclusion of a database",
    )

    dump = slurp_file(plainfile)
    assert not re.search(
        r"^\\connect postgres", dump, re.M
    ), "database postgres is not dumped"
    assert re.search(
        r"^\\connect template1", dump, re.M
    ), "database template1 is dumped"

    # Make sure this option doesn't break the existing limitation of using
    # --globals-only with exclusions
    node.command_fails_like(
        [
            "pg_dumpall",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--globals-only",
        ],
        re.escape(
            "pg_dumpall: error: options --exclude-database and "
            "-g/--globals-only cannot be used together"
        ),
        "pg_dumpall: options --exclude-database and -g/--globals-only "
        "cannot be used together",
    )

    # Test invalid filter command
    _write_filter(inputfile, "k")

    node.command_fails_like(
        ["pg_dumpall", "--port", str(port), "--file", plainfile, "--filter", inputfile],
        r"invalid filter command",
        "invalid syntax: incorrect filter command",
    )

    # Test invalid object type
    _write_filter(inputfile, "exclude xxx")

    node.command_fails_like(
        ["pg_dumpall", "--port", str(port), "--file", plainfile, "--filter", inputfile],
        r'unsupported filter object type: "xxx"',
        "invalid syntax: exclusion of non-existing object type",
    )

    _write_filter(inputfile, "exclude table foo")

    node.command_fails_like(
        ["pg_dumpall", "--port", str(port), "--file", plainfile, "--filter", inputfile],
        r"pg_dumpall: error: invalid format in filter",
        "invalid syntax: exclusion of unsupported object type",
    )

    #########################################
    # pg_restore tests

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            dumpfile,
            "--format",
            "custom",
            "postgres",
        ],
        "dump all tables",
    )

    _write_filter(inputfile, "include table table_two")

    node.command_ok(
        [
            "pg_restore",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--format",
            "custom",
            dumpfile,
        ],
        "restore tables with filter",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE TABLE public\.table_two", dump, re.M
    ), "wanted table restored"
    assert not re.search(
        r"^CREATE TABLE public\.table_one", dump, re.M
    ), "unwanted table is not restored"

    _write_filter(inputfile, "include table_data xxx")

    node.command_fails_like(
        ["pg_restore", "--port", str(port), "--file", plainfile, "--filter", inputfile],
        r'include filter for "table data" is not allowed',
        "invalid syntax: inclusion of unallowed object",
    )

    _write_filter(inputfile, "include extension xxx")

    node.command_fails_like(
        ["pg_restore", "--port", str(port), "--file", plainfile, "--filter", inputfile],
        r'include filter for "extension" is not allowed',
        "invalid syntax: inclusion of unallowed object",
    )

    _write_filter(inputfile, "exclude extension xxx")

    node.command_fails_like(
        ["pg_restore", "--port", str(port), "--file", plainfile, "--filter", inputfile],
        r'exclude filter for "extension" is not allowed',
        "invalid syntax: exclusion of unallowed object",
    )

    _write_filter(inputfile, "exclude table_data xxx")

    node.command_fails_like(
        ["pg_restore", "--port", str(port), "--file", plainfile, "--filter", inputfile],
        r'exclude filter for "table data" is not allowed',
        "invalid syntax: exclusion of unallowed object",
    )

    #########################################
    # test restore of other objects

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            dumpfile,
            "--format",
            "custom",
            "sourcedb",
        ],
        "dump all objects from sourcedb",
    )

    _write_filter(inputfile, "include function foo1(integer)")

    node.command_ok(
        [
            "pg_restore",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--format",
            "custom",
            dumpfile,
        ],
        "restore function with filter",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE FUNCTION public\.foo1", dump, re.M
    ), "wanted function restored"
    assert not re.search(
        r"^CREATE TABLE public\.foo2", dump, re.M
    ), "unwanted function is not restored"

    # this should be white space tolerant (against the -P argument)
    _write_filter(
        inputfile, "include function  foo3 ( double  precision ,   integer)  "
    )

    node.command_ok(
        [
            "pg_restore",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--format",
            "custom",
            dumpfile,
        ],
        "restore function with filter",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE FUNCTION public\.foo3", dump, re.M
    ), "wanted function restored"

    # attention! this hit pg_restore bug - correct name of trigger is "trg1"
    # not "t1 trg1".  Should be fixed when pg_restore will be fixed
    _write_filter(
        inputfile,
        "include index t1_idx1\ninclude trigger t1 trg1\n",
    )

    node.command_ok(
        [
            "pg_restore",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--format",
            "custom",
            dumpfile,
        ],
        "restore function with filter",
    )

    dump = slurp_file(plainfile)
    assert re.search(r"^CREATE INDEX t1_idx1", dump, re.M), "wanted index restored"
    assert not re.search(
        r"^CREATE INDEX t2_idx2", dump, re.M
    ), "unwanted index are not restored"
    assert re.search(r"^CREATE TRIGGER trg1", dump, re.M), "wanted trigger restored"
    assert not re.search(
        r"^CREATE TRIGGER trg2", dump, re.M
    ), "unwanted trigger is not restored"

    _write_filter(inputfile, "include schema s1\n")

    node.command_ok(
        [
            "pg_restore",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--format",
            "custom",
            dumpfile,
        ],
        "restore function with filter",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE TABLE s1\.t1", dump, re.M
    ), "wanted table from schema restored"
    assert re.search(
        r"^CREATE SEQUENCE s1\.s1", dump, re.M
    ), "wanted sequence from schema restored"
    assert not re.search(
        r"^CREATE TABLE s2\t2", dump, re.M
    ), "unwanted table is not restored"

    _write_filter(inputfile, "exclude schema s1\n")

    node.command_ok(
        [
            "pg_restore",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "--format",
            "custom",
            dumpfile,
        ],
        "restore function with filter",
    )

    dump = slurp_file(plainfile)
    assert not re.search(
        r"^CREATE TABLE s1\.t1", dump, re.M
    ), "unwanted table from schema is not restored"
    assert not re.search(
        r"^CREATE SEQUENCE s1\.s1", dump, re.M
    ), "unwanted sequence from schema is not restored"
    assert re.search(r"^CREATE TABLE s2\.t2", dump, re.M), "wanted table restored"
    assert re.search(r"^CREATE TABLE public\.t1", dump, re.M), "wanted table restored"

    #########################################
    # test of supported syntax

    _write_filter(inputfile, "include table_and_children footab\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "filter file without patterns",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE TABLE public\.bootab", dump, re.M
    ), "dumped children table"

    _write_filter(inputfile, "exclude table_and_children footab\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "filter file without patterns",
    )

    dump = slurp_file(plainfile)
    assert not re.search(
        r"^CREATE TABLE public\.bootab", dump, re.M
    ), "exclude dumped children table"

    _write_filter(inputfile, "exclude table_data_and_children footab\n")

    node.command_ok(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        "filter file without patterns",
    )

    dump = slurp_file(plainfile)
    assert re.search(
        r"^CREATE TABLE public\.bootab", dump, re.M
    ), "dumped children table"
    assert not re.search(
        r"^COPY public\.bootab", dump, re.M
    ), "exclude dumped children table"

    #########################################
    # Test extension

    _write_filter(inputfile, "include extension doesnt_exists\n")

    node.command_fails_like(
        [
            "pg_dump",
            "--port",
            str(port),
            "--file",
            plainfile,
            "--filter",
            inputfile,
            "postgres",
        ],
        r"pg_dump: error: no matching extensions were found",
        "dump nonexisting extension",
    )
