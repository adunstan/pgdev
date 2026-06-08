# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Smoke test for streaming-replication support in the pypg framework."""


def test_streaming_replication(create_pg):
    primary = create_pg("primary", allows_streaming=True)
    primary.safe_sql("CREATE TABLE t (id int)")
    primary.safe_sql("INSERT INTO t SELECT generate_series(1, 10)")

    primary.backup("b1")

    standby = create_pg("standby", start=False)
    standby.init_from_backup(primary, "b1", has_streaming=True, standby=True)
    standby.start()

    # More rows after the standby is up.
    primary.safe_sql("INSERT INTO t SELECT generate_series(11, 20)")
    primary.wait_for_catchup("standby")

    # The standby must report its node name as application_name; that is what
    # wait_for_catchup matches on, so assert it explicitly -- if the framework
    # ever stopped setting it, wait_for_catchup would time out instead of
    # failing clearly here.
    assert (
        primary.safe_sql(
            "SELECT application_name FROM pg_catalog.pg_stat_replication"
        )
        == "standby"
    )

    # The standby is read-only and should see all 20 rows.
    assert standby.safe_sql("SELECT pg_is_in_recovery()") == "t"
    assert standby.safe_sql("SELECT count(*) FROM t") == "20"
