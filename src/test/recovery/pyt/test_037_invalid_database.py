# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test we handle interrupted DROP DATABASE correctly."""

import re

from libpq import ExecStatusType
from libpq.errors import PqConnectionError


def test_037_invalid_database(create_pg):
    node = create_pg("node")
    node.append_conf(
        """
autovacuum = off
max_prepared_transactions=5
log_min_duration_statement=0
log_connections=receipt
log_disconnections=on
"""
    )
    node.restart()

    # First verify that we can't connect to or ALTER an invalid database. Just
    # mark the database as invalid ourselves, that's more reliable than hitting
    # the required race conditions (see testing further down)...

    # CREATE DATABASE cannot run inside a transaction block, so it must be a
    # statement of its own (the in-process Session runs a multi-statement
    # string as a single implicit transaction).
    node.safe_sql("CREATE DATABASE regression_invalid;")
    node.safe_sql(
        "UPDATE pg_database SET datconnlimit = -2 "
        "WHERE datname = 'regression_invalid';"
    )

    # can't connect to invalid database - error code / error message
    try:
        sess = node.connect("regression_invalid")
        sess.close()
        connect_err = None
    except PqConnectionError as exc:
        connect_err = str(exc)
    assert connect_err is not None, "can't connect to invalid database - error code"
    assert re.search(
        r'FATAL:\s+cannot connect to invalid database "regression_invalid"', connect_err
    ), "can't connect to invalid database - error message"

    # can't ALTER invalid database
    res = node.sql("ALTER DATABASE regression_invalid CONNECTION LIMIT 10")
    assert res.error_message is not None, "can't ALTER invalid database"

    # check invalid database can't be used as a template
    res = node.sql("CREATE DATABASE copy_invalid TEMPLATE regression_invalid")
    assert res.error_message is not None, "can't use invalid database as template"

    # Verify that VACUUM ignores an invalid database when computing how much of
    # the clog is needed (vac_truncate_clog()). For that we modify the
    # pg_database row of the invalid database to have an outdated datfrozenxid.
    sess = node.session()
    sess.clear_notices()
    sess.query_safe(
        "UPDATE pg_database SET datfrozenxid = '123456' "
        "WHERE datname = 'regression_invalid';"
        "DROP TABLE IF EXISTS foo_tbl; CREATE TABLE foo_tbl();"
    )
    # VACUUM cannot run inside a transaction block; run it separately.
    sess.query_safe("VACUUM FREEZE;")
    notices = sess.get_notices_str()
    assert not re.search(
        r"some databases have not been vacuumed in over 2 billion transactions", notices
    ), "invalid databases are ignored by vac_truncate_clog"

    # But we need to be able to drop an invalid database.
    res = node.sql("DROP DATABASE regression_invalid")
    assert res.error_message is None, "can DROP invalid database"

    # Ensure database is gone
    res = node.sql("DROP DATABASE regression_invalid")
    assert res.error_message is not None, "can't drop already dropped database"

    # Test that interruption of DROP DATABASE is handled properly. To ensure the
    # interruption happens at the appropriate moment, we lock pg_tablespace.
    # DROP DATABASE scans pg_tablespace once it has reached the "irreversible"
    # part of dropping the database, making it a suitable point to wait.  Since
    # relcache init reads pg_tablespace, establish each connection before
    # locking.  This avoids a connection-time hang with debug_discard_caches.
    cancel = node.connect()
    bgpsql = node.connect()
    pid = bgpsql.query_oneval("SELECT pg_backend_pid()")

    # create the database, prevent drop database via lock held by a 2PC
    # transaction
    assert (
        bgpsql.do(
            "CREATE DATABASE regression_invalid_interrupt;",
            "BEGIN;\nLOCK pg_tablespace;\nPREPARE TRANSACTION 'lock_tblspc';",
        )
        == 1
    )

    # Try to drop. This will wait due to the still held lock.
    bgpsql.do_async("DROP DATABASE regression_invalid_interrupt;")

    # Once the DROP DATABASE is waiting for the lock, interrupt it.
    cancel_res = cancel.query(
        f"""
        DO $$
        BEGIN
            WHILE NOT EXISTS(SELECT * FROM pg_locks WHERE NOT granted AND relation = 'pg_tablespace'::regclass AND mode = 'AccessShareLock') LOOP
                PERFORM pg_sleep(.1);
            END LOOP;
        END$$;
        SELECT pg_cancel_backend({pid})"""
    )
    assert (
        cancel_res.status == ExecStatusType.PGRES_TUPLES_OK
    ), "canceling DROP DATABASE"  # COMMAND_TUPLES_OK
    cancel.close()

    bgpsql.wait_for_completion()
    # wait for cancellation to be processed
    # pass("cancel processed")

    # Verify that connections to the database aren't allowed.  The backend
    # checks this before relcache init, so the lock won't interfere.
    try:
        sess = node.connect("regression_invalid_interrupt")
        sess.close()
        ii_connect_err = None
    except PqConnectionError as exc:
        ii_connect_err = str(exc)
    assert ii_connect_err is not None, "can't connect to invalid_interrupt database"

    # To properly drop the database, we need to release the lock previously
    # preventing doing so.
    assert (
        bgpsql.do("ROLLBACK PREPARED 'lock_tblspc'") == ExecStatusType.PGRES_COMMAND_OK
    ), "unblock DROP DATABASE"

    res = bgpsql.query("DROP DATABASE regression_invalid_interrupt")
    assert res.error_message is None, "DROP DATABASE invalid_interrupt"

    bgpsql.close()
