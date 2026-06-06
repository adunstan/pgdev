# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test primary/standby commit timestamps with the GUC toggled repeatedly.

Exercise a primary/standby scenario where the track_commit_timestamp GUC
is repeatedly toggled on and off.
"""

import re


def test_003_standby_2(create_pg):
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

    primary.append_conf("track_commit_timestamp = off")
    primary.restart()
    primary.safe_sql("checkpoint")
    primary_lsn = primary.safe_sql("select pg_current_wal_lsn()")
    assert standby.poll_query_until(
        f"SELECT '{primary_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    ), "standby never caught up"

    standby.safe_sql("checkpoint")
    standby.restart()

    res = standby.sql(
        "SELECT ts.* FROM pg_class, pg_xact_commit_timestamp(xmin) AS ts "
        "WHERE relname = 't10'")
    assert res.error_message is not None, \
        "expect error when getting commit timestamp after restart"
    assert res.psqlout == "", "standby does not return a value after restart"
    assert re.search(r"could not get commit timestamp data",
                     res.error_message), \
        "expected err msg after restart"

    primary.append_conf("track_commit_timestamp = on")
    primary.restart()
    primary.append_conf("track_commit_timestamp = off")
    primary.restart()

    standby.promote()

    standby.safe_sql("create table t11()")
    standby_ts = standby.safe_sql(
        "SELECT ts.* FROM pg_class, pg_xact_commit_timestamp(xmin) AS ts "
        "WHERE relname = 't11'")
    assert standby_ts != "", \
        f"standby gives valid value ({standby_ts}) after promotion"
