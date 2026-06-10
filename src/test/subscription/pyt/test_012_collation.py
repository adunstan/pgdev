# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test collations, in particular nondeterministic ones (only works with
ICU).
"""

import os

import pytest

# Nondeterministic collations require ICU.  Gate on the build's with_icu flag
# (as the Perl test does) rather than a catalog query: pg_collation can contain
# ICU rows even when the server was built without a usable ICU provider, so the
# catalog check would let the test run and then fail at CREATE COLLATION.
pytestmark = pytest.mark.skipif(
    os.environ.get("with_icu") != "yes",
    reason="ICU not supported by this build",
)


def test_012_collation(create_pg):
    node_publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        initdb_extra=["--locale=C", "--encoding=UTF8"],
    )

    node_subscriber = create_pg(
        "subscriber",
        initdb_extra=["--locale=C", "--encoding=UTF8"],
    )

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    # Test plan: Create a table with a nondeterministic collation in the
    # primary key column.  Pre-insert rows on the publisher and subscriber
    # that are collation-wise equal but byte-wise different.  (We use a
    # string in different normal forms for that.)  Set up publisher and
    # subscriber.  Update the row on the publisher, but don't change the
    # primary key column.  The subscriber needs to find the row to be
    # updated using the nondeterministic collation semantics.  We need to
    # test for both a replica identity index and for replica identity
    # full, since those have different code paths internally.

    node_subscriber.safe_sql(
        "CREATE COLLATION ctest_nondet (provider = icu, locale = 'und', "
        "deterministic = false)"
    )

    # table with replica identity index

    node_publisher.safe_sql(
        "CREATE TABLE tab1 (a text PRIMARY KEY, b text)")

    node_publisher.safe_sql(
        r"INSERT INTO tab1 VALUES (U&'\00E4bc', 'foo')")

    node_subscriber.safe_sql(
        "CREATE TABLE tab1 (a text COLLATE ctest_nondet PRIMARY KEY, b text)")

    node_subscriber.safe_sql(
        r"INSERT INTO tab1 VALUES (U&'\0061\0308bc', 'foo')")

    # table with replica identity full

    node_publisher.safe_sql("CREATE TABLE tab2 (a text, b text)")
    node_publisher.safe_sql("ALTER TABLE tab2 REPLICA IDENTITY FULL")

    node_publisher.safe_sql(
        r"INSERT INTO tab2 VALUES (U&'\00E4bc', 'foo')")

    node_subscriber.safe_sql(
        "CREATE TABLE tab2 (a text COLLATE ctest_nondet, b text)")
    node_subscriber.safe_sql("ALTER TABLE tab2 REPLICA IDENTITY FULL")

    node_subscriber.safe_sql(
        r"INSERT INTO tab2 VALUES (U&'\0061\0308bc', 'foo')")

    # set up publication, subscription

    node_publisher.safe_sql("CREATE PUBLICATION pub1 FOR ALL TABLES")

    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher_connstr}' "
        "PUBLICATION pub1 WITH (copy_data = false)"
    )

    node_publisher.wait_for_catchup("sub1")

    # test with replica identity index

    node_publisher.safe_sql("UPDATE tab1 SET b = 'bar' WHERE b = 'foo'")

    node_publisher.wait_for_catchup("sub1")

    assert node_subscriber.safe_sql("SELECT b FROM tab1") == "bar", \
        "update with primary key with nondeterministic collation"

    # test with replica identity full

    node_publisher.safe_sql("UPDATE tab2 SET b = 'bar' WHERE b = 'foo'")

    node_publisher.wait_for_catchup("sub1")

    assert node_subscriber.safe_sql("SELECT b FROM tab2") == "bar", \
        "update with replica identity full with nondeterministic collation"
