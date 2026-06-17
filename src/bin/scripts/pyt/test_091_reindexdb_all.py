# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for reindexdb --all across multiple databases."""

import re


def test_reindexdb_all(create_pg, monkeypatch):
    node = create_pg("main", start=False)
    # issues_sql_like needs the statements logged.
    node.append_conf("log_statement = 'all'\nlog_min_duration_statement = -1")
    node.start()

    monkeypatch.setenv("PGOPTIONS", "--client-min-messages=WARNING")

    node.safe_sql("CREATE TABLE test1 (a int); CREATE INDEX test1x ON test1 (a);")
    # Use a transient connection for template1 so it is not left connected
    # when CREATE DATABASE (which copies template1) runs below.
    sess = node.connect(dbname="template1")
    try:
        sess.query_safe("CREATE TABLE test1 (a int); CREATE INDEX test1x ON test1 (a);")
    finally:
        sess.close()
    node.issues_sql_like(
        ["reindexdb", "--all"],
        re.compile(r"statement: REINDEX.*statement: REINDEX", re.S),
        "reindex all databases",
    )
    node.issues_sql_like(
        ["reindexdb", "--all", "--system"],
        re.compile(r"statement: REINDEX SYSTEM postgres", re.S),
        "reindex system catalogs in all databases",
    )
    node.issues_sql_like(
        ["reindexdb", "--all", "--schema", "public"],
        re.compile(r"statement: REINDEX SCHEMA public", re.S),
        "reindex schema in all databases",
    )
    node.issues_sql_like(
        ["reindexdb", "--all", "--index", "test1x"],
        re.compile(r"statement: REINDEX INDEX public\.test1x", re.S),
        "reindex index in all databases",
    )
    node.issues_sql_like(
        ["reindexdb", "--all", "--table", "test1"],
        re.compile(r"statement: REINDEX TABLE public\.test1", re.S),
        "reindex table in all databases",
    )

    node.safe_sql("CREATE DATABASE regression_invalid")
    node.safe_sql(
        "UPDATE pg_database SET datconnlimit = -2 "
        "WHERE datname = 'regression_invalid'"
    )
    node.command_ok(
        ["reindexdb", "--all"],
        "invalid database not targeted by reindexdb --all",
    )

    # Doesn't quite belong here, but don't want to waste time by creating an
    # invalid database in the non-all reindexdb test as well.
    node.command_fails_like(
        ["reindexdb", "--dbname", "regression_invalid"],
        re.compile(r'FATAL:  cannot connect to invalid database "regression_invalid"'),
        "reindexdb cannot target invalid database",
    )
