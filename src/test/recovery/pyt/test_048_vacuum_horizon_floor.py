# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test that vacuum prunes away all dead tuples killed before OldestXmin.

This test creates a table on a primary, updates the table to generate dead
tuples for vacuum, and then, during the vacuum, uses the replica to force
GlobalVisState->maybe_needed on the primary to move backwards and precede
the value of OldestXmin set at the beginning of vacuuming the table.
"""

import re


def test_048_vacuum_horizon_floor(create_pg):
    # Set up nodes
    node_primary = create_pg("primary", start=False, allows_streaming="physical")

    # io_combine_limit is set to 1 to avoid pinning more than one buffer at a
    # time to ensure test determinism.
    node_primary.append_conf("""
hot_standby_feedback = on
autovacuum = off
log_min_messages = INFO
maintenance_work_mem = 64
io_combine_limit = 1
""")
    node_primary.start()

    node_replica = create_pg("standby", start=False)

    node_primary.backup("my_backup")
    node_replica.init_from_backup(node_primary, "my_backup", has_streaming=True)

    node_replica.start()

    test_db = "test_db"
    node_primary.safe_sql(f"CREATE DATABASE {test_db}")

    # Save the original connection info for later use
    orig_conninfo = node_primary.connstr()

    table1 = "vac_horizon_floor_table"

    # Long-running Primary Session A
    session_primaryA = node_primary.connect(test_db)

    # Long-running Primary Session B
    session_primaryB = node_primary.connect(test_db)

    try:
        # Our test relies on two rounds of index vacuuming for reasons
        # elaborated later. To trigger two rounds of index vacuuming, we must
        # fill up the TidStore with dead items partway through a vacuum of the
        # table. The number of rows is just enough to ensure we exceed
        # maintenance_work_mem on all supported platforms, while keeping test
        # runtime as short as we can.
        nrows = 2000

        # Because vacuum's first pass, pruning, is where we use the
        # GlobalVisState to check tuple visibility, GlobalVisState->maybe_needed
        # must move backwards during pruning before checking the visibility for
        # a tuple which would have been considered HEAPTUPLE_DEAD prior to
        # maybe_needed moving backwards but HEAPTUPLE_RECENTLY_DEAD compared to
        # the new, older value of maybe_needed.
        #
        # We must not only force the horizon on the primary to move backwards
        # but also force the vacuuming backend's GlobalVisState to be updated.
        # GlobalVisState is forced to update during index vacuuming.
        #
        # _bt_pendingfsm_finalize() calls GetOldestNonRemovableTransactionId()
        # at the end of a round of index vacuuming, updating the backend's
        # GlobalVisState and, in our case, moving maybe_needed backwards.
        #
        # Then vacuum's first (pruning) pass will continue and pruning will find
        # our later inserted and updated tuple HEAPTUPLE_RECENTLY_DEAD when
        # compared to maybe_needed but HEAPTUPLE_DEAD when compared to
        # OldestXmin.
        #
        # Thus, we must force at least two rounds of index vacuuming to ensure
        # that some tuple visibility checks will happen after a round of index
        # vacuuming. To accomplish this, we set maintenance_work_mem to its
        # minimum value and insert and delete enough rows that we force at least
        # one round of index vacuuming before getting to a dead tuple which was
        # killed after the standby is disconnected.
        node_primary.safe_sql(
            f"""
            CREATE TABLE {table1}(col1 int)
                WITH (autovacuum_enabled=false, fillfactor=10);
            INSERT INTO {table1} VALUES(7);
            INSERT INTO {table1} SELECT generate_series(1, {nrows}) % 3;
            CREATE INDEX on {table1}(col1);
            DELETE FROM {table1} WHERE col1 = 0;
            INSERT INTO {table1} VALUES(7);
        """,
            dbname=test_db,
        )

        # We will later move the primary forward while the standby is
        # disconnected. For now, however, there is no reason not to wait for the
        # standby to catch up.
        primary_lsn = node_primary.lsn("flush")
        node_primary.wait_for_catchup(node_replica, "replay", primary_lsn)

        # Test that the WAL receiver is up and running.
        assert node_replica.poll_query_until(
            "SELECT EXISTS (SELECT * FROM pg_stat_wal_receiver);",
            expected="t",
            dbname=test_db,
        )

        # Set primary_conninfo to something invalid on the replica and reload
        # the config. Once the config is reloaded, the startup process will
        # force the WAL receiver to restart and it will be unable to reconnect
        # because of the invalid connection information.
        #
        # psql runs each statement in its own implicit transaction, but the
        # in-process Session wraps a multi-statement string in one transaction
        # block.  ALTER SYSTEM cannot run inside a transaction block, so issue
        # the statements separately to match psql semantics.
        node_replica.safe_sql(
            "ALTER SYSTEM SET primary_conninfo = '';", dbname=test_db)
        node_replica.safe_sql("SELECT pg_reload_conf();", dbname=test_db)

        # Wait until the WAL receiver has shut down and been unable to start up
        # again.
        assert node_replica.poll_query_until(
            "SELECT EXISTS (SELECT * FROM pg_stat_wal_receiver);",
            expected="f",
            dbname=test_db,
        )

        # Now insert and update a tuple which will be visible to the vacuum on
        # the primary but which will have xmax newer than the oldest xmin on the
        # standby that was recently disconnected.
        res = session_primaryA.query(
            f"""
                INSERT INTO {table1} VALUES (99);
                UPDATE {table1} SET col1 = 100 WHERE col1 = 99;
                SELECT 'after_update';
            """
        )

        # Make sure the UPDATE finished
        assert re.search(r"^after_update$", res.psqlout, re.M), \
            "UPDATE occurred on primary session A"

        # Open a cursor on the primary whose pin will keep VACUUM from getting a
        # cleanup lock on the first page of the relation. We want VACUUM to be
        # able to start, calculate initial values for OldestXmin and
        # GlobalVisState and then be unable to proceed with pruning our dead
        # tuples. This will allow us to reconnect the standby and push the
        # horizon back before we start actual pruning and vacuuming.
        primary_cursor1 = "vac_horizon_floor_cursor1"

        # The first value inserted into the table was a 7, so FETCH FORWARD
        # should return a 7. That's how we know the cursor has a pin.
        # Disable index scans so the cursor pins heap pages and not index pages.
        res = session_primaryB.query(
            f"""
            BEGIN;
            SET enable_bitmapscan = off;
            SET enable_indexscan = off;
            SET enable_indexonlyscan = off;
            DECLARE {primary_cursor1} CURSOR FOR SELECT * FROM {table1} WHERE col1 = 7;
            FETCH {primary_cursor1};
            """
        )

        assert res.psqlout == "7", \
            f"Cursor query returned {res.psqlout}. Expected value 7."

        # Get the PID of the session which will run the VACUUM FREEZE so that we
        # can use it to filter pg_stat_activity later.
        vacuum_pid = session_primaryA.query_oneval("SELECT pg_backend_pid();")

        # Now start a VACUUM FREEZE on the primary. It will call
        # vacuum_get_cutoffs() and establish values of OldestXmin and
        # GlobalVisState which are newer than all of our dead tuples. Then it
        # will be unable to get a cleanup lock to start pruning, so it will hang.
        #
        # We use VACUUM FREEZE because it will wait for a cleanup lock instead of
        # skipping the page pinned by the cursor. Note that works because the
        # target tuple's xmax precedes OldestXmin which ensures that
        # lazy_scan_noprune() will return false and we will wait for the cleanup
        # lock.
        #
        # Disable any prefetching, parallelism, or other concurrent I/O by
        # vacuum. The pages of the heap must be processed in order by a single
        # worker to ensure test stability (PARALLEL 0 shouldn't be necessary but
        # guards against the possibility of parallel heap vacuuming).
        session_primaryA.do("SET maintenance_io_concurrency = 0;")
        assert session_primaryA.do_async(
            f"VACUUM (VERBOSE, FREEZE, PARALLEL 0) {table1};")

        # Make sure that the VACUUM has already called vacuum_get_cutoffs() and
        # is just waiting on the lock to start vacuuming. We don't want the
        # standby to re-establish a connection to the primary and push the
        # horizon back until we've saved initial values in GlobalVisState and
        # calculated OldestXmin.
        assert node_primary.poll_query_until(
            f"""
            SELECT count(*) >= 1 FROM pg_stat_activity
                WHERE pid = {vacuum_pid}
                AND wait_event = 'BufferCleanup';
            """,
            expected="t",
            dbname=test_db,
        )

        # Ensure the WAL receiver is still not active on the replica.
        assert node_replica.poll_query_until(
            "SELECT EXISTS (SELECT * FROM pg_stat_wal_receiver);",
            expected="f",
            dbname=test_db,
        )

        # Allow the WAL receiver connection to re-establish.  Issue the
        # statements separately (see note above) so ALTER SYSTEM does not run
        # inside a transaction block.
        # connstr() embeds single quotes (host='...' dbname='...'); double
        # them so the whole thing survives as one SQL string literal.
        escaped_conninfo = orig_conninfo.replace("'", "''")
        node_replica.safe_sql(
            f"ALTER SYSTEM SET primary_conninfo = '{escaped_conninfo}';",
            dbname=test_db,
        )
        node_replica.safe_sql("SELECT pg_reload_conf();", dbname=test_db)

        # Ensure the new WAL receiver has connected.
        assert node_replica.poll_query_until(
            "SELECT EXISTS (SELECT * FROM pg_stat_wal_receiver);",
            expected="t",
            dbname=test_db,
        )

        # Once the WAL sender is shown on the primary, the replica should have
        # connected with the primary and pushed the horizon backward. Primary
        # Session A won't see that until the VACUUM FREEZE proceeds and does its
        # first round of index vacuuming.
        assert node_primary.poll_query_until(
            "SELECT EXISTS (SELECT * FROM pg_stat_replication);",
            expected="t",
            dbname=test_db,
        )

        # Move the cursor forward to the next 7. We inserted the 7 much later,
        # so advancing the cursor should allow vacuum to proceed vacuuming most
        # pages of the relation. Because we set maintenance_work_mem
        # sufficiently low, we expect that a round of index vacuuming has
        # happened and that the vacuum is now waiting for the cursor to release
        # its pin on the last page of the relation.
        res = session_primaryB.query_oneval(f"FETCH {primary_cursor1}")
        assert res == "7", \
            f"Cursor query returned {res} from second fetch. Expected value 7."

        # Prevent the test from incorrectly passing by confirming that we did
        # indeed do a pass of index vacuuming.
        assert node_primary.poll_query_until(
            f"""
            SELECT index_vacuum_count > 0
            FROM pg_stat_progress_vacuum
            WHERE datname='{test_db}' AND relid::regclass = '{table1}'::regclass;
            """,
            expected="t",
            dbname=test_db,
        )

        # Commit the transaction with the open cursor so that the VACUUM can
        # finish.
        session_primaryB.do("COMMIT")

        # VACUUM proceeds with pruning and does a visibility check on each tuple.
        # In older versions of Postgres, pruning found our final dead tuple
        # non-removable (HEAPTUPLE_RECENTLY_DEAD) since its xmax is after the new
        # value of maybe_needed. Then heap_prepare_freeze_tuple() would decide
        # the tuple xmax should be frozen because it precedes OldestXmin. Vacuum
        # would then error out in heap_pre_freeze_checks() with "cannot freeze
        # committed xmax". This was fixed by changing pruning to find all
        # HEAPTUPLE_RECENTLY_DEAD tuples with xmaxes preceding OldestXmin
        # HEAPTUPLE_DEAD and removing them.

        # Collect the VACUUM's async result so it does not error out.
        vacuum_res = session_primaryA.get_async_result()
        assert vacuum_res is not None
        assert vacuum_res.error_message is None, vacuum_res.error_message

        # With the fix, VACUUM should finish successfully, incrementing the
        # table vacuum_count.
        assert node_primary.poll_query_until(
            f"""
            SELECT vacuum_count > 0
            FROM pg_stat_all_tables WHERE relname = '{table1}';
            """,
            expected="t",
            dbname=test_db,
        )

        primary_lsn = node_primary.lsn("flush")

        # Make sure something causes us to flush
        node_primary.safe_sql(f"INSERT INTO {table1} VALUES (1);", dbname=test_db)

        # Nothing on the replica should cause a recovery conflict, so this should
        # finish successfully.
        node_primary.wait_for_catchup(node_replica, "replay", primary_lsn)
    finally:
        ## Shut down sessions
        session_primaryA.close()
        session_primaryB.close()

    node_replica.stop()
    node_primary.stop()
