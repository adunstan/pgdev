# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Verify that BRIN autosummarization work items work correctly."""

import time


def test_01_workitems(create_pg):
    node = create_pg("tango", start=False)
    node.append_conf("autovacuum_naptime=1s")
    node.start()

    node.safe_sql("create extension pageinspect")

    # Create a table with an autosummarizing BRIN index.  The in-process
    # Session wraps a multi-statement query in one implicit transaction, which
    # is fine here (no transaction-incompatible statements), so the original
    # newline-separated DDL block is issued as-is.
    node.safe_sql(
        "create table brin_wi (a int) with (fillfactor = 10);\n"
        "create index brin_wi_idx on brin_wi using brin (a) "
        "with (pages_per_range=1, autosummarize=on);\n"
    )
    # Another table with an index that requires a snapshot to run
    node.safe_sql(
        "create table journal (d timestamp) with (fillfactor = 10);\n"
        "create function packdate(d timestamp) returns text language plpgsql\n"
        "  as $$ begin return to_char(d, 'yyyymm'); end; $$\n"
        "  returns null on null input immutable;\n"
        "create index brin_packdate_idx on journal using brin (packdate(d))\n"
        "  with (autosummarize = on, pages_per_range = 1);\n"
    )

    count = node.safe_sql(
        "select count(*) from brin_page_items("
        "get_raw_page('brin_wi_idx', 2), 'brin_wi_idx'::regclass)"
    )
    assert count == "1", "initial brin_wi_idx index state is correct"
    count = node.safe_sql(
        "select count(*) from brin_page_items("
        "get_raw_page('brin_packdate_idx', 2), 'brin_packdate_idx'::regclass)"
    )
    assert count == "1", "initial brin_packdate_idx index state is correct"

    node.safe_sql("insert into brin_wi select * from generate_series(1, 100)")
    node.safe_sql(
        "insert into journal select * from generate_series("
        "timestamp '1976-08-01', '1976-10-28', '1 day')"
    )

    # Give a little time for autovacuum to react.  This matches the naptime
    # configured above.
    time.sleep(1)

    assert node.poll_query_until(
        "select count(*) > 1 from brin_page_items("
        "get_raw_page('brin_wi_idx', 2), 'brin_wi_idx'::regclass)"
    )

    count = node.safe_sql(
        "select count(*) from brin_page_items("
        "get_raw_page('brin_wi_idx', 2), 'brin_wi_idx'::regclass)\n"
        " where not placeholder;"
    )
    assert int(count) > 1, f"{count} brin_wi_idx ranges got summarized"

    assert node.poll_query_until(
        "select count(*) > 1 from brin_page_items("
        "get_raw_page('brin_packdate_idx', 2), 'brin_packdate_idx'::regclass)"
    )

    count = node.safe_sql(
        "select count(*) from brin_page_items("
        "get_raw_page('brin_packdate_idx', 2), 'brin_packdate_idx'::regclass)\n"
        " where not placeholder;"
    )
    assert int(count) > 1, f"{count} brin_packdate_idx ranges got summarized"

    node.stop()
