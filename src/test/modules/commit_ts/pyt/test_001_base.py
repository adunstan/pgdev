# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Single-node commit timestamp test.

Verify that a commit timestamp can be set, and is still present after
crash recovery.
"""


def test_001_base(create_pg):
    node = create_pg(
        "foxtrot", start=False, initdb_extra=["-c", "track_commit_timestamp=on"]
    )
    node.start()

    # Create a table, compare "now()" to the commit TS of its xmin
    node.safe_sql("create table t as select now from (select now(), pg_sleep(1)) f")
    true = node.safe_sql(
        "select t.now - ts.* < '1s' from t, pg_class c, "
        "pg_xact_commit_timestamp(c.xmin) ts where relname = 't'"
    )
    assert true == "t", "commit TS is set"
    ts = node.safe_sql(
        "select ts.* from pg_class, pg_xact_commit_timestamp(xmin) ts "
        "where relname = 't'"
    )

    # Verify that we read the same TS after crash recovery
    node.stop("immediate")
    node.start()

    recovered_ts = node.safe_sql(
        "select ts.* from pg_class, pg_xact_commit_timestamp(xmin) ts "
        "where relname = 't'"
    )
    assert recovered_ts == ts, "commit TS remains after crash recovery"
