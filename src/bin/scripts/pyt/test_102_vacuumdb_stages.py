# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for vacuumdb --analyze-in-stages and the SQL it issues per stage."""

import re

import pytest


@pytest.fixture
def node(create_pg):
    n = create_pg("main", start=False)
    n.append_conf("log_statement = 'all'\nlog_min_duration_statement = -1")
    n.start()
    return n


def test_analyze_in_stages(node):
    node.issues_sql_like(
        ["vacuumdb", "--analyze-in-stages", "postgres"],
        re.compile(
            r"statement:\ SET\ default_statistics_target=1;\ SET\ vacuum_cost_delay=0;"
            r".*statement:\ ANALYZE"
            r".*statement:\ SET\ default_statistics_target=10;\ RESET\ vacuum_cost_delay;"
            r".*statement:\ ANALYZE"
            r".*statement:\ RESET\ default_statistics_target;"
            r".*statement:\ ANALYZE",
            re.S | re.X,
        ),
        "analyze three times",
    )


def test_analyze_in_stages_all(node):
    node.issues_sql_like(
        ["vacuumdb", "--analyze-in-stages", "--all"],
        re.compile(
            r"statement:\ SET\ default_statistics_target=1;\ SET\ vacuum_cost_delay=0;"
            r".*statement:\ ANALYZE"
            r".*statement:\ SET\ default_statistics_target=1;\ SET\ vacuum_cost_delay=0;"
            r".*statement:\ ANALYZE"
            r".*statement:\ SET\ default_statistics_target=10;\ RESET\ vacuum_cost_delay;"
            r".*statement:\ ANALYZE"
            r".*statement:\ SET\ default_statistics_target=10;\ RESET\ vacuum_cost_delay;"
            r".*statement:\ ANALYZE"
            r".*statement:\ RESET\ default_statistics_target;"
            r".*statement:\ ANALYZE"
            r".*statement:\ RESET\ default_statistics_target;"
            r".*statement:\ ANALYZE",
            re.S | re.X,
        ),
        "analyze more than one database in stages",
    )
