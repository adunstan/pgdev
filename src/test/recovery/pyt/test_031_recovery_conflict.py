# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that connections to a hot standby are correctly canceled when a
recovery conflict is detected.  Also, test that statistics in
pg_stat_database_conflicts are populated correctly.
"""

from pypg.util import TIMEOUT_DEFAULT


def test_031_recovery_conflict(create_pg):
    # Set up nodes
    node_primary = create_pg("primary", start=False, allows_streaming=True)

    tablespace1 = "test_recovery_conflict_tblspc"

    node_primary.append_conf(f"""
allow_in_place_tablespaces = on
log_temp_files = 0

# for deadlock test
max_prepared_transactions = 10

# wait some to test the wait paths as well, but not long for obvious reasons
max_standby_streaming_delay = 50ms

temp_tablespaces = {tablespace1}
# Some of the recovery conflict logging code only gets exercised after
# deadlock_timeout. The test doesn't rely on that additional output, but it's
# nice to get some minimal coverage of that code.
log_recovery_conflict_waits = on
deadlock_timeout = 10ms
""")
    node_primary.start()

    backup_name = "my_backup"

    node_primary.safe_sql(
        f"CREATE TABLESPACE {tablespace1} LOCATION ''")

    node_primary.backup(backup_name)
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)

    node_standby.start()

    test_db = "test_db"

    # use a new database, to trigger database recovery conflict
    node_primary.safe_sql(f"CREATE DATABASE {test_db}")

    # test schema / data
    table1 = "test_recovery_conflict_table1"
    table2 = "test_recovery_conflict_table2"
    node_primary.safe_sql(
        f"""
CREATE TABLE {table1}(a int, b int);
INSERT INTO {table1} SELECT i % 3, 0 FROM generate_series(1,20) i;
CREATE TABLE {table2}(a int, b int);
""",
        dbname=test_db,
    )
    node_primary.wait_for_replay_catchup(node_standby)

    # a longrunning session that we can use to trigger conflicts
    psql_standby = node_standby.connect(test_db)
    expected_conflicts = 0

    cursor1 = "test_recovery_conflict_cursor"

    # Mutable holder for the running log offset, mirroring $log_location.
    log_location = [0]

    def check_conflict_log(message, sect):
        old_log_location = log_location[0]
        log_location[0] = node_standby.wait_for_log(message, old_log_location)
        assert log_location[0] > old_log_location, (
            f"{sect}: logfile contains terminated connection due to "
            "recovery conflict"
        )

    def check_conflict_stat(conflict_type, sect):
        count = node_standby.safe_sql(
            f"SELECT confl_{conflict_type} FROM pg_stat_database_conflicts "
            f"WHERE datname='{test_db}'",
            dbname=test_db,
        )
        assert count == "1", f"{sect}: stats show conflict on standby"

    try:
        ## RECOVERY CONFLICT 1: Buffer pin conflict
        sect = "buffer pin conflict"
        expected_conflicts += 1

        # Aborted INSERT on primary that will be cleaned up by vacuum. Has to be
        # old enough so that there's not a snapshot conflict before the buffer
        # pin conflict.
        node_primary.safe_sql(
            f"""
        BEGIN;
        INSERT INTO {table1} VALUES (1,0);
        ROLLBACK;
        -- ensure flush, rollback doesn't do so
        BEGIN; LOCK {table1}; COMMIT;
        """,
            dbname=test_db,
        )

        node_primary.wait_for_replay_catchup(node_standby)

        # DECLARE and use a cursor on standby, causing buffer with the only
        # block of the relation to be pinned on the standby
        res = psql_standby.query_oneval(
            f"""
        BEGIN;
        DECLARE {cursor1} CURSOR FOR SELECT b FROM {table1};
        FETCH FORWARD FROM {cursor1};
        """)
        # FETCH FORWARD should have returned a 0 since all values of b in the
        # table are 0
        assert res == "0", f"{sect}: cursor with conflicting pin established"

        # to check the log starting now for recovery conflict messages
        log_location[0] = node_standby.log_position()

        # VACUUM FREEZE on the primary
        node_primary.safe_sql(f"VACUUM FREEZE {table1};", dbname=test_db)

        # Wait for catchup. Existing connection will be terminated before replay
        # is finished, so waiting for catchup ensures that there is no race
        # between encountering the recovery conflict which causes the disconnect
        # and checking the logfile for the terminated connection.
        node_primary.wait_for_replay_catchup(node_standby)

        check_conflict_log(
            "User was holding shared buffer pin for too long", sect)
        psql_standby.reconnect()
        check_conflict_stat("bufferpin", sect)

        ## RECOVERY CONFLICT 2: Snapshot conflict
        sect = "snapshot conflict"
        expected_conflicts += 1

        node_primary.safe_sql(
            f"INSERT INTO {table1} SELECT i, 0 FROM generate_series(1,20) i",
            dbname=test_db,
        )
        node_primary.wait_for_replay_catchup(node_standby)

        # DECLARE and FETCH from cursor on the standby
        res = psql_standby.query_oneval(
            f"""
        BEGIN;
        DECLARE {cursor1} CURSOR FOR SELECT b FROM {table1};
        FETCH FORWARD FROM {cursor1};
        """)
        assert res == "0", \
            f"{sect}: cursor with conflicting snapshot established"

        # Do some HOT updates
        node_primary.safe_sql(
            f"UPDATE {table1} SET a = a + 1 WHERE a > 2;", dbname=test_db)

        # VACUUM FREEZE, pruning those dead tuples
        node_primary.safe_sql(f"VACUUM FREEZE {table1};", dbname=test_db)

        # Wait for attempted replay of PRUNE records
        node_primary.wait_for_replay_catchup(node_standby)

        check_conflict_log(
            "User query might have needed to see row versions that must be "
            "removed", sect)
        psql_standby.reconnect()
        check_conflict_stat("snapshot", sect)

        ## RECOVERY CONFLICT 3: Lock conflict
        sect = "lock conflict"
        expected_conflicts += 1

        # acquire lock to conflict with
        res = psql_standby.query_oneval(
            f"""
        BEGIN;
        LOCK TABLE {table1} IN ACCESS SHARE MODE;
        SELECT 1;
        """)
        assert res == "1", f"{sect}: conflicting lock acquired"

        # DROP TABLE containing block which standby has in a pinned buffer
        node_primary.safe_sql(f"DROP TABLE {table1};", dbname=test_db)

        node_primary.wait_for_replay_catchup(node_standby)

        check_conflict_log("User was holding a relation lock for too long", sect)
        psql_standby.reconnect()
        check_conflict_stat("lock", sect)

        ## RECOVERY CONFLICT 4: Tablespace conflict
        sect = "tablespace conflict"
        expected_conflicts += 1

        # DECLARE a cursor for a query which, with sufficiently low work_mem,
        # will spill tuples into temp files in the temporary tablespace created
        # during setup.
        res = psql_standby.query_oneval(
            f"""
        BEGIN;
        SET work_mem = '64kB';
        DECLARE {cursor1} CURSOR FOR
          SELECT count(*) FROM generate_series(1,6000);
        FETCH FORWARD FROM {cursor1};
        """)
        assert res == "6000", \
            f"{sect}: cursor with conflicting temp file established"

        # Drop the tablespace currently containing spill files for the query on
        # the standby
        node_primary.safe_sql(f"DROP TABLESPACE {tablespace1};", dbname=test_db)

        node_primary.wait_for_replay_catchup(node_standby)

        check_conflict_log(
            "User was or might have been using tablespace that must be "
            "dropped", sect)
        psql_standby.reconnect()
        check_conflict_stat("tablespace", sect)

        ## RECOVERY CONFLICT 5: Deadlock
        sect = "startup deadlock"
        expected_conflicts += 1

        # Want to test recovery deadlock conflicts, not buffer pin conflicts.
        # Without changing max_standby_streaming_delay it'd be timing dependent
        # what we hit first
        node_standby.append_conf(
            f"max_standby_streaming_delay = {TIMEOUT_DEFAULT}s")
        psql_standby.close()
        node_standby.restart()
        psql_standby.reconnect()

        # Generate a few dead rows, to later be cleaned up by vacuum. Then
        # acquire a lock on another relation in a prepared xact, so it's held
        # continuously by the startup process. The standby session will block
        # acquiring that lock while holding a pin that vacuum needs, triggering
        # the deadlock.
        # psql runs each statement in its own implicit transaction, so the
        # in-process Session (which wraps a multi-statement string in one
        # implicit transaction) cannot run PREPARE TRANSACTION inside the same
        # string; issue the statements one at a time to match psql semantics.
        node_primary.safe_sql(f"CREATE TABLE {table1}(a int, b int);",
                               dbname=test_db)
        node_primary.safe_sql(f"INSERT INTO {table1} VALUES (1);",
                               dbname=test_db)
        node_primary.safe_sql(
            f"""
BEGIN;
INSERT INTO {table1}(a) SELECT generate_series(1, 100) i;
ROLLBACK;
""",
            dbname=test_db,
        )
        node_primary.safe_sql(
            f"""
BEGIN;
LOCK TABLE {table2};
PREPARE TRANSACTION 'lock';
""",
            dbname=test_db,
        )
        node_primary.safe_sql(f"INSERT INTO {table1}(a) VALUES (170);",
                               dbname=test_db)
        node_primary.safe_sql("SELECT txid_current();", dbname=test_db)

        node_primary.wait_for_replay_catchup(node_standby)

        res = psql_standby.query_oneval(
            f"""
        BEGIN;
        -- hold pin
        DECLARE {cursor1} CURSOR FOR SELECT a FROM {table1};
        FETCH FORWARD FROM {cursor1};
        """)
        assert res == "1", "pin held"
        assert psql_standby.do_async(
            f"""
        -- wait for lock held by prepared transaction
        SELECT * FROM {table2};
        """), (
            f"{sect}: cursor holding conflicting pin, also waiting for lock, "
            "established"
        )

        # just to make sure we're waiting for lock already
        assert node_standby.poll_query_until(
            "SELECT 'waiting' FROM pg_locks WHERE locktype = 'relation' AND "
            "NOT granted;",
            expected="waiting",
        ), f"{sect}: lock acquisition is waiting"

        # VACUUM FREEZE will prune away rows, causing a buffer pin conflict,
        # while standby session is waiting on lock
        node_primary.safe_sql(f"VACUUM FREEZE {table1};", dbname=test_db)
        node_primary.wait_for_replay_catchup(node_standby)

        check_conflict_log(
            "User transaction caused buffer deadlock with recovery.", sect)
        psql_standby.reconnect()
        check_conflict_stat("deadlock", sect)

        # clean up for next tests
        node_primary.safe_sql("ROLLBACK PREPARED 'lock';", dbname=test_db)
        node_standby.append_conf("max_standby_streaming_delay = 50ms")
        psql_standby.close()
        node_standby.restart()
        psql_standby.reconnect()

        # Check that expected number of conflicts show in pg_stat_database.
        # Needs to be tested before database is dropped, for obvious reasons.
        assert node_standby.safe_sql(
            "SELECT conflicts FROM pg_stat_database "
            f"WHERE datname='{test_db}';",
            dbname=test_db,
        ) == str(expected_conflicts), \
            f"{expected_conflicts} recovery conflicts shown in pg_stat_database"

        ## RECOVERY CONFLICT 6: Database conflict
        sect = "database conflict"

        # The standby's psql_standby session must stay connected to test_db:
        # that is the backend the database recovery conflict cancels.
        node_primary.safe_sql(f"DROP DATABASE {test_db};")

        node_primary.wait_for_replay_catchup(node_standby)

        check_conflict_log(
            "User was connected to a database that must be dropped", sect)
    finally:
        # explicitly close session gracefully
        psql_standby.close()

    node_standby.stop()
    node_primary.stop()
