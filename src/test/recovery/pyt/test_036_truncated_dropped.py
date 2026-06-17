# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests recovery scenarios where the files are shorter than in the common
cases, e.g. due to replaying WAL records of a relation that was subsequently
truncated or dropped.
"""


def test_036_truncated_dropped(create_pg):
    node = create_pg("n1", start=False)

    # Disable autovacuum to guarantee VACUUM can remove rows / truncate
    # relations
    node.append_conf(
        """
wal_level = 'replica'
autovacuum = off
"""
    )

    node.start()

    # Test: Replay replay of PRUNE records for a pre-existing, then dropped,
    # relation.
    #
    # Statements are issued one at a time because the in-process libpq Session
    # runs a multi-statement string as a single implicit transaction, and
    # VACUUM / CHECKPOINT cannot run inside a transaction block.
    node.safe_sql("CREATE TABLE truncme(i int) WITH (fillfactor = 50)")
    node.safe_sql("INSERT INTO truncme SELECT generate_series(1, 1000)")
    node.safe_sql("UPDATE truncme SET i = 1")
    node.safe_sql("CHECKPOINT")  # ensure relation exists at start of recovery
    node.safe_sql("VACUUM truncme")  # generate prune records
    node.safe_sql("DROP TABLE truncme")

    node.stop("immediate")

    node.start()  # replay of PRUNE records for a pre-existing, then dropped, relation

    # Test: Replay of PRUNE records for a newly created, then dropped, relation
    node.safe_sql("CREATE TABLE truncme(i int) WITH (fillfactor = 50)")
    node.safe_sql("INSERT INTO truncme SELECT generate_series(1, 1000)")
    node.safe_sql("UPDATE truncme SET i = 1")
    node.safe_sql("VACUUM truncme")  # generate prune records
    node.safe_sql("DROP TABLE truncme")

    node.stop("immediate")

    node.start()  # replay of PRUNE records for a newly created, then dropped, relation

    # Test: Replay of PRUNE records affecting truncated block. With FPIs used
    # for PRUNE.
    node.safe_sql("CREATE TABLE truncme(i int) WITH (fillfactor = 50)")
    node.safe_sql("INSERT INTO truncme SELECT generate_series(1, 1000)")
    node.safe_sql("UPDATE truncme SET i = 1")
    node.safe_sql("CHECKPOINT")  # generate FPIs
    node.safe_sql("VACUUM truncme")  # generate prune records
    node.safe_sql("TRUNCATE truncme")  # make blocks non-existing
    node.safe_sql("INSERT INTO truncme SELECT generate_series(1, 10)")

    node.stop("immediate")

    node.start()  # replay of PRUNE records affecting truncated block (FPIs)

    assert (
        node.safe_sql("select count(*), sum(i) FROM truncme") == "10|55"
    ), "table contents as expected after recovery"
    node.safe_sql("DROP TABLE truncme")

    # Test replay of PRUNE records for blocks that are later truncated. Without
    # FPIs used for PRUNE.
    node.safe_sql("CREATE TABLE truncme(i int) WITH (fillfactor = 50)")
    node.safe_sql("INSERT INTO truncme SELECT generate_series(1, 1000)")
    node.safe_sql("UPDATE truncme SET i = 1")
    node.safe_sql("VACUUM truncme")  # generate prune records
    node.safe_sql("TRUNCATE truncme")  # make blocks non-existing
    node.safe_sql("INSERT INTO truncme SELECT generate_series(1, 10)")

    node.stop("immediate")

    node.start()  # replay of PRUNE records affecting truncated block (no FPIs)

    assert (
        node.safe_sql("select count(*), sum(i) FROM truncme") == "10|55"
    ), "table contents as expected after recovery"
    node.safe_sql("DROP TABLE truncme")

    # Test: Replay of partial truncation via VACUUM
    node.safe_sql("CREATE TABLE truncme(i int) WITH (fillfactor = 50)")
    node.safe_sql("INSERT INTO truncme SELECT generate_series(1, 1000)")
    node.safe_sql("UPDATE truncme SET i = i + 1")
    # ensure a mix of pre/post truncation rows
    node.safe_sql("DELETE FROM truncme WHERE i > 500")

    node.safe_sql("VACUUM truncme")  # should truncate relation

    # rows at TIDs that previously existed
    node.safe_sql("INSERT INTO truncme SELECT generate_series(1000, 1010)")

    node.stop("immediate")

    node.start()  # replay of partial truncation via VACUUM

    assert (
        node.safe_sql("select count(*), sum(i), min(i), max(i) FROM truncme")
        == "510|136304|2|1010"
    ), "table contents as expected after recovery"
    node.safe_sql("DROP TABLE truncme")
