# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for dropdb option handling, the SQL it issues, and error cases."""

import re


def test_dropdb(pg_bin, create_pg):
    pg_bin.program_help_ok("dropdb")
    pg_bin.program_version_ok("dropdb")
    pg_bin.program_options_handling_ok("dropdb")

    node = create_pg("main")
    # issues_sql_like needs the statements logged.
    node.append_conf("log_statement = 'all'\nlog_min_duration_statement = -1")
    node.restart()

    node.safe_sql("CREATE DATABASE foobar1")
    node.issues_sql_like(
        ["dropdb", "foobar1"],
        re.compile(r"statement: DROP DATABASE foobar1"),
        "SQL DROP DATABASE run",
    )

    node.safe_sql("CREATE DATABASE foobar2")
    node.issues_sql_like(
        ["dropdb", "--force", "foobar2"],
        re.compile(r"statement: DROP DATABASE foobar2 WITH \(FORCE\);"),
        "SQL DROP DATABASE (FORCE) run",
    )

    node.command_fails_like(
        ["dropdb", "nonexistent"],
        re.compile(r'database "nonexistent" does not exist'),
        "fails with nonexistent database",
    )

    # check that invalid database can be dropped with dropdb
    node.safe_sql("CREATE DATABASE regression_invalid")
    node.safe_sql(
        "UPDATE pg_database SET datconnlimit = -2 "
        "WHERE datname = 'regression_invalid'"
    )
    node.command_ok(
        ["dropdb", "regression_invalid"],
        "invalid database can be dropped",
    )
