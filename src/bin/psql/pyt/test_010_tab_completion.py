# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Exercise psql's readline/libedit tab-completion machinery.

Drives an interactive psql session through a pty (via the ``interactive_psql``
fixture in conftest.py) and checks, for a long list of inputs, that readline
completes the partial command/identifier/filename as expected.

This test only runs when the build is --with-readline (the ``with_readline``
environment variable is 'yes'), and is skipped when SKIP_READLINE_TESTS is set
or when pexpect is unavailable.
"""

import os
import re

import pytest


# ---------------------------------------------------------------------------
# Helpers: check_completion / clear_query / clear_line.
# These are deliberately NOT named test_* -- they are called by the single
# test function below.
# ---------------------------------------------------------------------------


def check_completion(psql, send, pattern, annotation):
    """Type *send* and check psql responds with output matching *pattern*.

    *pattern* is a compiled regex.  Send the data, wait for its result, and
    assert the captured output matches the pattern and that the query did not
    time out.
    """
    out = psql.query_until(pattern, send)
    assert pattern.search(out) and not psql.timed_out, (
        f"{annotation}\nActual output was {out!r}\n"
        f"Did not match {pattern.pattern!r}"
    )


def clear_query(psql):
    """Clear the query buffer to start over.

    (won't work if we are inside a string literal!)
    """
    check_completion(
        psql,
        "\\r\n",
        re.compile(r"Query buffer reset.*postgres=# ", re.DOTALL),
        "\\r works",
    )


def clear_line(psql):
    """Clear the current line to start over.

    (this will work in an incomplete string literal, but it's less desirable
    than clear_query because we lose evidence in the history file)
    """
    check_completion(psql, "\025\n", re.compile(r"postgres=# "), "control-U works")


# ---------------------------------------------------------------------------
# The test.
# ---------------------------------------------------------------------------


def test_010_tab_completion(pg, interactive_psql, tmp_path):
    # Do nothing unless the build is --with-readline.
    if os.environ.get("with_readline") != "yes":
        pytest.skip("readline is not supported by this build")

    # Also, skip if user has set environment variable to command that.  This is
    # mainly intended to allow working around some of the more broken versions
    # of libedit --- some users might find them acceptable even if they won't
    # pass these tests.
    if "SKIP_READLINE_TESTS" in os.environ:
        pytest.skip("SKIP_READLINE_TESTS is set")

    # (pexpect availability is handled by the interactive_psql fixture, which
    # issues pytest.importorskip("pexpect").)

    node = pg

    # set up a few database objects
    node.safe_sql(
        "CREATE TABLE tab1 (c1 int primary key constraint foo not null, c2 text);\n"
        "CREATE TABLE mytab123 (f1 int, f2 text);\n"
        "CREATE TABLE mytab246 (f1 int, f2 text);\n"
        'CREATE TABLE "mixedName" (f1 int, f2 text);\n'
        "CREATE TYPE enum1 AS ENUM ('foo', 'bar', 'baz', 'BLACK');\n"
        "CREATE PUBLICATION some_publication;\n"
        "CREATE TABLE fpo_test (id int4range, valid_at daterange, name text);\n"
    )

    # Use relative paths to access the tab_comp_dir subdirectory; otherwise the
    # output from filename completion tests is too variable.  We do this by
    # creating tab_comp_dir under tmp_path and chdir'ing there for the duration
    # of the test, restoring the cwd afterwards.
    saved_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        # Create some junk files for filename completion testing.
        os.mkdir("tab_comp_dir")
        with open("tab_comp_dir/somefile", "w", encoding="utf-8") as fh:
            fh.write("some stuff\n")
        with open("tab_comp_dir/afile123", "w", encoding="utf-8") as fh:
            fh.write("more stuff\n")
        with open("tab_comp_dir/afile456", "w", encoding="utf-8") as fh:
            fh.write("other stuff\n")

        # Arrange to capture, not discard, the interactive session's history
        # output.  We put it under tmp_path so buildfarm runs can still capture
        # it for debugging.
        historyfile = str(tmp_path / "010_psql_history.txt")

        # fire up an interactive psql session
        psql = interactive_psql(node, dbname="postgres", history_file=historyfile)

        _run_cases(psql)

        # send psql an explicit \q to shut it down, else pty won't close
        # properly
        psql.quit()
    finally:
        os.chdir(saved_cwd)


def _run_cases(psql):
    # check basic command completion: SEL<tab> produces SELECT<space>
    check_completion(
        psql, "SEL\t", re.compile(r"SELECT "), "complete SEL<tab> to SELECT"
    )

    clear_query(psql)

    # check case variation is honored
    check_completion(
        psql, "sel\t", re.compile(r"select "), "complete sel<tab> to select"
    )

    # check basic table name completion
    check_completion(
        psql, "* from t\t", re.compile(r"\* from tab1 "), "complete t<tab> to tab1"
    )

    clear_query(psql)

    # check table name completion with multiple alternatives
    # note: readline might print a bell before the completion
    check_completion(
        psql,
        "select * from my\t",
        re.compile(r"select \* from my\a?tab"),
        "complete my<tab> to mytab when there are multiple choices",
    )

    # some versions of readline/libedit require two tabs here, some only need
    # one
    check_completion(
        psql,
        "\t\t",
        re.compile(r"mytab123 +mytab246"),
        "offer multiple table choices",
    )

    check_completion(
        psql,
        "2\t",
        re.compile(r"246 "),
        "finish completion of one of multiple table choices",
    )

    clear_query(psql)

    # check handling of quoted names
    check_completion(
        psql,
        'select * from "my\t',
        re.compile(r'select \* from "my\a?tab'),
        'complete "my<tab> to "mytab when there are multiple choices',
    )

    check_completion(
        psql,
        "\t\t",
        re.compile(r'"mytab123" +"mytab246"'),
        "offer multiple quoted table choices",
    )

    check_completion(
        psql,
        "2\t",
        re.compile(r'246" '),
        "finish completion of one of multiple quoted table choices",
    )

    clear_query(psql)

    # check handling of mixed-case names
    check_completion(
        psql,
        'select * from "mi\t',
        re.compile(r'"mixedName" '),
        "complete a mixed-case name",
    )

    clear_query(psql)

    # check case folding
    check_completion(
        psql,
        "select * from TAB\t",
        re.compile(r"tab1 "),
        "automatically fold case",
    )

    clear_query(psql)

    # check case-sensitive keyword replacement
    # note: various versions of readline/libedit handle backspacing
    # differently, so just check that the replacement comes out correctly
    check_completion(
        psql, "\\DRD\t", re.compile(r"drds "), "complete \\DRD<tab> to \\drds"
    )

    clear_query(psql)

    # check completion of a schema-qualified name
    check_completion(
        psql,
        "select * from pub\t",
        re.compile(r"public\."),
        "complete schema when relevant",
    )

    check_completion(
        psql, "tab\t", re.compile(r"tab1 "), "complete schema-qualified name"
    )

    clear_query(psql)

    check_completion(
        psql,
        "select * from PUBLIC.t\t",
        re.compile(r"public\.tab1 "),
        "automatically fold case in schema-qualified name",
    )

    clear_query(psql)

    # check interpretation of referenced names
    check_completion(
        psql,
        "alter table tab1 drop constraint t\t",
        re.compile(r"tab1_pkey "),
        "complete index name for referenced table",
    )

    clear_query(psql)

    check_completion(
        psql,
        "alter table TAB1 drop constraint t\t",
        re.compile(r"tab1_pkey "),
        "complete index name for referenced table, with downcasing",
    )

    clear_query(psql)

    check_completion(
        psql,
        'alter table public."tab1" drop constraint t\t',
        re.compile(r"tab1_pkey "),
        "complete index name for referenced table, with schema and quoting",
    )

    clear_query(psql)

    # check variant where we're completing a qualified name from a refname
    # (this one also checks successful completion in a multiline command)
    check_completion(
        psql,
        "comment on constraint tab1_pkey \n on public.\t",
        re.compile(r"public\.tab1"),
        "complete qualified name from object reference",
    )

    clear_query(psql)

    # check filename completion
    check_completion(
        psql,
        "\\lo_import tab_comp_dir/some\t",
        re.compile(r"tab_comp_dir/somefile "),
        "filename completion with one possibility",
    )

    clear_query(psql)

    # note: readline might print a bell before the completion
    check_completion(
        psql,
        "\\lo_import tab_comp_dir/af\t",
        re.compile(r"tab_comp_dir/af\a?ile"),
        "filename completion with multiple possibilities",
    )

    # here we are inside a string literal 'afile*', so must use clear_line().
    clear_line(psql)

    # COPY requires quoting
    check_completion(
        psql,
        "COPY foo FROM tab_comp_dir/some\t",
        re.compile(r"'tab_comp_dir/somefile' "),
        "quoted filename completion with one possibility",
    )

    clear_query(psql)

    check_completion(
        psql,
        "COPY foo FROM tab_comp_dir/af\t",
        re.compile(r"'tab_comp_dir/afile"),
        "quoted filename completion with multiple possibilities",
    )

    # some versions of readline/libedit require two tabs here, some only need
    # one
    # also, some will offer the whole path name and some just the file name
    # the quotes might appear, too
    check_completion(
        psql,
        "\t\t",
        re.compile(r"afile123'? +'?(tab_comp_dir/)?afile456"),
        "offer multiple file choices",
    )

    clear_line(psql)

    # check enum label completion
    # some versions of readline/libedit require two tabs here, some only need
    # one
    # also, some versions will offer quotes, some will not
    check_completion(
        psql,
        "ALTER TYPE enum1 RENAME VALUE 'ba\t\t",
        re.compile(r"'?bar'? +'?baz'?"),
        "offer multiple enum choices",
    )

    clear_line(psql)

    # enum labels are case sensitive, so this should complete BLACK immediately
    check_completion(
        psql,
        "ALTER TYPE enum1 RENAME VALUE 'B\t",
        re.compile(r"BLACK"),
        "enum labels are case sensitive",
    )

    clear_line(psql)

    # check timezone name completion
    check_completion(
        psql,
        "SET timezone TO am\t",
        re.compile(r"'America/"),
        "offer partial timezone name",
    )

    check_completion(
        psql, "new_\t", re.compile(r"New_York"), "complete partial timezone name"
    )

    clear_line(psql)

    # check completion of a keyword offered in addition to object names;
    # such a keyword should obey COMP_KEYWORD_CASE
    for case, in_, out in (
        ("lower", "CO", "column"),
        ("upper", "co", "COLUMN"),
        ("preserve-lower", "co", "column"),
        ("preserve-upper", "CO", "COLUMN"),
    ):
        check_completion(
            psql,
            f"\\set COMP_KEYWORD_CASE {case}\n",
            re.compile(r"postgres=#"),
            f"set completion case to '{case}'",
        )
        check_completion(
            psql,
            f"alter table tab1 rename {in_}\t\t\t",
            re.compile(out),
            f"offer keyword {out} for input {in_}<TAB>, " f"COMP_KEYWORD_CASE = {case}",
        )
        clear_query(psql)

    # alternate path where keyword comes from SchemaQuery
    check_completion(
        psql,
        "DROP TYPE big\t",
        re.compile(r"DROP TYPE bigint "),
        "offer keyword from SchemaQuery",
    )

    clear_query(psql)

    # check create_command_generator
    check_completion(
        psql,
        "CREATE TY\t",
        re.compile(r"CREATE TYPE "),
        "check create_command_generator",
    )

    clear_query(psql)

    # check words_after_create infrastructure
    check_completion(
        psql,
        "CREATE TABLE mytab\t\t",
        re.compile(r"mytab123 +mytab246"),
        "check words_after_create",
    )

    clear_query(psql)

    # check VersionedQuery infrastructure
    check_completion(
        psql,
        "DROP PUBLIC\t \t\t",
        re.compile(r"DROP PUBLICATION\s+some_publication "),
        "check VersionedQuery",
    )

    clear_query(psql)

    # hits ends_with() and logic for completing in multi-line queries
    check_completion(
        psql,
        "analyze (\n\t\t",
        re.compile(r"VERBOSE"),
        "check ANALYZE (VERBOSE ...",
    )

    clear_query(psql)

    # check completions for GUCs
    check_completion(
        psql,
        "set interval\t\t",
        re.compile(r"intervalstyle TO"),
        "complete a GUC name",
    )
    check_completion(
        psql, " iso\t", re.compile(r"iso_8601 "), "complete a GUC enum value"
    )

    clear_query(psql)

    # same, for qualified GUC names
    check_completion(
        psql,
        "DO $$begin end$$ LANGUAGE plpgsql;\n",
        re.compile(r"postgres=# "),
        "load plpgsql extension",
    )

    check_completion(
        psql,
        "set plpg\t",
        re.compile(r"plpg\a?sql\."),
        "complete prefix of a GUC name",
    )
    check_completion(
        psql,
        "var\t\t",
        re.compile(r"variable_conflict TO"),
        "complete a qualified GUC name",
    )
    check_completion(
        psql,
        " USE_C\t",
        re.compile(r"use_column"),
        "complete a qualified GUC enum value",
    )

    clear_query(psql)

    # check completions for psql variables
    check_completion(
        psql,
        "\\set VERB\t",
        re.compile(r"VERBOSITY "),
        "complete a psql variable name",
    )
    check_completion(
        psql, "def\t", re.compile(r"default "), "complete a psql variable value"
    )

    clear_query(psql)

    check_completion(
        psql,
        "\\echo :VERB\t",
        re.compile(r":VERBOSITY "),
        "complete an interpolated psql variable name",
    )

    clear_query(psql)

    # check completion for psql variable test
    check_completion(
        psql,
        "\\echo :{?VERB\t",
        re.compile(r":\{\?VERBOSITY} "),
        "complete a psql variable test",
    )

    clear_query(psql)

    # check no-completions code path
    check_completion(
        psql, "blarg \t\t", re.compile(r""), "check completion failure path"
    )

    clear_query(psql)

    # check COPY FROM with DEFAULT option
    check_completion(
        psql,
        "COPY foo FROM stdin WITH ( DEF\t)",
        re.compile(r"DEFAULT "),
        "COPY FROM with DEFAULT completion",
    )

    clear_line(psql)

    # check tab completion for DELETE ... FOR PORTION OF
    check_completion(
        psql,
        "DELETE FROM fpo_test F\t",
        re.compile(r"FOR "),
        "complete DELETE FROM <table> F<tab> to FOR",
    )

    check_completion(
        psql, "P\t", re.compile(r"PORTION "), "complete FOR P<tab> to PORTION"
    )

    check_completion(psql, "O\t", re.compile(r"OF "), "complete PORTION O<tab> to OF")

    check_completion(
        psql,
        "v\t",
        re.compile(r"valid_at "),
        "complete FOR PORTION OF offers column names",
    )

    check_completion(
        psql,
        "FR\t",
        re.compile(r"FROM "),
        "complete FOR PORTION OF <col> FR<tab> to FROM",
    )

    clear_query(psql)

    # check tab completion for UPDATE ... FOR PORTION OF
    check_completion(
        psql,
        "UPDATE fpo_test F\t",
        re.compile(r"FOR "),
        "complete UPDATE <table> F<tab> to FOR",
    )

    check_completion(
        psql, "P\t", re.compile(r"PORTION "), "complete FOR P<tab> to PORTION"
    )

    check_completion(psql, "O\t", re.compile(r"OF "), "complete PORTION O<tab> to OF")

    check_completion(
        psql,
        "v\t",
        re.compile(r"valid_at "),
        "complete FOR PORTION OF offers column names",
    )

    check_completion(
        psql,
        "FR\t",
        re.compile(r"FROM "),
        "complete FOR PORTION OF <col> FR<tab> to FROM",
    )

    clear_query(psql)
