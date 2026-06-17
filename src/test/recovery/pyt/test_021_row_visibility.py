# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Checks that snapshots on standbys behave in a minimally reasonable way."""


def _standby_rows(session):
    """Return the rows of "SELECT * FROM test_visibility ORDER BY data".

    Runs the standby query repeatedly and returns a list of single-column
    values (the "data" column).
    """
    res = session.query("SELECT * FROM test_visibility ORDER BY data")
    assert res.error_message is None, res.error_message
    return [row[0] for row in res.rows]


def test_021_row_visibility(create_pg):
    # Initialize primary node
    node_primary = create_pg("primary", start=False, allows_streaming=True)
    node_primary.append_conf("max_prepared_transactions=10")
    node_primary.start()

    # Initialize with empty test table
    node_primary.safe_sql("CREATE TABLE public.test_visibility (data text not null)")

    # Take backup
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Create streaming standby from backup
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.append_conf("max_prepared_transactions=10")
    node_standby.start()

    # One libpq session to primary and standby each, for all queries. That
    # allows to check uncommitted changes being replicated and such.  These
    # long-lived connections carry the transaction control
    # (BEGIN/COMMIT/PREPARE TRANSACTION) issued by the test.
    psql_primary = node_primary.connect("postgres")
    psql_standby = node_standby.connect("postgres")
    try:
        #
        # 1. Check initial data is the same
        #
        assert _standby_rows(psql_standby) == [], "data not visible"

        #
        # 2. Check if an INSERT is replayed and visible
        #
        node_primary.safe_sql("INSERT INTO test_visibility VALUES ('first insert')")
        node_primary.wait_for_catchup(node_standby)

        assert _standby_rows(psql_standby) == ["first insert"], "insert visible"

        #
        # 3. Verify that uncommitted changes aren't visible.
        #
        res = psql_primary.query(
            "BEGIN;\n"
            "UPDATE test_visibility SET data = 'first update' RETURNING data;"
        )
        assert res.error_message is None, res.error_message
        assert res.psqlout == "first update", "UPDATE"

        node_primary.safe_sql("SELECT txid_current()")  # ensure WAL flush
        node_primary.wait_for_catchup(node_standby)

        assert _standby_rows(psql_standby) == [
            "first insert"
        ], "uncommitted update invisible"

        #
        # 4. That a commit turns 3. visible
        #
        assert psql_primary.do("COMMIT") is not None, "COMMIT"

        node_primary.wait_for_catchup(node_standby)

        assert _standby_rows(psql_standby) == [
            "first update"
        ], "committed update visible"

        #
        # 5. Check that changes in prepared xacts is invisible
        #
        # delete old data, so we start with clean slate
        assert psql_primary.do("DELETE from test_visibility") is not None, "DELETE"
        res = psql_primary.query(
            "BEGIN;"
            "\nINSERT INTO test_visibility"
            " VALUES('inserted in prepared will_commit');"
            "\nPREPARE TRANSACTION 'will_commit';"
        )
        assert res.error_message is None, res.error_message

        res = psql_primary.query(
            "BEGIN;"
            "\nINSERT INTO test_visibility"
            " VALUES('inserted in prepared will_abort');"
            "\nPREPARE TRANSACTION 'will_abort';"
        )
        assert res.error_message is None, res.error_message

        node_primary.wait_for_catchup(node_standby)

        assert _standby_rows(psql_standby) == [], "uncommitted prepared invisible"

        # For some variation, finish prepared xacts via separate connections
        node_primary.safe_sql("COMMIT PREPARED 'will_commit';")
        node_primary.safe_sql("ROLLBACK PREPARED 'will_abort';")
        node_primary.wait_for_catchup(node_standby)

        assert _standby_rows(psql_standby) == [
            "inserted in prepared will_commit"
        ], "finished prepared visible"
    finally:
        psql_primary.close()
        psql_standby.close()

    node_primary.stop()
    node_standby.stop()
