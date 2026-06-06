# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for vacuumdb --all across multiple databases."""

import re

import pytest


@pytest.fixture
def node(create_pg):
    n = create_pg("main", start=False)
    n.append_conf("log_statement = 'all'\nlog_min_duration_statement = -1")
    n.start()
    return n


def test_vacuumdb_all(node):
    node.issues_sql_like(
        ["vacuumdb", "--all"],
        re.compile(r"statement: VACUUM.*statement: VACUUM", re.S),
        "vacuum all databases",
    )

    # CREATE DATABASE cannot run inside a transaction block with other
    # statements, so issue it separately from the catalog UPDATE.
    node.safe_sql("CREATE DATABASE regression_invalid;")
    node.safe_sql(
        "UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'regression_invalid';"
    )
    node.command_ok(
        ["vacuumdb", "--all"],
        "invalid database not targeted by vacuumdb -a",
    )

    # Doesn't quite belong here, but don't want to waste time by creating an
    # invalid database in the non-all vacuumdb test as well.
    node.command_fails_like(
        ["vacuumdb", "--dbname", "regression_invalid"],
        re.compile(r'FATAL:  cannot connect to invalid database "regression_invalid"'),
        "vacuumdb cannot target invalid database",
    )
