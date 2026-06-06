# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test XID wraparound limits.

When you get close to XID wraparound, you start to get warnings, and
when you get even closer, the system refuses to assign any more XIDs
until the oldest databases have been vacuumed and datfrozenxid has
been advanced.
"""

import os
import re

import pytest

if "xid_wraparound" not in os.environ.get("PG_TEST_EXTRA", ""):
    pytest.skip(
        "test xid_wraparound not enabled in PG_TEST_EXTRA",
        allow_module_level=True,
    )


def test_002_limits(create_pg):
    # Initialize node
    node = create_pg("wraparound", start=False)

    node.append_conf(
        """
autovacuum_naptime = 1s
log_autovacuum_min_duration = 0
log_connections = on
log_statement = 'all'
"""
    )
    node.start()
    node.safe_sql("CREATE EXTENSION xid_wraparound")

    # Create a test table. We disable autovacuum on the table to run it only
    # to prevent wraparound.
    node.safe_sql(
        """
CREATE TABLE wraparoundtest(t text) WITH (autovacuum_enabled = off);
INSERT INTO wraparoundtest VALUES ('start');
"""
    )

    # Start a background session, which holds a transaction open, preventing
    # autovacuum from advancing relfrozenxid and datfrozenxid.
    background_session = node.connect("postgres")
    assert (
        background_session.do(
            """
		BEGIN;
		INSERT INTO wraparoundtest VALUES ('oldxact');
"""
        )
        is not None
    )

    # Consume 2 billion transactions, to get close to wraparound
    node.safe_sql("SELECT consume_xids(1000000000)")
    node.safe_sql("INSERT INTO wraparoundtest VALUES ('after 1 billion')")

    node.safe_sql("SELECT consume_xids(1000000000)")
    node.safe_sql("INSERT INTO wraparoundtest VALUES ('after 2 billion')")

    # We are now just under 150 million XIDs away from wraparound.
    # Continue consuming XIDs, in batches of 10 million, until we get
    # the warning:
    #
    #  WARNING:  database "postgres" must be vacuumed within 3000024 transactions
    #  HINT:  To avoid a database shutdown, execute a database-wide VACUUM in that database.
    #  You might also need to commit or roll back old prepared transactions, or drop stale replication slots.
    warn_limit = 0
    for _i in range(1, 16):
        # Use a fresh session so the warnings (notices) for this batch can be
        # inspected in isolation, mirroring psql with on_error_die.
        sess = node.connect("postgres")
        try:
            sess.clear_notices()
            res = sess.query("SELECT consume_xids(10000000)")
            assert res.error_message is None, res.error_message
            stderr = sess.get_notices_str()
        finally:
            sess.close()

        if re.search(
            r'WARNING:  database "postgres" must be vacuumed within '
            r"[0-9]+ transactions",
            stderr,
        ):
            # Reached the warn-limit
            warn_limit = 1
            break
    assert warn_limit == 1, "warn-limit reached"

    # We can still INSERT, despite the warnings.
    node.safe_sql("INSERT INTO wraparoundtest VALUES ('reached warn-limit')")

    # Keep going. We'll hit the hard "stop" limit.
    sess = node.connect("postgres")
    try:
        res = sess.query("SELECT consume_xids(100000000)")
        stderr = (res.error_message or "") + sess.get_notices_str()
    finally:
        sess.close()
    assert re.search(
        r"ERROR:  database is not accepting commands that assign new "
        r'transaction IDs to avoid wraparound data loss in database "postgres"',
        stderr,
    ), "stop-limit"

    # Finish the old transaction, to allow vacuum freezing to advance
    # relfrozenxid and datfrozenxid again.
    background_session.do("COMMIT;")
    background_session.close()

    # VACUUM, to freeze the tables and advance datfrozenxid.
    #
    # Autovacuum does this for the other databases, and would do it for
    # 'postgres' too, but let's test manual VACUUM.
    #
    node.safe_sql("VACUUM")

    # Wait until autovacuum has processed the other databases and advanced
    # the system-wide oldest-XID.
    assert node.poll_query_until(
        "INSERT INTO wraparoundtest VALUES ('after VACUUM') RETURNING true"
    )

    # Check the table contents
    ret = node.safe_sql("SELECT * from wraparoundtest")
    assert ret == (
        "start\n"
        "oldxact\n"
        "after 1 billion\n"
        "after 2 billion\n"
        "reached warn-limit\n"
        "after VACUUM"
    )

    node.stop()
