# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test a simple commit timestamp scenario involving a standby."""

import re


def test_002_standby(create_pg):
    bkplabel = "backup"
    primary = create_pg("primary", start=False, allows_streaming=True)
    primary.append_conf("""
track_commit_timestamp = on
max_wal_senders = 5
""")
    primary.start()
    primary.backup(bkplabel)

    standby = create_pg("standby", start=False)
    standby.init_from_backup(primary, bkplabel, has_streaming=True)
    standby.start()

    for i in range(1, 11):
        primary.safe_sql(f"create table t{i}()")

    primary_ts = primary.safe_sql(
        "SELECT ts.* FROM pg_class, pg_xact_commit_timestamp(xmin) AS ts "
        "WHERE relname = 't10'")
    primary_lsn = primary.safe_sql("select pg_current_wal_lsn()")
    assert standby.poll_query_until(
        f"SELECT '{primary_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    ), "standby never caught up"

    standby_ts = standby.safe_sql(
        "select ts.* from pg_class, pg_xact_commit_timestamp(xmin) ts "
        "where relname = 't10'")
    assert primary_ts == standby_ts, "standby gives same value as primary"

    primary.append_conf("track_commit_timestamp = off")
    primary.restart()
    primary.safe_sql("checkpoint")
    primary_lsn = primary.safe_sql("select pg_current_wal_lsn()")
    assert standby.poll_query_until(
        f"SELECT '{primary_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    ), "standby never caught up"
    standby.safe_sql("checkpoint")

    # This one should raise an error now
    res = standby.sql(
        "select ts.* from pg_class, pg_xact_commit_timestamp(xmin) ts "
        "where relname = 't10'")
    assert res.error_message is not None, \
        "standby errors when primary turned feature off"
    assert res.psqlout == "", \
        "standby gives no value when primary turned feature off"
    assert re.search(r"could not get commit timestamp data",
                     res.error_message), \
        "expected error when primary turned feature off"
