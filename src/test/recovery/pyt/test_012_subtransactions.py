# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests dedicated to subtransactions in recovery."""

# Function borrowed from src/test/regress/sql/hs_primary_extremes.sql
_HS_SUBXIDS = """
    CREATE OR REPLACE FUNCTION hs_subxids (n integer)
    RETURNS void
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF n <= 0 THEN RETURN; END IF;
        INSERT INTO t_012_tbl VALUES (n);
        PERFORM hs_subxids(n - 1);
        RETURN;
    EXCEPTION WHEN raise_exception THEN NULL; END;
    $$;"""


def test_012_subtransactions(create_pg):
    # Setup primary node
    node_primary = create_pg("primary", start=False, allows_streaming=True)
    node_primary.append_conf(
        """
	max_prepared_transactions = 10
	log_checkpoints = true
"""
    )
    node_primary.start()
    node_primary.backup("primary_backup")
    node_primary.safe_sql("CREATE TABLE t_012_tbl (id int)")

    # Setup standby node
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_primary, "primary_backup", has_streaming=True)
    node_standby.start()

    # Switch to synchronous replication
    node_primary.append_conf(
        """
	synchronous_standby_names = '*'
"""
    )
    node_primary.safe_sql("SELECT pg_reload_conf()")

    ###########################################################################
    # Check that replay will correctly set SUBTRANS and properly advance
    # nextXid so that it won't conflict with savepoint xids.
    ###########################################################################

    node_primary.safe_sql(
        """
	BEGIN;
	DELETE FROM t_012_tbl;
	INSERT INTO t_012_tbl VALUES (43);
	SAVEPOINT s1;
	INSERT INTO t_012_tbl VALUES (43);
	SAVEPOINT s2;
	INSERT INTO t_012_tbl VALUES (43);
	SAVEPOINT s3;
	INSERT INTO t_012_tbl VALUES (43);
	SAVEPOINT s4;
	INSERT INTO t_012_tbl VALUES (43);
	SAVEPOINT s5;
	INSERT INTO t_012_tbl VALUES (43);
	PREPARE TRANSACTION 'xact_012_1';
	CHECKPOINT;"""
    )

    node_primary.stop()
    node_primary.start()
    # here we can get xid of previous savepoint if nextXid
    # wasn't properly advanced.  COMMIT PREPARED must run outside a
    # transaction block, so it is issued as a separate query (libpq's
    # simple query protocol wraps a multi-statement string in one implicit
    # transaction, unlike psql which splits on ';').
    node_primary.safe_sql(
        """
	BEGIN;
	INSERT INTO t_012_tbl VALUES (142);
	ROLLBACK;"""
    )
    node_primary.safe_sql("COMMIT PREPARED 'xact_012_1';")

    psql_out = node_primary.safe_sql("SELECT count(*) FROM t_012_tbl")
    assert psql_out == "6", "Check nextXid handling for prepared subtransactions"

    ###########################################################################
    # Check that replay will correctly set 2PC with more than
    # PGPROC_MAX_CACHED_SUBXIDS subtransactions and also show data properly
    # on promotion
    ###########################################################################
    node_primary.safe_sql("DELETE FROM t_012_tbl")

    node_primary.safe_sql(_HS_SUBXIDS)
    node_primary.safe_sql(
        """
	BEGIN;
	SELECT hs_subxids(127);
	COMMIT;"""
    )
    node_primary.wait_for_catchup(node_standby)
    psql_out = node_standby.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "8128", "Visible"
    node_primary.stop()
    node_standby.promote()

    psql_out = node_standby.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "8128", "Visible"

    # restore state
    node_primary, node_standby = node_standby, node_primary
    node_standby.enable_streaming(node_primary)
    node_standby.start()
    psql_out = node_standby.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "8128", "Visible"

    node_primary.safe_sql("DELETE FROM t_012_tbl")

    node_primary.safe_sql(_HS_SUBXIDS)
    node_primary.safe_sql(
        """
	BEGIN;
	SELECT hs_subxids(127);
	PREPARE TRANSACTION 'xact_012_1';"""
    )
    node_primary.wait_for_catchup(node_standby)
    psql_out = node_standby.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "-1", "Not visible"
    node_primary.stop()
    node_standby.promote()

    psql_out = node_standby.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "-1", "Not visible"

    # restore state
    node_primary, node_standby = node_standby, node_primary
    node_standby.enable_streaming(node_primary)
    node_standby.start()
    # safe_sql raises on error, so a successful return mirrors psql_rc == 0
    node_primary.safe_sql("COMMIT PREPARED 'xact_012_1'")

    psql_out = node_primary.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "8128", "Visible"

    node_primary.safe_sql("DELETE FROM t_012_tbl")
    node_primary.safe_sql(
        """
	BEGIN;
	SELECT hs_subxids(201);
	PREPARE TRANSACTION 'xact_012_1';"""
    )
    node_primary.wait_for_catchup(node_standby)
    psql_out = node_standby.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "-1", "Not visible"
    node_primary.stop()
    node_standby.promote()

    psql_out = node_standby.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "-1", "Not visible"

    # restore state
    node_primary, node_standby = node_standby, node_primary
    node_standby.enable_streaming(node_primary)
    node_standby.start()
    # safe_sql raises on error, so a successful return mirrors psql_rc == 0
    node_primary.safe_sql("ROLLBACK PREPARED 'xact_012_1'")

    psql_out = node_primary.safe_sql("SELECT coalesce(sum(id),-1) FROM t_012_tbl")
    assert psql_out == "-1", "Not visible"
