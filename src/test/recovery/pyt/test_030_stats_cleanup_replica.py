# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests that standbys:

- drop stats for objects when the those records are replayed
- persist stats across graceful restarts
- discard stats after immediate / crash restarts
"""


def _close_cached_session(node, dbname):
    # Close (and forget) the cached libpq session for *dbname*, so it no
    # longer holds a persistent backend on that database.
    sess = node._sessions.pop(dbname, None)
    if sess is not None:
        sess.close()


def _populate_standby_stats(node_primary, node_standby, connect_db, schema):
    # create objects on primary
    node_primary.safe_sql(
        f"CREATE TABLE {schema}.drop_tab_test1 AS "
        "SELECT generate_series(1,100) AS a",
        connect_db,
    )
    node_primary.safe_sql(
        f"CREATE FUNCTION {schema}.drop_func_test1() RETURNS VOID AS "
        "'select 2;' LANGUAGE SQL IMMUTABLE",
        connect_db,
    )
    node_primary.wait_for_replay_catchup(node_standby)

    # collect object oids
    dboid = node_standby.safe_sql(
        f"SELECT oid FROM pg_database WHERE datname = '{connect_db}'",
        connect_db,
    )
    tableoid = node_standby.safe_sql(
        f"SELECT '{schema}.drop_tab_test1'::regclass::oid", connect_db
    )
    funcoid = node_standby.safe_sql(
        f"SELECT '{schema}.drop_func_test1()'::regprocedure::oid", connect_db
    )

    # Generate stats on standby.  This framework keeps one long-lived
    # backend per database.  A backend that
    # has referenced an object pins its stats entry, so a later replayed drop
    # can only mark the entry dropped (not free it) and pg_stat_have_stats()
    # on that same backend would still report the object as present.  Run the
    # stats-generating queries on a throwaway connection that is closed
    # immediately, mirroring the per-statement fresh connection semantics.
    with node_standby.connect(connect_db) as gen:
        gen.query_safe(f"SELECT * FROM {schema}.drop_tab_test1")
        gen.query_safe(f"SELECT {schema}.drop_func_test1()")

    return dboid, tableoid, funcoid


def _drop_function_by_oid(node_primary, connect_db, funcoid):
    # Get function name from returned oid
    func_name = node_primary.safe_sql(
        f"SELECT '{funcoid}'::regprocedure", connect_db
    )
    node_primary.safe_sql(f"DROP FUNCTION {func_name}", connect_db)


def _drop_table_by_oid(node_primary, connect_db, tableoid):
    # Get table name from returned oid
    table_name = node_primary.safe_sql(
        f"SELECT '{tableoid}'::regclass", connect_db
    )
    node_primary.safe_sql(f"DROP TABLE {table_name}", connect_db)


def _test_standby_func_tab_stats_status(
    node_standby, sect, connect_db, dboid, tableoid, funcoid, present
):
    expected = {"rel": present, "func": present}
    stats = {}

    stats["rel"] = node_standby.safe_sql(
        f"SELECT pg_stat_have_stats('relation', {dboid}, {tableoid})", connect_db
    )
    stats["func"] = node_standby.safe_sql(
        f"SELECT pg_stat_have_stats('function', {dboid}, {funcoid})", connect_db
    )

    assert stats == expected, f"{sect}: standby stats as expected"


def _test_standby_db_stats_status(node_standby, sect, connect_db, dboid, present):
    assert (
        node_standby.safe_sql(
            f"SELECT pg_stat_have_stats('database', {dboid}, 0)", connect_db
        )
        == present
    ), f"{sect}: standby db stats as expected"


def test_030_stats_cleanup_replica(create_pg):
    node_primary = create_pg("primary", start=False, allows_streaming=True)
    node_primary.append_conf("track_functions = 'all'")
    node_primary.start()

    backup_name = "my_backup"
    node_primary.backup(backup_name)

    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.start()

    # Test that stats are cleaned up on standby after dropping table or function

    sect = "initial"

    dboid, tableoid, funcoid = _populate_standby_stats(
        node_primary, node_standby, "postgres", "public"
    )
    _test_standby_func_tab_stats_status(
        node_standby, sect, "postgres", dboid, tableoid, funcoid, "t"
    )

    _drop_table_by_oid(node_primary, "postgres", tableoid)
    _drop_function_by_oid(node_primary, "postgres", funcoid)

    sect = "post drop"
    node_primary.wait_for_replay_catchup(node_standby)
    _test_standby_func_tab_stats_status(
        node_standby, sect, "postgres", dboid, tableoid, funcoid, "f"
    )

    # Test that stats are cleaned up on standby after dropping indirectly

    sect = "schema creation"

    node_primary.safe_sql("CREATE SCHEMA drop_schema_test1", "postgres")
    node_primary.wait_for_replay_catchup(node_standby)

    dboid, tableoid, funcoid = _populate_standby_stats(
        node_primary, node_standby, "postgres", "drop_schema_test1"
    )

    _test_standby_func_tab_stats_status(
        node_standby, sect, "postgres", dboid, tableoid, funcoid, "t"
    )
    node_primary.safe_sql("DROP SCHEMA drop_schema_test1 CASCADE", "postgres")

    sect = "post schema drop"

    node_primary.wait_for_replay_catchup(node_standby)

    # verify table and function stats removed from standby
    _test_standby_func_tab_stats_status(
        node_standby, sect, "postgres", dboid, tableoid, funcoid, "f"
    )

    # Test that stats are cleaned up on standby after dropping database

    sect = "createdb"

    node_primary.safe_sql("CREATE DATABASE test", "postgres")
    node_primary.wait_for_replay_catchup(node_standby)

    dboid, tableoid, funcoid = _populate_standby_stats(
        node_primary, node_standby, "test", "public"
    )

    # verify stats are present
    _test_standby_func_tab_stats_status(
        node_standby, sect, "test", dboid, tableoid, funcoid, "t"
    )
    _test_standby_db_stats_status(node_standby, sect, "test", dboid, "t")

    # This framework caches one long-lived session per database, so close
    # both nodes' connections to "test" before dropping
    # it; otherwise DROP DATABASE fails with "database is being accessed by
    # other users".
    _close_cached_session(node_standby, "test")
    _close_cached_session(node_primary, "test")

    node_primary.safe_sql("DROP DATABASE test", "postgres")
    sect = "post dropdb"
    node_primary.wait_for_replay_catchup(node_standby)

    # Test that the stats were cleaned up on standby
    # Note that this connects to 'postgres' but provides the dboid of dropped db
    # 'test' which we acquired previously
    _test_standby_func_tab_stats_status(
        node_standby, sect, "postgres", dboid, tableoid, funcoid, "f"
    )

    _test_standby_db_stats_status(node_standby, sect, "postgres", dboid, "f")

    # verify that stats persist across graceful restarts on a replica

    # NB: Can't test database stats, they're immediately repopulated when
    # reconnecting...
    sect = "pre restart"
    dboid, tableoid, funcoid = _populate_standby_stats(
        node_primary, node_standby, "postgres", "public"
    )
    _test_standby_func_tab_stats_status(
        node_standby, sect, "postgres", dboid, tableoid, funcoid, "t"
    )

    node_standby.restart()

    sect = "post non-immediate"

    _test_standby_func_tab_stats_status(
        node_standby, sect, "postgres", dboid, tableoid, funcoid, "t"
    )

    # but gone after an immediate restart
    node_standby.stop("immediate")
    node_standby.start()

    sect = "post immediate restart"

    _test_standby_func_tab_stats_status(
        node_standby, sect, "postgres", dboid, tableoid, funcoid, "f"
    )
