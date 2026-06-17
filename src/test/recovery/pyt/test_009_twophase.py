# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests dedicated to two-phase commit in recovery."""

import os


def _configure_and_reload(node, parameter):
    """Append *parameter* to postgresql.conf and reload, asserting success."""
    node.append_conf(parameter + "\n")
    psql_out = node.safe_sql("SELECT pg_reload_conf()")
    assert psql_out == "t", f"reload node {node.name} with {parameter}"


def test_009_twophase(create_pg, tmp_path):
    # Set up two nodes, which will alternately be primary and replication
    # standby.

    # Setup london node
    node_london = create_pg("london", start=False, allows_streaming=True)
    node_london.append_conf(
        """
	max_prepared_transactions = 10
	log_checkpoints = true
"""
    )
    node_london.start()
    node_london.backup("london_backup")

    # Setup paris node
    node_paris = create_pg("paris", start=False)
    node_paris.init_from_backup(node_london, "london_backup", has_streaming=True)
    node_paris.append_conf(
        """
	subtransaction_buffers = 32
"""
    )
    node_paris.start()

    # Switch to synchronous replication in both directions
    _configure_and_reload(node_london, "synchronous_standby_names = 'paris'")
    _configure_and_reload(node_paris, "synchronous_standby_names = 'london'")

    # Set up nonce names for current primary and standby nodes
    # Initially, london is primary and paris is standby
    cur_primary, cur_standby = node_london, node_paris
    cur_primary_name = cur_primary.name

    # Create table we'll use in the test transactions
    cur_primary.safe_sql("CREATE TABLE t_009_tbl (id int, msg text)")

    ###########################################################################
    # Check that we can commit and abort transaction after soft restart.
    # Here checkpoint happens before shutdown and no WAL replay will occur at
    # next startup. In this case postgres re-creates shared-memory state from
    # twophase files.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (1, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (2, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_1';"""
    )
    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (3, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (4, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_2';"""
    )
    cur_primary.stop()
    cur_primary.start()

    # safe_sql raises on error, so a successful return mirrors psql_rc == 0
    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_1'")
    cur_primary.safe_sql("ROLLBACK PREPARED 'xact_009_2'")

    ###########################################################################
    # Check that we can commit and abort after a hard restart.
    # At next startup, WAL replay will re-create shared memory state for
    # prepared transaction using dedicated WAL records.
    ###########################################################################

    cur_primary.safe_sql("CHECKPOINT")
    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (5, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (6, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_3';"""
    )
    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (7, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (8, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_4';"""
    )
    cur_primary.stop("immediate")
    cur_primary.start()

    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_3'")
    cur_primary.safe_sql("ROLLBACK PREPARED 'xact_009_4'")

    ###########################################################################
    # Check that WAL replay can handle several transactions with same GID name.
    ###########################################################################

    cur_primary.safe_sql("CHECKPOINT")
    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (9, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (10, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_5';"""
    )
    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_5'")
    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (11, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (12, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_5';"""
    )
    cur_primary.stop("immediate")
    cur_primary.start()

    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_5'")

    ###########################################################################
    # Check that WAL replay cleans up its shared memory state and releases
    # locks while replaying transaction commits.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (13, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (14, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_6';"""
    )
    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_6'")
    cur_primary.stop("immediate")
    cur_primary.start()
    # This prepare can fail due to conflicting GID or locks conflicts if
    # replay did not fully cleanup its state on previous commit.
    # safe_sql raises on error, so a successful return mirrors psql_rc == 0.
    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (15, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (16, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_7';"""
    )

    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_7'")

    ###########################################################################
    # Check that WAL replay will cleanup its shared memory state on running
    # standby.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (17, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (18, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_8';"""
    )
    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_8'")
    psql_out = cur_standby.safe_sql("SELECT count(*) FROM pg_prepared_xacts")
    assert (
        psql_out == "0"
    ), "Cleanup of shared memory state on running standby without checkpoint"

    ###########################################################################
    # Same as in previous case, but let's force checkpoint on standby between
    # prepare and commit to use on-disk twophase files.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (19, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (20, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_9';"""
    )
    cur_standby.safe_sql("CHECKPOINT")
    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_9'")
    psql_out = cur_standby.safe_sql("SELECT count(*) FROM pg_prepared_xacts")
    assert (
        psql_out == "0"
    ), "Cleanup of shared memory state on running standby after checkpoint"

    ###########################################################################
    # Check that prepared transactions can be committed on promoted standby.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (21, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (22, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_10';"""
    )
    cur_primary.stop()
    cur_standby.promote()

    # change roles
    # Now paris is primary and london is standby
    cur_primary, cur_standby = node_paris, node_london
    cur_primary_name = cur_primary.name

    # london is not running at this point, so we must not commit synchronously
    # here (it would wait forever for the down standby).  COMMIT PREPARED
    # cannot run in a transaction block, and a multi-statement string is one
    # implicit transaction, so set synchronous_commit and commit as separate
    # statements on one persistent connection.
    with cur_primary.connect() as sess:
        sess.query_safe("SET synchronous_commit = off")
        sess.query_safe("COMMIT PREPARED 'xact_009_10'")

    # restart old primary as new standby
    cur_standby.enable_streaming(cur_primary)
    cur_standby.start()

    ###########################################################################
    # Check that prepared transactions are replayed after soft restart of
    # standby while primary is down. Since standby knows that primary is down
    # it uses a different code path on startup to ensure that the status of
    # transactions is consistent.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (23, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (24, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_11';"""
    )
    cur_primary.stop()
    cur_standby.restart()
    cur_standby.promote()

    # change roles
    # Now london is primary and paris is standby
    cur_primary, cur_standby = node_london, node_paris
    cur_primary_name = cur_primary.name

    psql_out = cur_primary.safe_sql("SELECT count(*) FROM pg_prepared_xacts")
    assert psql_out == "1", "Restore prepared transactions from files with primary down"

    # restart old primary as new standby
    cur_standby.enable_streaming(cur_primary)
    cur_standby.start()

    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_11'")

    ###########################################################################
    # Check that prepared transactions are correctly replayed after standby
    # hard restart while primary is down.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	INSERT INTO t_009_tbl VALUES (25, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl VALUES (26, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_12';
	"""
    )
    cur_primary.stop()
    cur_standby.stop("immediate")
    cur_standby.start()
    cur_standby.promote()

    # change roles
    # Now paris is primary and london is standby
    cur_primary, cur_standby = node_paris, node_london
    cur_primary_name = cur_primary.name

    psql_out = cur_primary.safe_sql("SELECT count(*) FROM pg_prepared_xacts")
    assert (
        psql_out == "1"
    ), "Restore prepared transactions from records with primary down"

    # restart old primary as new standby
    cur_standby.enable_streaming(cur_primary)
    cur_standby.start()

    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_12'")

    ###########################################################################
    # Check visibility of prepared transactions in standby after a restart
    # while primary is down.
    ###########################################################################

    # Set synchronous_commit='remote_apply' so the standby is caught up.  The
    # GUC must persist across the CREATE TABLE and the prepared transaction,
    # and neither may share an implicit transaction with the SET, so run them
    # as separate statements on one persistent connection.
    with cur_primary.connect() as sess:
        sess.query_safe("SET synchronous_commit='remote_apply'")
        sess.query_safe("CREATE TABLE t_009_tbl_standby_mvcc (id int, msg text)")
        sess.query_safe(
            f"""
	BEGIN;
	INSERT INTO t_009_tbl_standby_mvcc VALUES (1, 'issued to {cur_primary_name}');
	SAVEPOINT s1;
	INSERT INTO t_009_tbl_standby_mvcc VALUES (2, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_standby_mvcc';
	"""
        )
    cur_primary.stop()
    cur_standby.restart()

    # Acquire a snapshot in standby, before we commit the prepared transaction
    standby_session = cur_standby.connect()
    standby_session.do("BEGIN ISOLATION LEVEL REPEATABLE READ")
    psql_out = standby_session.query_oneval(
        "SELECT count(*) FROM t_009_tbl_standby_mvcc"
    )
    assert psql_out == "0", "Prepared transaction not visible in standby before commit"

    # Commit the transaction in primary
    cur_primary.start()
    # Set synchronous_commit='remote_apply' so the standby is caught up.
    # COMMIT PREPARED cannot run in a transaction block, so set the GUC and
    # commit as separate statements on one persistent connection.
    with cur_primary.connect() as sess:
        sess.query_safe("SET synchronous_commit='remote_apply'")
        sess.query_safe("COMMIT PREPARED 'xact_009_standby_mvcc'")

    # Still not visible to the old snapshot
    psql_out = standby_session.query_oneval(
        "SELECT count(*) FROM t_009_tbl_standby_mvcc"
    )
    assert (
        psql_out == "0"
    ), "Committed prepared transaction not visible to old snapshot in standby"

    # Is visible to a new snapshot
    standby_session.do("COMMIT")
    psql_out = standby_session.query_oneval(
        "SELECT count(*) FROM t_009_tbl_standby_mvcc"
    )
    assert (
        psql_out == "2"
    ), "Committed prepared transaction is visible to new snapshot in standby"
    standby_session.close()

    ###########################################################################
    # Check for a lock conflict between prepared transaction with DDL inside
    # and replay of XLOG_STANDBY_LOCK wal record.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	CREATE TABLE t_009_tbl2 (id int, msg text);
	SAVEPOINT s1;
	INSERT INTO t_009_tbl2 VALUES (27, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_13';"""
    )
    # checkpoint will issue XLOG_STANDBY_LOCK that can conflict with lock
    # held by 'create table' statement
    cur_primary.safe_sql("CHECKPOINT")
    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_13'")

    # Ensure that last transaction is replayed on standby.
    cur_primary_lsn = cur_primary.safe_sql("SELECT pg_current_wal_lsn()")
    caughtup_query = f"SELECT '{cur_primary_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    assert cur_standby.poll_query_until(
        caughtup_query
    ), "Timed out while waiting for standby to catch up"

    psql_out = cur_standby.safe_sql("SELECT count(*) FROM t_009_tbl2")
    assert psql_out == "1", "Replay prepared transaction with DDL"

    ###########################################################################
    # Check recovery of prepared transaction with DDL inside after a hard
    # restart of the primary.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	CREATE TABLE t_009_tbl3 (id int, msg text);
	SAVEPOINT s1;
	INSERT INTO t_009_tbl3 VALUES (28, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_14';"""
    )
    cur_primary.safe_sql(
        f"""
	BEGIN;
	CREATE TABLE t_009_tbl4 (id int, msg text);
	SAVEPOINT s1;
	INSERT INTO t_009_tbl4 VALUES (29, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_15';"""
    )

    cur_primary.stop("immediate")
    cur_primary.start()

    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_14'")
    cur_primary.safe_sql("ROLLBACK PREPARED 'xact_009_15'")

    ###########################################################################
    # Check recovery of prepared transaction with DDL inside after a soft
    # restart of the primary.
    ###########################################################################

    cur_primary.safe_sql(
        f"""
	BEGIN;
	CREATE TABLE t_009_tbl5 (id int, msg text);
	SAVEPOINT s1;
	INSERT INTO t_009_tbl5 VALUES (30, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_16';"""
    )
    cur_primary.safe_sql(
        f"""
	BEGIN;
	CREATE TABLE t_009_tbl6 (id int, msg text);
	SAVEPOINT s1;
	INSERT INTO t_009_tbl6 VALUES (31, 'issued to {cur_primary_name}');
	PREPARE TRANSACTION 'xact_009_17';"""
    )

    cur_primary.stop()
    cur_primary.start()

    cur_primary.safe_sql("COMMIT PREPARED 'xact_009_16'")
    cur_primary.safe_sql("ROLLBACK PREPARED 'xact_009_17'")

    ###########################################################################
    # Verify expected data appears on both servers.
    ###########################################################################

    psql_out = cur_primary.safe_sql("SELECT count(*) FROM pg_prepared_xacts")
    assert psql_out == "0", "No uncommitted prepared transactions on primary"

    expected_tbl = """1|issued to london
2|issued to london
5|issued to london
6|issued to london
9|issued to london
10|issued to london
11|issued to london
12|issued to london
13|issued to london
14|issued to london
15|issued to london
16|issued to london
17|issued to london
18|issued to london
19|issued to london
20|issued to london
21|issued to london
22|issued to london
23|issued to paris
24|issued to paris
25|issued to london
26|issued to london"""

    psql_out = cur_primary.safe_sql("SELECT * FROM t_009_tbl ORDER BY id")
    assert psql_out == expected_tbl, "Check expected t_009_tbl data on primary"

    psql_out = cur_primary.safe_sql("SELECT * FROM t_009_tbl2")
    assert psql_out == "27|issued to paris", "Check expected t_009_tbl2 data on primary"

    psql_out = cur_standby.safe_sql("SELECT count(*) FROM pg_prepared_xacts")
    assert psql_out == "0", "No uncommitted prepared transactions on standby"

    psql_out = cur_standby.safe_sql("SELECT * FROM t_009_tbl ORDER BY id")
    assert psql_out == expected_tbl, "Check expected t_009_tbl data on standby"

    psql_out = cur_standby.safe_sql("SELECT * FROM t_009_tbl2")
    assert psql_out == "27|issued to paris", "Check expected t_009_tbl2 data on standby"

    # Exercise the 2PC recovery code in StartupSUBTRANS, which is concerned
    # with ensuring that enough pg_subtrans pages exist on disk to cover the
    # range of prepared transactions at server start time.  There's not much we
    # can verify directly, but let's at least get the code to run.
    cur_standby.stop()
    _configure_and_reload(cur_primary, "synchronous_standby_names = ''")

    cur_primary.safe_sql("CHECKPOINT")

    cur_primary.safe_sql("select pg_current_wal_insert_lsn()")
    # "CREATE TABLE test()" autocommits on its own, then the BEGIN..PREPARE
    # block prepares test1.  As a single multi-statement string the whole
    # batch would be one implicit transaction ending in PREPARE, so "test"
    # would never commit; issue them as separate statements.
    cur_primary.safe_sql("CREATE TABLE test()")
    cur_primary.safe_sql("BEGIN; CREATE TABLE test1(); PREPARE TRANSACTION 'foo';")
    osubtrans = cur_primary.safe_sql(
        "select 'pg_subtrans/'||f, s.size from pg_ls_dir('pg_subtrans') f, "
        "pg_stat_file('pg_subtrans/'||f) s"
    )

    # pgbench run to cause pg_subtrans traffic
    pgb_script = os.path.join(str(tmp_path), "009_twophase.pgb")
    with open(pgb_script, "w", encoding="utf-8") as fh:
        fh.write("insert into test default values\n")
    cur_primary.pg_bin.command_ok(
        [
            "pgbench",
            "--no-vacuum",
            "--client=5",
            "--transactions=1000",
            "-f",
            pgb_script,
            "postgres",
        ],
        "pgbench run to cause pg_subtrans traffic",
    )

    # StartupSUBTRANS is exercised with a wide range of visible XIDs in this
    # stop/start sequence, because we left a prepared transaction open above.
    # Also, setting subtransaction_buffers to 32 above causes to switch SLRU
    # bank, for additional code coverage.
    cur_primary.stop()
    cur_primary.start()
    nsubtrans = cur_primary.safe_sql(
        "select 'pg_subtrans/'||f, s.size from pg_ls_dir('pg_subtrans') f, "
        "pg_stat_file('pg_subtrans/'||f) s"
    )
    assert osubtrans != nsubtrans, "contents of pg_subtrans/ have changed"
