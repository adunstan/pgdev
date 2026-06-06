# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Check that a concurrent transaction doesn't cause false negatives in pg_check_visible()."""


def test_001_concurrent_transaction(create_pg):
    # Initialize the primary node
    node = create_pg("main", allows_streaming=True)

    # Initialize the streaming standby
    backup_name = "my_backup"
    node.backup(backup_name)
    standby = create_pg("standby", start=False)
    standby.init_from_backup(node, backup_name, has_streaming=True)
    standby.start()

    # Setup another database
    node.safe_sql("CREATE DATABASE other_database;")
    bsession = node.connect(dbname="other_database")

    # Run a concurrent transaction
    bsession.query("""
        BEGIN;
        SELECT txid_current();
    """)

    # Create a sample table and run vacuum
    node.safe_sql(
        "CREATE EXTENSION pg_visibility;\n"
        "CREATE TABLE vacuum_test AS SELECT 42 i;")
    node.safe_sql("VACUUM (disable_page_skipping) vacuum_test;")

    # Run pg_check_visible()
    result = node.safe_sql(
        "SELECT * FROM pg_check_visible('vacuum_test');")

    # There should be no false negatives
    assert result == "", "pg_check_visible() detects no errors"

    # Run pg_check_visible() on standby
    node.wait_for_catchup(standby)
    result = standby.safe_sql(
        "SELECT * FROM pg_check_visible('vacuum_test');")

    # There should be no false negatives either
    assert result == "", "pg_check_visible() detects no errors"

    # Shutdown
    bsession.query("COMMIT;")
    bsession.close()
    node.stop()
    standby.stop()
