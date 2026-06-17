# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for clusterdb --all across multiple databases."""

import re


def test_clusterdb_all(create_pg):
    node = create_pg("main", start=False)
    node.append_conf(
        "log_statement = 'all'\n"
        "log_min_messages = 'debug1'\n"
        "log_min_duration_statement = -1\n"
    )
    node.start()

    # clusterdb -a is not compatible with -d.  This relies on PGDATABASE to be
    # set, something the test framework does (via the node's environment).
    node.issues_sql_like(
        ["clusterdb", "--all"],
        re.compile(r"statement: CLUSTER.*statement: CLUSTER", re.S),
        "cluster all databases",
    )

    node.safe_sql("CREATE DATABASE regression_invalid")
    node.safe_sql(
        "UPDATE pg_database SET datconnlimit = -2 "
        "WHERE datname = 'regression_invalid'"
    )
    node.command_ok(
        ["clusterdb", "--all"],
        "invalid database not targeted by clusterdb -a",
    )

    # Doesn't quite belong here, but don't want to waste time by creating an
    # invalid database in the non-all clusterdb test as well.
    node.command_fails_like(
        ["clusterdb", "--dbname", "regression_invalid"],
        re.compile(r'FATAL:  cannot connect to invalid database "regression_invalid"'),
        "clusterdb cannot target invalid database",
    )

    node.safe_sql(
        "CREATE TABLE test1 (a int); CREATE INDEX test1x ON test1 (a); "
        "CLUSTER test1 USING test1x"
    )
    node.safe_sql(
        "CREATE TABLE test1 (a int); CREATE INDEX test1x ON test1 (a); "
        "CLUSTER test1 USING test1x",
        dbname="template1",
    )
    node.issues_sql_like(
        ["clusterdb", "--all", "--table", "test1"],
        re.compile(r"statement: CLUSTER public\.test1", re.S),
        "cluster specific table in all databases",
    )
