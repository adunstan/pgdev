# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Logical replication tests for temporal tables."""

# A table can use a temporal PRIMARY KEY or UNIQUE index as its REPLICA
# IDENTITY.  This is a GiST index (not B-tree) and its last element uses
# WITHOUT OVERLAPS.  That element restricts other rows with overlaps
# semantics instead of equality, but it is always at least as restrictive
# as a normal non-null unique index.  Therefore we can still apply logical
# decoding messages to the subscriber.

import re


def _create_tables(node_publisher, node_subscriber):
    # create tables on publisher

    node_publisher.safe_sql(
        "CREATE TABLE temporal_no_key (id int4range, valid_at daterange, a text)"
    )

    node_publisher.safe_sql(
        "CREATE TABLE temporal_pk (id int4range, valid_at daterange, a text, "
        "PRIMARY KEY (id, valid_at WITHOUT OVERLAPS))"
    )

    node_publisher.safe_sql(
        "CREATE TABLE temporal_unique (id int4range, valid_at daterange, a text, "
        "UNIQUE (id, valid_at WITHOUT OVERLAPS))"
    )

    # create tables on subscriber

    node_subscriber.safe_sql(
        "CREATE TABLE temporal_no_key (id int4range, valid_at daterange, a text)"
    )

    node_subscriber.safe_sql(
        "CREATE TABLE temporal_pk (id int4range, valid_at daterange, a text, "
        "PRIMARY KEY (id, valid_at WITHOUT OVERLAPS))"
    )

    node_subscriber.safe_sql(
        "CREATE TABLE temporal_unique (id int4range, valid_at daterange, a text, "
        "UNIQUE (id, valid_at WITHOUT OVERLAPS))"
    )


def _drop_everything(node_publisher, node_subscriber):
    node_publisher.safe_sql("DROP TABLE IF EXISTS temporal_no_key")
    node_publisher.safe_sql("DROP TABLE IF EXISTS temporal_pk")
    node_publisher.safe_sql("DROP TABLE IF EXISTS temporal_unique")
    node_publisher.safe_sql("DROP PUBLICATION pub1")
    node_subscriber.safe_sql("DROP TABLE IF EXISTS temporal_no_key")
    node_subscriber.safe_sql("DROP TABLE IF EXISTS temporal_pk")
    node_subscriber.safe_sql("DROP TABLE IF EXISTS temporal_unique")
    node_subscriber.safe_sql("DROP SUBSCRIPTION sub1")


def _assert_no_replica_identity_error(node, query, op, table):
    # Run in-process via sql() and inspect the returned error message; the
    # libpq error lacks the "psql:<stdin>:1:" prefix, so match the substance.
    res = node.sql(query)
    stderr = res.error_message or ""
    assert re.search(
        rf'ERROR: ( [A-Z0-9]+:)? cannot {op} (from )?table "{table}" because '
        rf"it does not have a replica identity and publishes {op}s",
        stderr,
    ), stderr


def test_034_temporal(create_pg):
    # setup

    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    # #################################
    # Test with REPLICA IDENTITY DEFAULT:
    # #################################

    _create_tables(node_publisher, node_subscriber)

    # sync initial data:

    node_publisher.safe_sql(
        "INSERT INTO temporal_no_key (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )
    node_publisher.safe_sql(
        "INSERT INTO temporal_pk (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )
    node_publisher.safe_sql(
        "INSERT INTO temporal_unique (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' " "PUBLICATION pub1"
    )
    node_subscriber.wait_for_subscription_sync(node_publisher, "sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_no_key ORDER BY id, valid_at"
    )
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_no_key DEFAULT"

    result = node_subscriber.safe_sql("SELECT * FROM temporal_pk ORDER BY id, valid_at")
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_pk DEFAULT"

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_unique ORDER BY id, valid_at"
    )
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_unique DEFAULT"

    # replicate with no key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_no_key (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    _assert_no_replica_identity_error(
        node_publisher,
        "UPDATE temporal_no_key SET a = 'b' WHERE id = '[2,3)'",
        "update",
        "temporal_no_key",
    )
    # No need to test again with FOR PORTION OF

    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_no_key WHERE id = '[3,4)'",
        "delete",
        "temporal_no_key",
    )
    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_no_key FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'",
        "delete",
        "temporal_no_key",
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_no_key ORDER BY id, valid_at"
    )
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2010-01-01)|a\n"
        "[3,4)|[2000-01-01,2010-01-01)|a\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_no_key DEFAULT"

    # replicate with a primary key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_pk (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("UPDATE temporal_pk SET a = 'b' WHERE id = '[2,3)'")
    node_publisher.safe_sql(
        "UPDATE temporal_pk FOR PORTION OF valid_at "
        "FROM '2001-01-01' TO '2002-01-01' SET a = 'c' WHERE id = '[2,3)'"
    )

    node_publisher.safe_sql("DELETE FROM temporal_pk WHERE id = '[3,4)'")
    node_publisher.safe_sql(
        "DELETE FROM temporal_pk FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'"
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT * FROM temporal_pk ORDER BY id, valid_at")
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2001-01-01)|b\n"
        "[2,3)|[2001-01-01,2002-01-01)|c\n"
        "[2,3)|[2003-01-01,2010-01-01)|b\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_pk DEFAULT"

    # replicate with a unique key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_unique (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    _assert_no_replica_identity_error(
        node_publisher,
        "UPDATE temporal_unique SET a = 'b' WHERE id = '[2,3)'",
        "update",
        "temporal_unique",
    )
    # No need to test again with FOR PORTION OF

    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_unique WHERE id = '[3,4)'",
        "delete",
        "temporal_unique",
    )
    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_unique FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'",
        "delete",
        "temporal_unique",
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_unique ORDER BY id, valid_at"
    )
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2010-01-01)|a\n"
        "[3,4)|[2000-01-01,2010-01-01)|a\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_unique DEFAULT"

    # cleanup

    _drop_everything(node_publisher, node_subscriber)

    # #################################
    # Test with REPLICA IDENTITY FULL:
    # #################################

    _create_tables(node_publisher, node_subscriber)

    node_publisher.safe_sql("ALTER TABLE temporal_no_key REPLICA IDENTITY FULL")
    node_publisher.safe_sql("ALTER TABLE temporal_pk REPLICA IDENTITY FULL")
    node_publisher.safe_sql("ALTER TABLE temporal_unique REPLICA IDENTITY FULL")

    node_subscriber.safe_sql("ALTER TABLE temporal_no_key REPLICA IDENTITY FULL")
    node_subscriber.safe_sql("ALTER TABLE temporal_pk REPLICA IDENTITY FULL")
    node_subscriber.safe_sql("ALTER TABLE temporal_unique REPLICA IDENTITY FULL")

    # sync initial data:

    node_publisher.safe_sql(
        "INSERT INTO temporal_no_key (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )
    node_publisher.safe_sql(
        "INSERT INTO temporal_pk (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )
    node_publisher.safe_sql(
        "INSERT INTO temporal_unique (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' " "PUBLICATION pub1"
    )
    node_subscriber.wait_for_subscription_sync(node_publisher, "sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_no_key ORDER BY id, valid_at"
    )
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_no_key FULL"

    result = node_subscriber.safe_sql("SELECT * FROM temporal_pk ORDER BY id, valid_at")
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_pk FULL"

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_unique ORDER BY id, valid_at"
    )
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_unique FULL"

    # replicate with no key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_no_key (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("UPDATE temporal_no_key SET a = 'b' WHERE id = '[2,3)'")
    node_publisher.safe_sql(
        "UPDATE temporal_no_key FOR PORTION OF valid_at "
        "FROM '2001-01-01' TO '2002-01-01' SET a = 'c' WHERE id = '[2,3)'"
    )

    node_publisher.safe_sql("DELETE FROM temporal_no_key WHERE id = '[3,4)'")
    node_publisher.safe_sql(
        "DELETE FROM temporal_no_key FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'"
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_no_key ORDER BY id, valid_at"
    )
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2001-01-01)|b\n"
        "[2,3)|[2001-01-01,2002-01-01)|c\n"
        "[2,3)|[2003-01-01,2010-01-01)|b\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_no_key FULL"

    # replicate with a primary key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_pk (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("UPDATE temporal_pk SET a = 'b' WHERE id = '[2,3)'")
    node_publisher.safe_sql(
        "UPDATE temporal_pk FOR PORTION OF valid_at "
        "FROM '2001-01-01' TO '2002-01-01' SET a = 'c' WHERE id = '[2,3)'"
    )

    node_publisher.safe_sql("DELETE FROM temporal_pk WHERE id = '[3,4)'")
    node_publisher.safe_sql(
        "DELETE FROM temporal_pk FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'"
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT * FROM temporal_pk ORDER BY id, valid_at")
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2001-01-01)|b\n"
        "[2,3)|[2001-01-01,2002-01-01)|c\n"
        "[2,3)|[2003-01-01,2010-01-01)|b\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_pk FULL"

    # replicate with a unique key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_unique (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("UPDATE temporal_unique SET a = 'b' WHERE id = '[2,3)'")
    node_publisher.safe_sql(
        "UPDATE temporal_unique FOR PORTION OF valid_at "
        "FROM '2001-01-01' TO '2002-01-01' SET a = 'c' WHERE id = '[2,3)'"
    )

    node_publisher.safe_sql("DELETE FROM temporal_unique WHERE id = '[3,4)'")
    node_publisher.safe_sql(
        "DELETE FROM temporal_unique FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'"
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_unique ORDER BY id, valid_at"
    )
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2001-01-01)|b\n"
        "[2,3)|[2001-01-01,2002-01-01)|c\n"
        "[2,3)|[2003-01-01,2010-01-01)|b\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_unique FULL"

    # cleanup

    _drop_everything(node_publisher, node_subscriber)

    # #################################
    # Test with REPLICA IDENTITY USING INDEX
    # #################################

    # create tables on publisher

    node_publisher.safe_sql(
        "CREATE TABLE temporal_pk (id int4range, valid_at daterange, a text, "
        "PRIMARY KEY (id, valid_at WITHOUT OVERLAPS))"
    )
    node_publisher.safe_sql(
        "ALTER TABLE temporal_pk REPLICA IDENTITY USING INDEX temporal_pk_pkey"
    )

    node_publisher.safe_sql(
        "CREATE TABLE temporal_unique (id int4range NOT NULL, "
        "valid_at daterange NOT NULL, a text, "
        "UNIQUE (id, valid_at WITHOUT OVERLAPS))"
    )
    node_publisher.safe_sql(
        "ALTER TABLE temporal_unique REPLICA IDENTITY USING INDEX "
        "temporal_unique_id_valid_at_key"
    )

    # create tables on subscriber

    node_subscriber.safe_sql(
        "CREATE TABLE temporal_pk (id int4range, valid_at daterange, a text, "
        "PRIMARY KEY (id, valid_at WITHOUT OVERLAPS))"
    )
    node_subscriber.safe_sql(
        "ALTER TABLE temporal_pk REPLICA IDENTITY USING INDEX temporal_pk_pkey"
    )

    node_subscriber.safe_sql(
        "CREATE TABLE temporal_unique (id int4range NOT NULL, "
        "valid_at daterange NOT NULL, a text, "
        "UNIQUE (id, valid_at WITHOUT OVERLAPS))"
    )
    node_subscriber.safe_sql(
        "ALTER TABLE temporal_unique REPLICA IDENTITY USING INDEX "
        "temporal_unique_id_valid_at_key"
    )

    # sync initial data:

    node_publisher.safe_sql(
        "INSERT INTO temporal_pk (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )
    node_publisher.safe_sql(
        "INSERT INTO temporal_unique (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' " "PUBLICATION pub1"
    )
    node_subscriber.wait_for_subscription_sync(node_publisher, "sub1")

    result = node_subscriber.safe_sql("SELECT * FROM temporal_pk ORDER BY id, valid_at")
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_pk USING INDEX"

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_unique ORDER BY id, valid_at"
    )
    assert (
        result == "[1,2)|[2000-01-01,2010-01-01)|a"
    ), "synced temporal_unique USING INDEX"

    # replicate with a primary key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_pk (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("UPDATE temporal_pk SET a = 'b' WHERE id = '[2,3)'")
    node_publisher.safe_sql(
        "UPDATE temporal_pk FOR PORTION OF valid_at "
        "FROM '2001-01-01' TO '2002-01-01' SET a = 'c' WHERE id = '[2,3)'"
    )

    node_publisher.safe_sql("DELETE FROM temporal_pk WHERE id = '[3,4)'")
    node_publisher.safe_sql(
        "DELETE FROM temporal_pk FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'"
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT * FROM temporal_pk ORDER BY id, valid_at")
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2001-01-01)|b\n"
        "[2,3)|[2001-01-01,2002-01-01)|c\n"
        "[2,3)|[2003-01-01,2010-01-01)|b\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_pk USING INDEX"

    # replicate with a unique key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_unique (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("UPDATE temporal_unique SET a = 'b' WHERE id = '[2,3)'")
    node_publisher.safe_sql(
        "UPDATE temporal_unique FOR PORTION OF valid_at "
        "FROM '2001-01-01' TO '2002-01-01' SET a = 'c' WHERE id = '[2,3)'"
    )

    node_publisher.safe_sql("DELETE FROM temporal_unique WHERE id = '[3,4)'")
    node_publisher.safe_sql(
        "DELETE FROM temporal_unique FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'"
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_unique ORDER BY id, valid_at"
    )
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2001-01-01)|b\n"
        "[2,3)|[2001-01-01,2002-01-01)|c\n"
        "[2,3)|[2003-01-01,2010-01-01)|b\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_unique USING INDEX"

    # cleanup

    _drop_everything(node_publisher, node_subscriber)

    # #################################
    # Test with REPLICA IDENTITY NOTHING
    # #################################

    _create_tables(node_publisher, node_subscriber)

    node_publisher.safe_sql("ALTER TABLE temporal_no_key REPLICA IDENTITY NOTHING")
    node_publisher.safe_sql("ALTER TABLE temporal_pk REPLICA IDENTITY NOTHING")
    node_publisher.safe_sql("ALTER TABLE temporal_unique REPLICA IDENTITY NOTHING")

    node_subscriber.safe_sql("ALTER TABLE temporal_no_key REPLICA IDENTITY NOTHING")
    node_subscriber.safe_sql("ALTER TABLE temporal_pk REPLICA IDENTITY NOTHING")
    node_subscriber.safe_sql("ALTER TABLE temporal_unique REPLICA IDENTITY NOTHING")

    # sync initial data:

    node_publisher.safe_sql(
        "INSERT INTO temporal_no_key (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )
    node_publisher.safe_sql(
        "INSERT INTO temporal_pk (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )
    node_publisher.safe_sql(
        "INSERT INTO temporal_unique (id, valid_at, a)\n"
        "   VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
    )

    node_publisher.safe_sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' " "PUBLICATION pub1"
    )
    node_subscriber.wait_for_subscription_sync(node_publisher, "sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_no_key ORDER BY id, valid_at"
    )
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_no_key NOTHING"

    result = node_subscriber.safe_sql("SELECT * FROM temporal_pk ORDER BY id, valid_at")
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_pk NOTHING"

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_unique ORDER BY id, valid_at"
    )
    assert result == "[1,2)|[2000-01-01,2010-01-01)|a", "synced temporal_unique NOTHING"

    # replicate with no key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_no_key (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    _assert_no_replica_identity_error(
        node_publisher,
        "UPDATE temporal_no_key SET a = 'b' WHERE id = '[2,3)'",
        "update",
        "temporal_no_key",
    )
    # No need to test again with FOR PORTION OF

    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_no_key WHERE id = '[3,4)'",
        "delete",
        "temporal_no_key",
    )
    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_no_key FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'",
        "delete",
        "temporal_no_key",
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_no_key ORDER BY id, valid_at"
    )
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2010-01-01)|a\n"
        "[3,4)|[2000-01-01,2010-01-01)|a\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_no_key NOTHING"

    # replicate with a primary key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_pk (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    _assert_no_replica_identity_error(
        node_publisher,
        "UPDATE temporal_pk SET a = 'b' WHERE id = '[2,3)'",
        "update",
        "temporal_pk",
    )
    # No need to test again with FOR PORTION OF

    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_pk WHERE id = '[3,4)'",
        "delete",
        "temporal_pk",
    )
    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_pk FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'",
        "delete",
        "temporal_pk",
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql("SELECT * FROM temporal_pk ORDER BY id, valid_at")
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2010-01-01)|a\n"
        "[3,4)|[2000-01-01,2010-01-01)|a\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_pk NOTHING"

    # replicate with a unique key:

    node_publisher.safe_sql(
        "INSERT INTO temporal_unique (id, valid_at, a)\n"
        "   VALUES ('[2,3)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[3,4)', '[2000-01-01,2010-01-01)', 'a'),\n"
        "          ('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
    )

    _assert_no_replica_identity_error(
        node_publisher,
        "UPDATE temporal_unique SET a = 'b' WHERE id = '[2,3)'",
        "update",
        "temporal_unique",
    )
    # No need to test again with FOR PORTION OF

    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_unique WHERE id = '[3,4)'",
        "delete",
        "temporal_unique",
    )
    _assert_no_replica_identity_error(
        node_publisher,
        "DELETE FROM temporal_unique FOR PORTION OF valid_at "
        "FROM '2002-01-01' TO '2003-01-01' WHERE id = '[2,3)'",
        "delete",
        "temporal_unique",
    )

    node_publisher.wait_for_catchup("sub1")

    result = node_subscriber.safe_sql(
        "SELECT * FROM temporal_unique ORDER BY id, valid_at"
    )
    assert result == (
        "[1,2)|[2000-01-01,2010-01-01)|a\n"
        "[2,3)|[2000-01-01,2010-01-01)|a\n"
        "[3,4)|[2000-01-01,2010-01-01)|a\n"
        "[4,5)|[2000-01-01,2010-01-01)|a"
    ), "replicated temporal_unique NOTHING"

    # cleanup

    _drop_everything(node_publisher, node_subscriber)
