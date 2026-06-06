# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Logical replication tests for various bugs found over time."""

import re

from libpq import Session


def test_bug_15114_index_predicate_crash(create_pg):
    # Bug #15114
    #
    # The bug was that determining which columns are part of the replica
    # identity index using RelationGetIndexAttrBitmap() would run
    # eval_const_expressions() on index expressions and predicates across
    # all indexes of the table, which in turn might require a snapshot,
    # but there wasn't one set, so it crashes.  There were actually two
    # separate bugs, one on the publisher and one on the subscriber.  The
    # fix was to avoid the constant expressions simplification in
    # RelationGetIndexAttrBitmap(), so it's safe to call in more contexts.

    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    node_publisher.safe_sql("CREATE TABLE tab1 (a int PRIMARY KEY, b int)")

    node_publisher.safe_sql(
        "CREATE FUNCTION double(x int) RETURNS int IMMUTABLE LANGUAGE SQL "
        "AS 'select x * 2'"
    )

    # an index with a predicate that lends itself to constant expressions
    # evaluation
    node_publisher.safe_sql("CREATE INDEX ON tab1 (b) WHERE a > double(1)")

    # and the same setup on the subscriber
    node_subscriber.safe_sql("CREATE TABLE tab1 (a int PRIMARY KEY, b int)")

    node_subscriber.safe_sql(
        "CREATE FUNCTION double(x int) RETURNS int IMMUTABLE LANGUAGE SQL "
        "AS 'select x * 2'"
    )

    node_subscriber.safe_sql("CREATE INDEX ON tab1 (b) WHERE a > double(1)")

    node_publisher.safe_sql("CREATE PUBLICATION pub1 FOR ALL TABLES")

    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' " "PUBLICATION pub1"
    )

    node_publisher.wait_for_catchup("sub1")

    # This would crash, first on the publisher, and then (if the publisher
    # is fixed) on the subscriber.
    node_publisher.safe_sql("INSERT INTO tab1 VALUES (1, 2)")

    node_publisher.wait_for_catchup("sub1")

    # pass('index predicates do not cause crash')


def test_temp_and_unlogged_tables_for_all_tables(create_pg):
    # Handling of temporary and unlogged tables with FOR ALL TABLES
    # publications
    #
    # If a FOR ALL TABLES publication exists, temporary and unlogged
    # tables are ignored for publishing changes.  The bug was that we
    # would still check in that case that such a table has a replica
    # identity set before accepting updates.  If it did not it would cause
    # an error when an update was attempted.

    node_publisher = create_pg("publisher", allows_streaming="logical")

    node_publisher.safe_sql("CREATE PUBLICATION pub FOR ALL TABLES")

    # update to temporary table without replica identity with FOR ALL TABLES
    # publication
    node_publisher.safe_sql(
        "CREATE TEMPORARY TABLE tt1 AS SELECT 1 AS a; UPDATE tt1 SET a = 2;"
    )

    # update to unlogged table without replica identity with FOR ALL TABLES
    # publication
    node_publisher.safe_sql(
        "CREATE UNLOGGED TABLE tu1 AS SELECT 1 AS a; UPDATE tu1 SET a = 2;"
    )


def test_bug_16643_initial_sync(create_pg):
    # Bug #16643 - https://postgr.es/m/16643-eaadeb2a1a58d28c@postgresql.org
    #
    # Initial sync doesn't complete; the protocol was not being followed per
    # expectations after commit 07082b08cc5d.
    node_twoways = create_pg("twoways", allows_streaming="logical")
    for db in ("d1", "d2"):
        node_twoways.safe_sql(f"CREATE DATABASE {db}")
        node_twoways.safe_sql("CREATE TABLE t (f int)", dbname=db)
        node_twoways.safe_sql("CREATE TABLE t2 (f int)", dbname=db)

    rows = 3000
    # pg_create_logical_replication_slot cannot run in a transaction that has
    # performed writes, so issue each statement separately (psql autocommits
    # each statement, but our multi-statement safe_sql is one implicit
    # transaction).
    node_twoways.safe_sql(
        f"INSERT INTO t SELECT * FROM generate_series(1, {rows})", dbname="d1"
    )
    node_twoways.safe_sql(
        f"INSERT INTO t2 SELECT * FROM generate_series(1, {rows})", dbname="d1"
    )
    node_twoways.safe_sql("CREATE PUBLICATION testpub FOR TABLE t", dbname="d1")
    node_twoways.safe_sql(
        "SELECT pg_create_logical_replication_slot('testslot', 'pgoutput')", dbname="d1"
    )

    d1_connstr = f"host={node_twoways.host} port={node_twoways.port} dbname=d1"
    node_twoways.safe_sql(
        f"CREATE SUBSCRIPTION testsub CONNECTION '{d1_connstr}' "
        "PUBLICATION testpub WITH (create_slot=false, slot_name='testslot')",
        dbname="d2",
    )
    node_twoways.safe_sql(
        f"""
        INSERT INTO t SELECT * FROM generate_series(1, {rows});
        INSERT INTO t2 SELECT * FROM generate_series(1, {rows});
        """,
        dbname="d1",
    )
    node_twoways.safe_sql("ALTER PUBLICATION testpub ADD TABLE t2", dbname="d1")
    node_twoways.safe_sql("ALTER SUBSCRIPTION testsub REFRESH PUBLICATION", dbname="d2")

    # We cannot rely solely on wait_for_catchup() here; it isn't sufficient
    # when tablesync workers might still be running. So in addition to that,
    # verify that tables are synced.
    node_twoways.wait_for_subscription_sync(node_twoways, "testsub", "d2")

    result = node_twoways.safe_sql("SELECT count(f) FROM t", dbname="d2")
    assert result == str(rows * 2), f"2x{rows} rows in t"
    result = node_twoways.safe_sql("SELECT count(f) FROM t2", dbname="d2")
    assert result == str(rows * 2), f"2x{rows} rows in t2"


def test_cascaded_replication_tablesync(create_pg):
    # Verify table data is synced with cascaded replication setup. This is
    # mainly to test whether the data written by tablesync worker gets
    # replicated.
    node_pub = create_pg("testpublisher1", allows_streaming="logical")
    node_pub_sub = create_pg("testpublisher_subscriber", allows_streaming="logical")
    node_sub = create_pg("testsubscriber1")

    # Create the tables in all nodes.
    node_pub.safe_sql("CREATE TABLE tab1 (a int)")
    node_pub_sub.safe_sql("CREATE TABLE tab1 (a int)")
    node_sub.safe_sql("CREATE TABLE tab1 (a int)")

    # Create a cascaded replication setup like:
    # N1 - Create publication testpub1.
    # N2 - Create publication testpub2 and also include subscriber which
    #      subscribes to testpub1.
    # N3 - Create subscription testsub2 subscribes to testpub2.
    #
    # Note that subscription on N3 needs to be created before subscription on
    # N2 to test whether the data written by tablesync worker of N2 gets
    # replicated.
    node_pub.safe_sql("CREATE PUBLICATION testpub1 FOR TABLE tab1")

    node_pub_sub.safe_sql("CREATE PUBLICATION testpub2 FOR TABLE tab1")

    publisher1_connstr = f"host={node_pub.host} port={node_pub.port} dbname=postgres"
    publisher2_connstr = (
        f"host={node_pub_sub.host} port={node_pub_sub.port} dbname=postgres"
    )

    node_sub.safe_sql(
        f"CREATE SUBSCRIPTION testsub2 CONNECTION '{publisher2_connstr}' "
        "PUBLICATION testpub2"
    )

    node_pub_sub.safe_sql(
        f"CREATE SUBSCRIPTION testsub1 CONNECTION '{publisher1_connstr}' "
        "PUBLICATION testpub1"
    )

    node_pub.safe_sql("INSERT INTO tab1 values(generate_series(1,10))")

    # Verify that the data is cascaded from testpub1 to testsub1 and further
    # from testpub2 (which had testsub1) to testsub2.
    node_pub.wait_for_catchup("testsub1")
    node_pub_sub.wait_for_catchup("testsub2")

    # Drop subscriptions as we don't need them anymore
    node_pub_sub.safe_sql("DROP SUBSCRIPTION testsub1")
    node_sub.safe_sql("DROP SUBSCRIPTION testsub2")

    # Drop publications as we don't need them anymore
    node_pub.safe_sql("DROP PUBLICATION testpub1")
    node_pub_sub.safe_sql("DROP PUBLICATION testpub2")

    # Clean up the tables on both publisher and subscriber as we don't need them
    node_pub.safe_sql("DROP TABLE tab1")
    node_pub_sub.safe_sql("DROP TABLE tab1")
    node_sub.safe_sql("DROP TABLE tab1")


def test_replica_identity_index_change(create_pg):
    # https://postgr.es/m/OS0PR01MB61133CA11630DAE45BC6AD95FB939%40OS0PR01MB6113.jpnprd01.prod.outlook.com
    #
    # The bug was that when changing the REPLICA IDENTITY INDEX to another one,
    # the target table's relcache was not being invalidated. This leads to
    # skipping UPDATE/DELETE operations during apply on the subscriber side as
    # the columns required to search corresponding rows won't get logged.

    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    node_publisher.safe_sql(
        "CREATE TABLE tab_replidentity_index(a int not null, b int not null)"
    )
    node_publisher.safe_sql(
        "CREATE UNIQUE INDEX idx_replidentity_index_a ON tab_replidentity_index(a)"
    )
    node_publisher.safe_sql(
        "CREATE UNIQUE INDEX idx_replidentity_index_b ON tab_replidentity_index(b)"
    )

    # use index idx_replidentity_index_a as REPLICA IDENTITY on publisher.
    node_publisher.safe_sql(
        "ALTER TABLE tab_replidentity_index REPLICA IDENTITY "
        "USING INDEX idx_replidentity_index_a"
    )

    node_publisher.safe_sql("INSERT INTO tab_replidentity_index VALUES(1, 1),(2, 2)")

    node_subscriber.safe_sql(
        "CREATE TABLE tab_replidentity_index(a int not null, b int not null)"
    )
    node_subscriber.safe_sql(
        "CREATE UNIQUE INDEX idx_replidentity_index_a ON tab_replidentity_index(a)"
    )
    node_subscriber.safe_sql(
        "CREATE UNIQUE INDEX idx_replidentity_index_b ON tab_replidentity_index(b)"
    )
    # use index idx_replidentity_index_b as REPLICA IDENTITY on subscriber
    # because it reflects the future scenario we are testing: changing REPLICA
    # IDENTITY INDEX.
    node_subscriber.safe_sql(
        "ALTER TABLE tab_replidentity_index REPLICA IDENTITY "
        "USING INDEX idx_replidentity_index_b"
    )

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql(
        "CREATE PUBLICATION tap_pub FOR TABLE tab_replidentity_index"
    )
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub"
    )

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "tap_sub")

    result = node_subscriber.safe_sql("SELECT * FROM tab_replidentity_index")
    assert result == "1|1\n2|2", "check initial data on subscriber"

    # Set REPLICA IDENTITY to idx_replidentity_index_b on publisher, then run
    # UPDATE and DELETE.
    node_publisher.safe_sql(
        """
        ALTER TABLE tab_replidentity_index REPLICA IDENTITY USING INDEX idx_replidentity_index_b;
        UPDATE tab_replidentity_index SET a = -a WHERE a = 1;
        DELETE FROM tab_replidentity_index WHERE a = 2;
    """
    )

    node_publisher.wait_for_catchup("tap_sub")
    result = node_subscriber.safe_sql("SELECT * FROM tab_replidentity_index")
    assert result == "-1|1", "update works with REPLICA IDENTITY"


def test_schema_invalidation_by_rename(create_pg):
    # Test schema invalidation by renaming the schema
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    # Create tables on publisher
    node_publisher.safe_sql("CREATE SCHEMA sch1")
    node_publisher.safe_sql("CREATE TABLE sch1.t1 (c1 int)")

    # Create tables on subscriber
    node_subscriber.safe_sql("CREATE SCHEMA sch1")
    node_subscriber.safe_sql("CREATE TABLE sch1.t1 (c1 int)")
    node_subscriber.safe_sql("CREATE SCHEMA sch2")
    node_subscriber.safe_sql("CREATE TABLE sch2.t1 (c1 int)")

    # Setup logical replication that will cover t1 under both schema names
    node_publisher.safe_sql("CREATE PUBLICATION tap_pub_sch FOR ALL TABLES")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub_sch CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub_sch"
    )

    # Wait for initial table sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "tap_sub_sch")

    # Check what happens to data inserted before and after schema rename
    node_publisher.safe_sql(
        """begin;
insert into sch1.t1 values(1);
alter schema sch1 rename to sch2;
create schema sch1;
create table sch1.t1(c1 int);
insert into sch1.t1 values(2);
insert into sch2.t1 values(3);
commit;"""
    )

    node_subscriber.wait_for_subscription_sync(node_publisher, "tap_sub_sch")

    # Subscriber's sch1.t1 should receive the row inserted into the new sch1.t1,
    # but not the row inserted into the old sch1.t1 post-rename.
    result = node_subscriber.safe_sql("SELECT * FROM sch1.t1")
    assert result == "1\n2", "check data in subscriber sch1.t1 after schema rename"

    # Subscriber's sch2.t1 won't have gotten anything yet ...
    result = node_subscriber.safe_sql("SELECT * FROM sch2.t1")
    assert result == "", "no data yet in subscriber sch2.t1 after schema rename"

    # ... but it should show up after REFRESH.
    node_subscriber.safe_sql("ALTER SUBSCRIPTION tap_sub_sch REFRESH PUBLICATION")

    node_subscriber.wait_for_subscription_sync(node_publisher, "tap_sub_sch")

    result = node_subscriber.safe_sql("SELECT * FROM sch2.t1")
    assert result == "1\n3", "check data in subscriber sch2.t1 after schema rename"


def test_replica_identity_full_dropped_columns(create_pg):
    # The bug was that when the REPLICA IDENTITY FULL is used with dropped
    # we fail to apply updates and deletes
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    node_publisher.safe_sql(
        """
        CREATE TABLE dropped_cols (a int, b_drop int, c int);
        ALTER TABLE dropped_cols REPLICA IDENTITY FULL;
        CREATE PUBLICATION pub_dropped_cols FOR TABLE dropped_cols;
        -- some initial data
        INSERT INTO dropped_cols VALUES (1, 1, 1);
    """
    )

    node_subscriber.safe_sql(
        """
         CREATE TABLE dropped_cols (a int, b_drop int, c int);
    """
    )

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub_dropped_cols CONNECTION "
        f"'{publisher_connstr}' PUBLICATION pub_dropped_cols"
    )
    node_subscriber.wait_for_subscription_sync()

    node_publisher.safe_sql(
        """
            ALTER TABLE dropped_cols DROP COLUMN b_drop;
    """
    )
    node_subscriber.safe_sql(
        """
            ALTER TABLE dropped_cols DROP COLUMN b_drop;
    """
    )

    node_publisher.safe_sql(
        """
            UPDATE dropped_cols SET a = 100;
    """
    )
    node_publisher.wait_for_catchup("sub_dropped_cols")

    result = node_subscriber.safe_sql("SELECT count(*) FROM dropped_cols WHERE a = 100")
    assert result == "1", "replication with RI FULL and dropped columns"


def test_pgoutput_missing_attributes_and_slot_drop(create_pg):
    # The bug was that pgoutput was incorrectly replacing missing attributes in
    # tuples with NULL. This could result in incorrect replication with
    # `REPLICA IDENTITY FULL`.
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    # Set up a table with schema `(a int, b bool)` where the `b` attribute is
    # missing for one row due to the `ALTER TABLE ... ADD COLUMN ... DEFAULT`
    # fast path.
    node_publisher.safe_sql(
        """
        CREATE TABLE tab_default (a int);
        ALTER TABLE tab_default REPLICA IDENTITY FULL;
        INSERT INTO tab_default VALUES (1);
        ALTER TABLE tab_default ADD COLUMN b bool DEFAULT false NOT NULL;
        INSERT INTO tab_default VALUES (2, true);
        CREATE PUBLICATION pub1 FOR TABLE tab_default;
    """
    )

    # Replicate to the subscriber.  CREATE SUBSCRIPTION (create_slot=true)
    # cannot run inside a transaction block, so split it from the CREATE TABLE.
    node_subscriber.safe_sql("CREATE TABLE tab_default (a int, b bool)")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' " "PUBLICATION pub1"
    )

    node_subscriber.wait_for_subscription_sync(node_publisher, "sub1")
    result = node_subscriber.safe_sql("SELECT a, b FROM tab_default")
    assert result == "1|f\n2|t", "check snapshot on subscriber"

    # Update all rows in the table and ensure the rows with the missing `b`
    # attribute replicate correctly.
    node_publisher.safe_sql("UPDATE tab_default SET a = a + 1")
    node_publisher.wait_for_catchup("sub1")

    # When the bug is present, the `1|f` row will not be updated to `2|f`
    # because the publisher incorrectly fills in `NULL` for `b` and publishes
    # an update for `1|NULL`, which doesn't exist in the subscriber.
    result = node_subscriber.safe_sql("SELECT a, b FROM tab_default")
    assert result == "2|f\n3|t", "check replicated update on subscriber"

    # Test create and immediate drop of replication slot via replication
    # commands (this exposed a memory-management bug in v18)
    publisher_host = node_publisher.host
    publisher_port = node_publisher.port
    connstr_db = (
        f"host={publisher_host} port={publisher_port} "
        "replication=database dbname=postgres"
    )

    sess = Session(connstr=connstr_db, libdir=node_publisher.libdir)
    try:
        res = sess.query(
            "CREATE_REPLICATION_SLOT test_slot LOGICAL pgoutput (SNAPSHOT export)"
        )
        assert (
            res.error_message is None
        ), f"create replication slot: {res.error_message}"
        res = sess.query("DROP_REPLICATION_SLOT test_slot")
        assert res.error_message is None, f"drop replication slot: {res.error_message}"
    finally:
        sess.close()
    # 'create and immediate drop of replication slot'


def test_apply_worker_origin_advance_after_exception(create_pg):
    # The bug was that when an ERROR was caught and handled by a (PL/pgSQL)
    # function, the apply worker reset the replication origin but continued
    # processing subsequent changes. So, we fail to update the replication
    # origin during further apply operations. This can lead to the apply
    # worker requesting the changes that have been applied again after
    # restarting.
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    # Set up a publication with a table
    node_publisher.safe_sql(
        """
        CREATE TABLE t1 (a int);
        CREATE PUBLICATION regress_pub FOR TABLE t1;
    """
    )

    # Set up a subscription which subscribes the publication.  CREATE
    # SUBSCRIPTION (create_slot=true) cannot run inside a transaction block, so
    # split it from the CREATE TABLE.
    node_subscriber.safe_sql("CREATE TABLE t1 (a int)")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION regress_pub"
    )

    node_subscriber.wait_for_subscription_sync(node_publisher, "regress_sub")

    # Create an AFTER INSERT trigger on the table that raises and subsequently
    # handles an exception. Subsequent insertions will trigger this exception,
    # causing the apply worker to invoke its error callback with an ERROR.
    # However, since the error is caught within the trigger, the apply worker
    # will continue processing changes.
    node_subscriber.safe_sql(
        r"""
CREATE FUNCTION handle_exception_trigger()
RETURNS TRIGGER AS $$
BEGIN
	BEGIN
		-- Raise an exception
		RAISE EXCEPTION 'This is a test exception';
	EXCEPTION
		WHEN OTHERS THEN
			RETURN NEW;
	END;

	RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER silent_exception_trigger
AFTER INSERT OR UPDATE ON t1
FOR EACH ROW
EXECUTE FUNCTION handle_exception_trigger();

ALTER TABLE t1 ENABLE ALWAYS TRIGGER silent_exception_trigger;
"""
    )

    # Obtain current remote_lsn value to check its advancement later
    remote_lsn = node_subscriber.safe_sql(
        "SELECT remote_lsn FROM pg_replication_origin_status os, "
        "pg_subscription s WHERE os.external_id = 'pg_' || s.oid "
        "AND s.subname = 'regress_sub'"
    )

    # Insert a tuple to replicate changes
    node_publisher.safe_sql("INSERT INTO t1 VALUES (1);")
    node_publisher.wait_for_catchup("regress_sub")

    # Confirms the origin can be advanced
    result = node_subscriber.safe_sql(
        f"SELECT remote_lsn > '{remote_lsn}' FROM "
        "pg_replication_origin_status os, pg_subscription s "
        "WHERE os.external_id = 'pg_' || s.oid AND s.subname = 'regress_sub'"
    )
    assert (
        result == "t"
    ), "remote_lsn has advanced for apply worker raising an exception"


def test_bug_18988_drop_subscription_deadlock(create_pg):
    # BUG #18988
    # The bug happened due to a self-deadlock between the DROP SUBSCRIPTION
    # command and the walsender process for accessing pg_subscription. This
    # occurred when DROP SUBSCRIPTION attempted to remove a replication slot by
    # connecting to a newly created database whose caches are not yet
    # initialized.
    #
    # The bug is fixed by reducing the lock-level during DROP SUBSCRIPTION.
    node_publisher = create_pg("publisher", allows_streaming="logical")

    # A publication is referenced by the (non-connecting) subscription.
    node_publisher.safe_sql(
        "CREATE TABLE t1 (a int); CREATE PUBLICATION regress_pub FOR TABLE t1"
    )

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} " "dbname=regress_db"
    )
    # CREATE DATABASE cannot run inside a transaction block, so split it from
    # the CREATE SUBSCRIPTION.
    node_publisher.safe_sql("CREATE DATABASE regress_db")
    node_publisher.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub1 CONNECTION '{publisher_connstr}' "
        "PUBLICATION regress_pub WITH (connect=false)"
    )

    res = node_publisher.sql("DROP SUBSCRIPTION regress_sub1")

    assert (
        res.error_message is not None
    ), "replication slot does not exist: exit code not 0"
    assert re.search(
        r'ERROR:  could not drop replication slot "regress_sub1" on publisher',
        res.error_message,
    ), "could not drop replication slot: error message"

    node_publisher.safe_sql("DROP DATABASE regress_db")
