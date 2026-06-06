# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test that a standby correctly handles relfilenode reuse after a database
is dropped and recreated.
"""

import re


def _disconnect_db(node, dbname):
    """Close and forget node's cached safe_sql session for *dbname*.

    safe_sql() keeps one long-lived connection per database.  This drops the
    cached connection so it no longer counts as a session using the
    database (e.g. for CREATE DATABASE ... TEMPLATE).
    """
    sess = node._sessions.pop(dbname, None)
    if sess is not None:
        sess.close()


def _verify(primary, standby, counter, message):
    """Check that the primary and (after catchup) the standby both report the
    expected single grouped row for the "datab" column.
    """
    query = "SELECT datab, count(*) FROM large GROUP BY 1 ORDER BY 1 LIMIT 10"
    # safe_sql caches one connection per database; on the standby that
    # connection may
    # have been terminated by a recovery conflict (DROP DATABASE replay), so
    # drop the cached sessions to force a clean reconnect.
    _disconnect_db(primary, "conflict_db")
    assert primary.safe_sql(query, dbname="conflict_db") == \
        f"{counter}|4000", f"primary: {message}"

    primary.wait_for_catchup(standby)
    _disconnect_db(standby, "conflict_db")
    assert standby.safe_sql(query, dbname="conflict_db") == \
        f"{counter}|4000", f"standby: {message}"


def _cause_eviction(psql_primary, psql_standby):
    """Run pg_prewarm work on the long-lived primary and standby sessions to
    write back dirty data and (re)open the relevant file descriptors.
    """
    prewarm = ("SELECT SUM(pg_prewarm(oid)) warmed_buffers FROM pg_class "
               "WHERE pg_relation_filenode(oid) != 0;")
    res = psql_primary.query(prewarm)
    assert res.error_message is None, res.error_message
    res = psql_standby.query(prewarm)
    assert res.error_message is None, res.error_message


def test_032_relfilenode_reuse(create_pg, pg_bin):
    node_primary = create_pg("primary", start=False, allows_streaming=True)
    node_primary.append_conf("""
allow_in_place_tablespaces = true
log_connections=receipt
# to avoid "repairing" corruption
full_page_writes=off
log_min_messages=debug2
shared_buffers=1MB
""")
    node_primary.start()

    # Create streaming standby linking to primary
    backup_name = "my_backup"
    node_primary.backup(backup_name)
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.start()

    # Create template database with a table that we'll update, to trigger dirty
    # rows. Using a template database + preexisting rows makes it a bit easier
    # to reproduce, because there's no cache invalidations generated.
    node_primary.safe_sql(
        "CREATE DATABASE conflict_db_template OID = 50000;")
    # Split non-transactional statements (each is one implicit txn).
    node_primary.safe_sql(
        "CREATE TABLE large(id serial primary key, dataa text, datab text);",
        dbname="conflict_db_template")
    node_primary.safe_sql(
        "INSERT INTO large(dataa, datab) SELECT g.i::text, 1 "
        "FROM generate_series(1, 4000) g(i);",
        dbname="conflict_db_template")
    # safe_sql() caches one connection per database.  Close the cached
    # template-db connection so it does not block CREATE DATABASE ... TEMPLATE.
    _disconnect_db(node_primary, "conflict_db_template")
    node_primary.safe_sql(
        "CREATE DATABASE conflict_db TEMPLATE conflict_db_template OID = 50001;")

    node_primary.safe_sql("CREATE EXTENSION pg_prewarm;")
    node_primary.safe_sql("CREATE TABLE replace_sb(data text);")
    node_primary.safe_sql(
        "INSERT INTO replace_sb(data) SELECT random()::text "
        "FROM generate_series(1, 15000);")

    node_primary.wait_for_catchup(node_standby)

    # Long-lived sessions to primary and standby; using longrunning
    # transactions means AtEOXact_SMgr doesn't close files.
    psql_primary = node_primary.connect("postgres")
    psql_standby = node_standby.connect("postgres")
    try:
        assert psql_primary.do("BEGIN") is not None, "BEGIN"
        assert psql_standby.do("BEGIN") is not None, "BEGIN"

        # Cause lots of dirty rows in shared_buffers
        node_primary.safe_sql("UPDATE large SET datab = 1;",
                               dbname="conflict_db")

        # Now do a bunch of work in another database. That will end up needing
        # to write back dirty data from the previous step, opening the relevant
        # file descriptors
        _cause_eviction(psql_primary, psql_standby)

        # drop and recreate database
        _disconnect_db(node_primary, "conflict_db")
        node_primary.safe_sql("DROP DATABASE conflict_db;")
        node_primary.safe_sql(
            "CREATE DATABASE conflict_db TEMPLATE conflict_db_template "
            "OID = 50001;")

        _verify(node_primary, node_standby, 1, "initial contents as expected")

        # Again cause lots of dirty rows in shared_buffers, but use a different
        # update value so we can check everything is OK
        node_primary.safe_sql("UPDATE large SET datab = 2;",
                               dbname="conflict_db")

        # Again cause a lot of IO. That'll again write back dirty data, but
        # uses newly opened file descriptors, so we don't confuse old files
        # with new files despite recycling relfilenodes.
        _cause_eviction(psql_primary, psql_standby)

        _verify(node_primary, node_standby, 2,
                "update to reused relfilenode (due to DB oid conflict) "
                "is not lost")

        node_primary.safe_sql("VACUUM FULL large;", dbname="conflict_db")
        node_primary.safe_sql("UPDATE large SET datab = 3;",
                               dbname="conflict_db")

        _verify(node_primary, node_standby, 3,
                "restored contents as expected")

        # Test for old filehandles after moving a database in / out of
        # tablespace
        node_primary.safe_sql(
            "CREATE TABLESPACE test_tablespace LOCATION ''")

        # cause dirty buffers
        node_primary.safe_sql("UPDATE large SET datab = 4;",
                               dbname="conflict_db")
        # cause files to be opened in backend in other database
        _cause_eviction(psql_primary, psql_standby)

        # move database back / forth (ALTER DATABASE SET TABLESPACE needs no
        # connection to the target database)
        _disconnect_db(node_primary, "conflict_db")
        node_primary.safe_sql(
            "ALTER DATABASE conflict_db SET TABLESPACE test_tablespace")
        node_primary.safe_sql(
            "ALTER DATABASE conflict_db SET TABLESPACE pg_default")

        # cause dirty buffers
        node_primary.safe_sql("UPDATE large SET datab = 5;",
                               dbname="conflict_db")
        _cause_eviction(psql_primary, psql_standby)

        _verify(node_primary, node_standby, 5,
                "post move contents as expected")

        _disconnect_db(node_primary, "conflict_db")
        node_primary.safe_sql(
            "ALTER DATABASE conflict_db SET TABLESPACE test_tablespace")

        node_primary.safe_sql("UPDATE large SET datab = 7;",
                               dbname="conflict_db")
        _cause_eviction(psql_primary, psql_standby)
        node_primary.safe_sql("UPDATE large SET datab = 8;",
                               dbname="conflict_db")
        _disconnect_db(node_primary, "conflict_db")
        node_primary.safe_sql("DROP DATABASE conflict_db")
        node_primary.safe_sql("DROP TABLESPACE test_tablespace")

        node_primary.safe_sql("REINDEX TABLE pg_database")
    finally:
        # explicitly close the long-lived sessions gracefully
        psql_primary.close()
        psql_standby.close()

    node_primary.stop()
    node_standby.stop()

    # Make sure that there weren't crashes during shutdown
    res = pg_bin.result(["pg_controldata", node_primary.data_dir])
    assert re.search(r"Database cluster state:\s+shut down\n", res.stdout), \
        "primary shut down ok"
    res = pg_bin.result(["pg_controldata", node_standby.data_dir])
    assert re.search(
        r"Database cluster state:\s+shut down in recovery\n", res.stdout), \
        "standby shut down ok"
