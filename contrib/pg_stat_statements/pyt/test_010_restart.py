# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Check that pg_stat_statements contents are preserved across restarts.

All queries run in-process via the libpq Session.
"""

_QUERY = (
    "SELECT query FROM pg_stat_statements "
    "WHERE query NOT LIKE '%pg_stat_statements%' ORDER BY query"
)


def test_pg_stat_statements_across_restart(create_pg):
    node = create_pg("main", start=False)
    node.append_conf("shared_preload_libraries = 'pg_stat_statements'")
    node.start()

    node.safe_sql("CREATE EXTENSION pg_stat_statements")

    node.safe_sql("CREATE TABLE t1 (a int)")
    node.safe_sql("SELECT a FROM t1")

    assert (
        node.safe_sql(_QUERY) == "CREATE TABLE t1 (a int)\nSELECT a FROM t1"
    ), "pg_stat_statements populated"

    node.restart()

    assert (
        node.safe_sql(_QUERY) == "CREATE TABLE t1 (a int)\nSELECT a FROM t1"
    ), "pg_stat_statements data kept across restart"

    node.append_conf("pg_stat_statements.save = false")
    node.reload()

    node.restart()

    assert (
        node.safe_sql(
            "SELECT count(*) FROM pg_stat_statements "
            "WHERE query NOT LIKE '%pg_stat_statements%'"
        )
        == "0"
    ), "pg_stat_statements data not kept across restart with .save=false"

    node.stop()
