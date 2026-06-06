# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that logical replication respects permissions."""

import os
import re

from pypg.util import USE_UNIX_SOCKETS

publisher_connstr = None
offset = 0


def run_as(node, role, *statements):
    """Run *statements* on a fresh connection as *role*.

    Running each role-scoped statement on its own one-shot connection keeps
    the authorization change from outliving the statement.
    Here safe_sql reuses one cached libpq session per database, so a stray
    SET SESSION AUTHORIZATION would leak into later safe_sql calls; running
    the role-scoped statements on a throwaway connection (logging in as the
    role directly) keeps each statement properly isolated.
    """
    sess = node.connect(user=role)
    try:
        for stmt in statements:
            sess.query_safe(stmt)
    finally:
        sess.close()


def publish_insert(node_publisher, tbl, new_i):
    sess = node_publisher.connect(user="regress_alice")
    try:
        sess.query_safe(f"INSERT INTO {tbl} (i) VALUES ({new_i})")
    finally:
        sess.close()


def publish_update(node_publisher, tbl, old_i, new_i):
    sess = node_publisher.connect(user="regress_alice")
    try:
        sess.query_safe(f"UPDATE {tbl} SET i = {new_i} WHERE i = {old_i}")
    finally:
        sess.close()


def publish_delete(node_publisher, tbl, old_i):
    sess = node_publisher.connect(user="regress_alice")
    try:
        sess.query_safe(f"DELETE FROM {tbl} WHERE i = {old_i}")
    finally:
        sess.close()


def expect_replication(node_publisher, node_subscriber, tbl, cnt, mn, mx, testname):
    node_publisher.wait_for_catchup("admin_sub")
    result = node_subscriber.safe_sql(f"SELECT COUNT(i), MIN(i), MAX(i) FROM {tbl}")
    assert result == f"{cnt}|{mn}|{mx}", testname


def expect_failure(node_subscriber, tbl, cnt, mn, mx, regexp, testname):
    global offset  # pylint: disable=global-statement
    offset = node_subscriber.wait_for_log(regexp, offset)
    result = node_subscriber.safe_sql(f"SELECT COUNT(i), MIN(i), MAX(i) FROM {tbl}")
    assert result == f"{cnt}|{mn}|{mx}", testname


def revoke_superuser(node_subscriber, role):
    node_subscriber.safe_sql(f"ALTER ROLE {role} NOSUPERUSER")


def grant_superuser(node_subscriber, role):
    node_subscriber.safe_sql(f"ALTER ROLE {role} SUPERUSER")


def test_027_nosuperuser(create_pg):
    global publisher_connstr, offset  # pylint: disable=global-statement

    # Create publisher and subscriber nodes with schemas owned and published by
    # "regress_alice" but subscribed and replicated by different role
    # "regress_admin".  For partitioned tables, layout the partitions
    # differently on the publisher than on the subscriber.
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    remainder_a = {"publisher": 0, "subscriber": 1}
    remainder_b = {"publisher": 1, "subscriber": 0}

    for node in (node_publisher, node_subscriber):
        ra = remainder_a[node.name]
        rb = remainder_b[node.name]
        node.safe_sql(
            """
  CREATE ROLE regress_admin SUPERUSER LOGIN;
  CREATE ROLE regress_alice NOSUPERUSER LOGIN;
  GRANT CREATE ON DATABASE postgres TO regress_alice;
  GRANT PG_CREATE_SUBSCRIPTION TO regress_alice;
  """
        )
        # Remaining statements run as regress_alice.
        run_as(
            node,
            "regress_alice",
            "CREATE SCHEMA alice",
            "GRANT USAGE ON SCHEMA alice TO regress_admin",
            "CREATE TABLE alice.unpartitioned (i INTEGER)",
            "ALTER TABLE alice.unpartitioned REPLICA IDENTITY FULL",
            "GRANT SELECT ON TABLE alice.unpartitioned TO regress_admin",
            "CREATE TABLE alice.hashpart (i INTEGER) PARTITION BY HASH (i)",
            "ALTER TABLE alice.hashpart REPLICA IDENTITY FULL",
            "GRANT SELECT ON TABLE alice.hashpart TO regress_admin",
            "CREATE TABLE alice.hashpart_a PARTITION OF alice.hashpart "
            f"FOR VALUES WITH (MODULUS 2, REMAINDER {ra})",
            "ALTER TABLE alice.hashpart_a REPLICA IDENTITY FULL",
            "CREATE TABLE alice.hashpart_b PARTITION OF alice.hashpart "
            f"FOR VALUES WITH (MODULUS 2, REMAINDER {rb})",
            "ALTER TABLE alice.hashpart_b REPLICA IDENTITY FULL",
        )

    run_as(
        node_publisher,
        "regress_alice",
        "CREATE PUBLICATION alice "
        "  FOR TABLE alice.unpartitioned, alice.hashpart "
        "  WITH (publish_via_partition_root = true)",
    )

    # CREATE SUBSCRIPTION cannot run in a transaction block, so it must be sent
    # as a standalone statement.  Run it directly as regress_admin rather than
    # via SET SESSION AUTHORIZATION (which would force it into an implicit
    # transaction with the SET).
    sess = node_subscriber.connect(user="regress_admin")
    try:
        sess.query_safe(
            f"CREATE SUBSCRIPTION admin_sub CONNECTION '{publisher_connstr}' "
            "PUBLICATION alice WITH (password_required=false)"
        )
    finally:
        sess.close()

    # Wait for initial sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "admin_sub")

    # Verify that "regress_admin" can replicate into the tables
    publish_insert(node_publisher, "alice.unpartitioned", 1)
    publish_insert(node_publisher, "alice.unpartitioned", 3)
    publish_insert(node_publisher, "alice.unpartitioned", 5)
    publish_update(node_publisher, "alice.unpartitioned", 1, 7)
    publish_delete(node_publisher, "alice.unpartitioned", 3)
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        2,
        5,
        7,
        "superuser admin replicates into unpartitioned",
    )

    # Revoke and restore superuser privilege for "regress_admin", verifying
    # that replication fails while superuser privilege is missing, but works
    # again and catches up once superuser is restored.
    revoke_superuser(node_subscriber, "regress_admin")
    publish_update(node_publisher, "alice.unpartitioned", 5, 9)
    expect_failure(
        node_subscriber,
        "alice.unpartitioned",
        2,
        5,
        7,
        r'(?msi)ERROR: ( [A-Z0-9]+:)? role "regress_admin" cannot SET ROLE to "regress_alice"',
        "non-superuser admin fails to replicate update",
    )
    grant_superuser(node_subscriber, "regress_admin")
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        2,
        7,
        9,
        "admin with restored superuser privilege replicates update",
    )

    # Privileges on the target role suffice for non-superuser replication.
    node_subscriber.safe_sql(
        """
ALTER ROLE regress_admin NOSUPERUSER;
GRANT regress_alice TO regress_admin;
"""
    )

    publish_insert(node_publisher, "alice.unpartitioned", 11)
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        3,
        7,
        11,
        "nosuperuser admin with privileges on role can replicate INSERT into unpartitioned",
    )

    publish_update(node_publisher, "alice.unpartitioned", 7, 13)
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        3,
        9,
        13,
        "nosuperuser admin with privileges on role can replicate UPDATE into unpartitioned",
    )

    publish_delete(node_publisher, "alice.unpartitioned", 9)
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        2,
        11,
        13,
        "nosuperuser admin with privileges on role can replicate DELETE into unpartitioned",
    )

    # Test partitioning
    publish_insert(node_publisher, "alice.hashpart", 101)
    publish_insert(node_publisher, "alice.hashpart", 102)
    publish_insert(node_publisher, "alice.hashpart", 103)
    publish_update(node_publisher, "alice.hashpart", 102, 120)
    publish_delete(node_publisher, "alice.hashpart", 101)
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.hashpart",
        2,
        103,
        120,
        "nosuperuser admin with privileges on role can replicate into hashpart",
    )

    # Force RLS on the target table and check that replication fails.
    run_as(
        node_subscriber,
        "regress_alice",
        "ALTER TABLE alice.unpartitioned ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE alice.unpartitioned FORCE ROW LEVEL SECURITY",
    )

    publish_insert(node_publisher, "alice.unpartitioned", 15)
    expect_failure(
        node_subscriber,
        "alice.unpartitioned",
        2,
        11,
        13,
        r'(?msi)ERROR: ( [A-Z0-9]+:)? user "regress_alice" cannot replicate into relation with row-level security enabled: "unpartitioned\w*"',
        "replication of insert into table with forced rls fails",
    )

    # Since replication acts as the table owner, replication will succeed if we
    # don't force it.
    node_subscriber.safe_sql(
        "ALTER TABLE alice.unpartitioned NO FORCE ROW LEVEL SECURITY"
    )
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        3,
        11,
        15,
        "non-superuser admin can replicate insert if rls is not forced",
    )

    node_subscriber.safe_sql("ALTER TABLE alice.unpartitioned FORCE ROW LEVEL SECURITY")
    publish_update(node_publisher, "alice.unpartitioned", 11, 17)
    expect_failure(
        node_subscriber,
        "alice.unpartitioned",
        3,
        11,
        15,
        r'(?msi)ERROR: ( [A-Z0-9]+:)? user "regress_alice" cannot replicate into relation with row-level security enabled: "unpartitioned\w*"',
        "replication of update into table with forced rls fails",
    )
    node_subscriber.safe_sql(
        "ALTER TABLE alice.unpartitioned NO FORCE ROW LEVEL SECURITY"
    )
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        3,
        13,
        17,
        "non-superuser admin can replicate update if rls is not forced",
    )

    # Remove some of alice's privileges on her own table. Then replication
    # should fail.
    node_subscriber.safe_sql(
        "REVOKE SELECT, INSERT ON alice.unpartitioned FROM regress_alice"
    )
    publish_insert(node_publisher, "alice.unpartitioned", 19)
    expect_failure(
        node_subscriber,
        "alice.unpartitioned",
        3,
        13,
        17,
        r"(?msi)ERROR: ( [A-Z0-9]+:)? permission denied for table unpartitioned",
        "replication of insert fails if table owner lacks insert permission",
    )

    # alice needs INSERT but not SELECT to replicate an INSERT.
    node_subscriber.safe_sql("GRANT INSERT ON alice.unpartitioned TO regress_alice")
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        4,
        13,
        19,
        "restoring insert permission permits replication to continue",
    )

    # Now let's try an UPDATE and a DELETE.
    node_subscriber.safe_sql(
        "REVOKE UPDATE, DELETE ON alice.unpartitioned FROM regress_alice"
    )
    publish_update(node_publisher, "alice.unpartitioned", 13, 21)
    publish_delete(node_publisher, "alice.unpartitioned", 15)
    expect_failure(
        node_subscriber,
        "alice.unpartitioned",
        4,
        13,
        19,
        r"(?msi)ERROR: ( [A-Z0-9]+:)? permission denied for table unpartitioned",
        "replication of update/delete fails if table owner lacks corresponding permission",
    )

    # Restoring UPDATE and DELETE is insufficient.
    node_subscriber.safe_sql(
        "GRANT UPDATE, DELETE ON alice.unpartitioned TO regress_alice"
    )
    expect_failure(
        node_subscriber,
        "alice.unpartitioned",
        4,
        13,
        19,
        r"(?msi)ERROR: ( [A-Z0-9]+:)? permission denied for table unpartitioned",
        "replication of update/delete fails if table owner lacks SELECT permission",
    )

    # alice needs INSERT but not SELECT to replicate an INSERT.
    node_subscriber.safe_sql("GRANT SELECT ON alice.unpartitioned TO regress_alice")
    expect_replication(
        node_publisher,
        node_subscriber,
        "alice.unpartitioned",
        3,
        17,
        21,
        "restoring SELECT permission permits replication to continue",
    )

    # The apply worker should get restarted after the superuser privileges are
    # revoked for subscription owner alice.
    grant_superuser(node_subscriber, "regress_alice")

    # CREATE SUBSCRIPTION cannot run in a transaction block; run it standalone
    # as regress_alice.
    sess = node_subscriber.connect(user="regress_alice")
    try:
        sess.query_safe(
            f"CREATE SUBSCRIPTION regression_sub CONNECTION '{publisher_connstr}' "
            "PUBLICATION alice"
        )
    finally:
        sess.close()

    # Wait for initial sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "regression_sub")

    # Check the subscriber log from now on.
    offset = node_subscriber.log_position()

    revoke_superuser(node_subscriber, "regress_alice")

    # After the user becomes non-superuser the apply worker should be
    # restarted.
    node_subscriber.wait_for_log(
        r'LOG: ( [A-Z0-9]+:)? logical replication worker for subscription "regression_sub" will restart because the subscription owner\'s superuser privileges have been revoked',
        offset,
    )

    # If the subscription connection requires a password ('password_required'
    # is true) then a non-superuser must specify that password in the
    # connection string.  Below this rewrites pg_hba.conf with a "local"
    # (Unix-domain socket) rule, so the connection must come over a socket;
    # skip it over TCP.
    if not USE_UNIX_SOCKETS:
        return

    node_publisher1 = create_pg("publisher1", allows_streaming="logical")
    node_subscriber1 = create_pg("subscriber1")
    publisher_connstr1 = (
        f"host={node_publisher1.host} port={node_publisher1.port} "
        "user=regress_test_user dbname=postgres"
    )
    publisher_connstr2 = (
        f"host={node_publisher1.host} port={node_publisher1.port} "
        "user=regress_test_user dbname=postgres password=secret"
    )

    for node in (node_publisher1, node_subscriber1):
        node.safe_sql(
            """
                CREATE ROLE regress_test_user PASSWORD 'secret' LOGIN REPLICATION;
                GRANT CREATE ON DATABASE postgres TO regress_test_user;
                GRANT PG_CREATE_SUBSCRIPTION TO regress_test_user;
            """
        )

    sess = node_publisher1.connect(user="regress_test_user")
    try:
        sess.query_safe("CREATE PUBLICATION regress_test_pub")
    finally:
        sess.close()
    node_subscriber1.safe_sql(
        f"CREATE SUBSCRIPTION regress_test_sub CONNECTION '{publisher_connstr1}' "
        "PUBLICATION regress_test_pub"
    )

    # Wait for initial sync to finish
    node_subscriber1.wait_for_subscription_sync(node_publisher1, "regress_test_sub")

    # Setup pg_hba configuration so that logical replication connection without
    # password is not allowed.
    os.remove(os.path.join(node_publisher1.data_dir, "pg_hba.conf"))
    node_publisher1.append_conf(
        "local all \t\t\t\tregress_test_user \tmd5", filename="pg_hba.conf"
    )
    node_publisher1.reload()

    # Change the subscription owner to a non-superuser
    node_subscriber1.safe_sql(
        "ALTER SUBSCRIPTION regress_test_sub OWNER TO regress_test_user"
    )

    # Non-superuser must specify password in the connection string.  Run as
    # regress_test_user (ALTER SUBSCRIPTION ... REFRESH PUBLICATION cannot run
    # in a transaction block, so it cannot be combined with SET SESSION
    # AUTHORIZATION).
    sess = node_subscriber1.connect(user="regress_test_user")
    try:
        res = sess.query("ALTER SUBSCRIPTION regress_test_sub REFRESH PUBLICATION")
        stderr = res.error_message or ""
    finally:
        sess.close()
    assert stderr != "", (
        "non zero exit for subscription whose owner is a non-superuser must "
        "specify password parameter of the connection string"
    )
    assert re.search(
        r"DETAIL:  Non-superusers must provide a password in the connection string.",
        stderr,
    ), (
        "subscription whose owner is a non-superuser must specify password "
        "parameter of the connection string"
    )

    # It should succeed after including the password parameter of the
    # connection string.
    sess = node_subscriber1.connect(user="regress_test_user")
    try:
        res = sess.query(
            f"ALTER SUBSCRIPTION regress_test_sub CONNECTION '{publisher_connstr2}'"
        )
        err1 = res.error_message
        res = sess.query("ALTER SUBSCRIPTION regress_test_sub REFRESH PUBLICATION")
        err2 = res.error_message
    finally:
        sess.close()
    assert err1 is None and err2 is None, (
        "Non-superuser will be able to refresh the publication after specifying "
        "the password parameter of the connection string"
    )
