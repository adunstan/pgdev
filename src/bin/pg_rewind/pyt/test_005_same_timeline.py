# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that running pg_rewind with the source and target clusters on the same
timeline runs successfully.
"""


def test_005_same_timeline(rewind):
    rewind.setup_cluster()
    rewind.start_primary()
    rewind.create_standby()
    rewind.run_pg_rewind("local")
    rewind.clean_rewind_test()
