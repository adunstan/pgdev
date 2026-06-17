# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that logical replication respects permissions."""

# Regex matching the permission-denied error logged on the subscriber.
PERMISSION_DENIED_RE = (
    r"ERROR: ( [A-Z0-9]+:)? permission denied for table unpartitioned"
)


def test_033_run_as_table_owner(create_pg):
    node_publisher = create_pg("publisher", allows_streaming="logical")
    node_subscriber = create_pg("subscriber")

    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )

    # The subscriber log offset used by expect_failure, tracked across calls.
    offset = [0]

    # Note: our in-process safe_sql reuses a cached connection, so a SET
    # SESSION AUTHORIZATION would persist across calls.  Each block that changes
    # the session role therefore resets it again before returning, to keep the
    # session role isolated between calls.

    def publish_insert(tbl, new_i):
        node_publisher.safe_sql(
            "SET SESSION AUTHORIZATION regress_alice;\n"
            f"INSERT INTO {tbl} (i) VALUES ({new_i});\n"
            "RESET SESSION AUTHORIZATION;"
        )

    def publish_update(tbl, old_i, new_i):
        node_publisher.safe_sql(
            "SET SESSION AUTHORIZATION regress_alice;\n"
            f"UPDATE {tbl} SET i = {new_i} WHERE i = {old_i};\n"
            "RESET SESSION AUTHORIZATION;"
        )

    def publish_delete(tbl, old_i):
        node_publisher.safe_sql(
            "SET SESSION AUTHORIZATION regress_alice;\n"
            f"DELETE FROM {tbl} WHERE i = {old_i};\n"
            "RESET SESSION AUTHORIZATION;"
        )

    def expect_replication(tbl, cnt, minimum, maximum, testname):
        node_publisher.wait_for_catchup("admin_sub")
        result = node_subscriber.safe_sql(f"SELECT COUNT(i), MIN(i), MAX(i) FROM {tbl}")
        assert result == f"{cnt}|{minimum}|{maximum}", testname

    def expect_failure(tbl, cnt, minimum, maximum, re_pat, testname):
        offset[0] = node_subscriber.wait_for_log(re_pat, offset[0])
        result = node_subscriber.safe_sql(f"SELECT COUNT(i), MIN(i), MAX(i) FROM {tbl}")
        assert result == f"{cnt}|{minimum}|{maximum}", testname

    def revoke_superuser(role):
        node_subscriber.safe_sql(f"ALTER ROLE {role} NOSUPERUSER")

    # Create publisher and subscriber nodes with schemas owned and published by
    # "regress_alice" but subscribed and replicated by different role
    # "regress_admin" and "regress_admin2". For partitioned tables, layout the
    # partitions differently on the publisher than on the subscriber.
    for node in (node_publisher, node_subscriber):
        node.safe_sql(
            "CREATE ROLE regress_admin SUPERUSER LOGIN;\n"
            "CREATE ROLE regress_admin2 SUPERUSER LOGIN;\n"
            "CREATE ROLE regress_alice NOSUPERUSER LOGIN;\n"
            "GRANT CREATE ON DATABASE postgres TO regress_alice;\n"
            "SET SESSION AUTHORIZATION regress_alice;\n"
            "CREATE SCHEMA alice;\n"
            "GRANT USAGE ON SCHEMA alice TO regress_admin;\n"
            "\n"
            "CREATE TABLE alice.unpartitioned (i INTEGER);\n"
            "ALTER TABLE alice.unpartitioned REPLICA IDENTITY FULL;\n"
            "GRANT SELECT ON TABLE alice.unpartitioned TO regress_admin;\n"
            "RESET SESSION AUTHORIZATION;"
        )

    node_publisher.safe_sql(
        "SET SESSION AUTHORIZATION regress_alice;\n"
        "\n"
        "CREATE PUBLICATION alice FOR TABLE alice.unpartitioned\n"
        "  WITH (publish_via_partition_root = true);\n"
        "RESET SESSION AUTHORIZATION;"
    )

    # CREATE SUBSCRIPTION cannot run inside a multi-statement implicit txn, so
    # split the SET SESSION AUTHORIZATION from it.  Run both on a dedicated
    # session and discard it so the role change does not leak into the cached
    # session used elsewhere.
    admin_sess = node_subscriber.connect()
    try:
        admin_sess.query_safe("SET SESSION AUTHORIZATION regress_admin")
        admin_sess.query_safe(
            f"CREATE SUBSCRIPTION admin_sub CONNECTION '{publisher_connstr}' "
            "PUBLICATION alice "
            "WITH (run_as_owner = true, password_required = false)"
        )
    finally:
        admin_sess.close()

    # Wait for initial sync to finish
    node_subscriber.wait_for_subscription_sync(node_publisher, "admin_sub")

    # Verify that "regress_admin" can replicate into the tables
    publish_insert("alice.unpartitioned", 1)
    publish_insert("alice.unpartitioned", 3)
    publish_insert("alice.unpartitioned", 5)
    publish_update("alice.unpartitioned", 1, 7)
    publish_delete("alice.unpartitioned", 3)
    expect_replication("alice.unpartitioned", 2, 5, 7, "superuser can replicate")

    # Revoke superuser privilege for "regress_admin", and verify that we now
    # fail to replicate an insert.
    revoke_superuser("regress_admin")
    publish_insert("alice.unpartitioned", 9)
    expect_failure(
        "alice.unpartitioned",
        2,
        5,
        7,
        PERMISSION_DENIED_RE,
        "with no privileges cannot replicate",
    )

    # Now grant DML privileges and verify that we can replicate an INSERT.
    node_subscriber.safe_sql(
        "ALTER ROLE regress_admin NOSUPERUSER;\n"
        "SET SESSION AUTHORIZATION regress_alice;\n"
        "GRANT INSERT,UPDATE,DELETE ON alice.unpartitioned TO regress_admin;\n"
        "REVOKE SELECT ON alice.unpartitioned FROM regress_admin;\n"
        "RESET SESSION AUTHORIZATION;"
    )
    expect_replication(
        "alice.unpartitioned", 3, 5, 9, "with INSERT privilege can replicate INSERT"
    )

    # We can't yet replicate an UPDATE because we don't have SELECT.
    publish_update("alice.unpartitioned", 5, 11)
    publish_delete("alice.unpartitioned", 9)
    expect_failure(
        "alice.unpartitioned",
        3,
        5,
        9,
        PERMISSION_DENIED_RE,
        "without SELECT privilege cannot replicate UPDATE or DELETE",
    )

    # After granting SELECT, replication resumes.
    node_subscriber.safe_sql(
        "SET SESSION AUTHORIZATION regress_alice;\n"
        "GRANT SELECT ON alice.unpartitioned TO regress_admin;\n"
        "RESET SESSION AUTHORIZATION;"
    )
    expect_replication(
        "alice.unpartitioned", 2, 7, 11, "with all privileges can replicate"
    )

    # Remove all privileges again. Instead, give the ability to SET ROLE to
    # regress_alice.
    node_subscriber.safe_sql(
        "SET SESSION AUTHORIZATION regress_alice;\n"
        "REVOKE ALL PRIVILEGES ON alice.unpartitioned FROM regress_admin;\n"
        "RESET SESSION AUTHORIZATION;\n"
        "GRANT regress_alice TO regress_admin WITH INHERIT FALSE, SET TRUE;"
    )

    # Because replication is running as the subscription owner in this test,
    # the above grant doesn't help: it gives the ability to SET ROLE, but not
    # privileges on the table.
    publish_insert("alice.unpartitioned", 13)
    expect_failure(
        "alice.unpartitioned",
        2,
        7,
        11,
        PERMISSION_DENIED_RE,
        "with SET ROLE but not INHERIT cannot replicate",
    )

    # Now remove SET ROLE and add INHERIT and check that things start working.
    node_subscriber.safe_sql(
        "GRANT regress_alice TO regress_admin WITH INHERIT TRUE, SET FALSE;"
    )
    expect_replication(
        "alice.unpartitioned", 3, 7, 13, "with INHERIT but not SET ROLE can replicate"
    )

    # Similar to the previous test, remove all privileges again and instead,
    # give the ability to SET ROLE to regress_alice.
    node_subscriber.safe_sql(
        "SET SESSION AUTHORIZATION regress_alice;\n"
        "REVOKE ALL PRIVILEGES ON alice.unpartitioned FROM regress_admin;\n"
        "RESET SESSION AUTHORIZATION;\n"
        "GRANT regress_alice TO regress_admin WITH INHERIT FALSE, SET TRUE;"
    )

    # Because replication is running as the subscription owner in this test,
    # the above grant doesn't help.
    publish_insert("alice.unpartitioned", 14)
    expect_failure(
        "alice.unpartitioned",
        3,
        7,
        13,
        PERMISSION_DENIED_RE,
        "with no privileges cannot replicate",
    )

    # Allow the replication to run as table owner and check that things start
    # working.
    node_subscriber.safe_sql("ALTER SUBSCRIPTION admin_sub SET (run_as_owner = false);")

    expect_replication(
        "alice.unpartitioned",
        4,
        7,
        14,
        "can replicate after setting run_as_owner to false",
    )

    # Remove the subscrition and truncate the table for the initial data sync
    # tests.
    # DROP SUBSCRIPTION cannot run inside a transaction block, so it cannot
    # share the implicit multi-statement transaction with the TRUNCATE.
    node_subscriber.safe_sql("DROP SUBSCRIPTION admin_sub")
    node_subscriber.safe_sql("TRUNCATE alice.unpartitioned")

    # Create a new subscription "admin_sub" owned by regress_admin2. It's
    # disabled so that we revoke superuser privilege after creation.
    admin2_sess = node_subscriber.connect()
    try:
        admin2_sess.query_safe("SET SESSION AUTHORIZATION regress_admin2")
        admin2_sess.query_safe(
            f"CREATE SUBSCRIPTION admin_sub CONNECTION '{publisher_connstr}' "
            "PUBLICATION alice "
            "WITH (run_as_owner = false, password_required = false, "
            "copy_data = true, enabled = false)"
        )
    finally:
        admin2_sess.close()

    # Revoke superuser privilege for "regress_admin2", and give it the
    # ability to SET ROLE. Then enable the subscription "admin_sub".
    revoke_superuser("regress_admin2")
    node_subscriber.safe_sql(
        "GRANT regress_alice TO regress_admin2 WITH INHERIT FALSE, SET TRUE;\n"
        "ALTER SUBSCRIPTION admin_sub ENABLE;"
    )

    # Because the initial data sync is working as the table owner, all
    # data should be copied.
    node_subscriber.wait_for_subscription_sync(node_publisher, "admin_sub")
    expect_replication(
        "alice.unpartitioned", 4, 7, 14, "table owner can do the initial data copy"
    )
