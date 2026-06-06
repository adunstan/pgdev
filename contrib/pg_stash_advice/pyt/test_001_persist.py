# Copyright (c) 2016-2026, PostgreSQL Global Development Group

"""Test that pg_stash_advice persists advice stashes across a server restart."""

import os


def test_001_persist(create_pg):
    node = create_pg("main", start=False)
    node.append_conf(
        "shared_preload_libraries = 'pg_plan_advice, pg_stash_advice'\n"
        "pg_stash_advice.persist = true\n"
        "pg_stash_advice.persist_interval = 0"
    )
    node.start()

    node.safe_sql("CREATE EXTENSION pg_stash_advice;\n")

    # Create two stashes: one with 2 entries, one with 1 entry.
    node.safe_sql("""
        SELECT pg_create_advice_stash('stash_a');
        SELECT pg_set_stashed_advice('stash_a', 1001, 'IndexScan(t)');
        SELECT pg_set_stashed_advice('stash_a', 1002, E'line1\\nline2\\ttab\\\\backslash');
        SELECT pg_create_advice_stash('stash_b');
        SELECT pg_set_stashed_advice('stash_b', 2001, 'SeqScan(t)');
    """)

    # Verify before restart.
    result = node.safe_sql(
        "SELECT stash_name, num_entries FROM pg_get_advice_stashes() "
        "ORDER BY stash_name"
    )
    assert result == "stash_a|2\nstash_b|1", "stashes present before restart"

    # Restart and verify the data survived.
    node.restart()
    node.wait_for_log("loaded 2 advice stashes and 3 entries")

    result = node.safe_sql(
        "SELECT stash_name, num_entries FROM pg_get_advice_stashes() "
        "ORDER BY stash_name"
    )
    assert result == "stash_a|2\nstash_b|1", "stashes survived restart"

    # Verify entry contents, including the one with special characters.
    result = node.safe_sql(
        "SELECT stash_name, query_id, advice_string "
        "FROM pg_get_advice_stash_contents(NULL) ORDER BY stash_name, query_id"
    )
    assert result == (
        "stash_a|1001|IndexScan(t)\nstash_a|1002|line1\nline2\ttab\\backslash\n"
        "stash_b|2001|SeqScan(t)"
    ), "entry contents survived restart with special characters intact"

    # Add a third stash with 0 entries.
    node.safe_sql("""
        SELECT pg_create_advice_stash('stash_c');
    """)

    # Restart again and verify all three stashes are present.
    node.restart()
    node.wait_for_log("loaded 3 advice stashes and 3 entries")

    result = node.safe_sql(
        "SELECT stash_name, num_entries FROM pg_get_advice_stashes() "
        "ORDER BY stash_name"
    )
    assert result == (
        "stash_a|2\nstash_b|1\nstash_c|0"
    ), "all three stashes survived second restart"

    # Drop all stashes and verify the dump file is removed after restart.
    node.safe_sql("""
        SELECT pg_drop_advice_stash('stash_a');
        SELECT pg_drop_advice_stash('stash_b');
        SELECT pg_drop_advice_stash('stash_c');
    """)

    node.restart()

    result = node.safe_sql("SELECT count(*) FROM pg_get_advice_stashes()")
    assert result == "0", "no stashes after dropping all and restarting"

    assert not os.path.isfile(
        os.path.join(node.data_dir, "pg_stash_advice.tsv")
    ), "dump file removed after all stashes dropped"

    node.stop()
