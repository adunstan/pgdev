# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test for promotion handling with WAL records generated post-promotion
before the first checkpoint is generated.  This test case checks for
invalid page references at replay based on the minimum consistent
recovery point defined.
"""


def test_015_promotion_pages(create_pg):
    # Initialize primary node
    alpha = create_pg("alpha", start=False, allows_streaming=True)
    # Setting wal_log_hints to off is important to get invalid page
    # references.
    alpha.append_conf(
        """
wal_log_hints = off
"""
    )

    # Start the primary
    alpha.start()

    # setup/start a standby
    alpha.backup("bkp")
    bravo = create_pg("bravo", start=False)
    bravo.init_from_backup(alpha, "bkp", has_streaming=True)
    bravo.append_conf(
        """
checkpoint_timeout=1h
"""
    )
    bravo.start()

    # Dummy table for the upcoming tests.
    alpha.safe_sql("create table test1 (a int)")
    alpha.safe_sql("insert into test1 select generate_series(1, 10000)")

    # take a checkpoint
    alpha.safe_sql("checkpoint")

    # The following vacuum will set visibility map bits and create
    # problematic WAL records.
    alpha.safe_sql("vacuum verbose test1")
    # Wait for last record to have been replayed on the standby.
    alpha.wait_for_catchup(bravo)

    # Now force a checkpoint on the standby. This seems unnecessary but for
    # "some" reason, the previous checkpoint on the primary does not reflect on
    # the standby and without an explicit checkpoint, it may start redo
    # recovery from a much older point, which includes even create table and
    # initial page additions.
    bravo.safe_sql("checkpoint")

    # Now just use a dummy table and run some operations to move
    # minRecoveryPoint beyond the previous vacuum.
    alpha.safe_sql("create table test2 (a int, b bytea)")
    alpha.safe_sql(
        "insert into test2 select generate_series(1,10000), "
        "sha256(random()::text::bytea)"
    )
    alpha.safe_sql("truncate test2")

    # Wait again for all records to be replayed.
    alpha.wait_for_catchup(bravo)

    # Do the promotion, which reinitializes minRecoveryPoint in the control
    # file so as WAL is replayed up to the end.
    bravo.promote()

    # Truncate the table on the promoted standby, vacuum and extend it
    # again to create new page references.  The first post-recovery checkpoint
    # has not happened yet.
    bravo.safe_sql("truncate test1")
    bravo.safe_sql("vacuum verbose test1")
    bravo.safe_sql("insert into test1 select generate_series(1,1000)")

    # Now crash-stop the promoted standby and restart.  This makes sure that
    # replay does not see invalid page references because of an invalid
    # minimum consistent recovery point.
    bravo.stop("immediate")
    bravo.start()

    # Check state of the table after full crash recovery.  All its data should
    # be here.
    psql_out = bravo.safe_sql("SELECT count(*) FROM test1")
    assert psql_out == "1000", "Check that table state is correct"
