# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for clusterdb option handling, the SQL it issues, and error cases."""

import re


def test_clusterdb(pg_bin, create_pg):
    pg_bin.program_help_ok("clusterdb")
    pg_bin.program_version_ok("clusterdb")
    pg_bin.program_options_handling_ok("clusterdb")

    node = create_pg("main", start=False)
    # issues_sql_like needs the SQL logged on the server side.
    node.append_conf(
        "log_statement = 'all'\n"
        "log_min_messages = 'debug1'\n"
        "log_min_duration_statement = -1\n"
    )
    node.start()

    node.issues_sql_like(
        ["clusterdb"],
        re.compile(r"statement: CLUSTER;"),
        "SQL CLUSTER run",
    )

    node.command_fails_like(
        ["clusterdb", "--table", "nonexistent"],
        re.compile(r'relation "nonexistent" does not exist'),
        "fails with nonexistent table",
    )

    node.safe_sql(
        "CREATE TABLE test1 (a int); CREATE INDEX test1x ON test1 (a); "
        "CLUSTER test1 USING test1x"
    )
    node.issues_sql_like(
        ["clusterdb", "--table", "test1"],
        re.compile(r"statement: CLUSTER public\.test1;"),
        "cluster specific table",
    )

    node.command_ok(
        ["clusterdb", "--echo", "--verbose", "dbname=template1"],
        "clusterdb with connection string",
    )
