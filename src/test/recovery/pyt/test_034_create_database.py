# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test WAL replay for CREATE DATABASE .. STRATEGY WAL_LOG."""


def test_034_create_database(create_pg):
    node = create_pg("node")

    # This checks that any DDLs run on the template database that modify
    # pg_class are persisted after creating a database from it using the
    # WAL_LOG strategy, as a direct copy of the template database's pg_class is
    # used in this case.
    db_template = "template1"
    db_new = "test_db_1"

    # Create table.  It should persist on the template database.
    node.safe_sql(
        f"CREATE DATABASE {db_new} STRATEGY WAL_LOG TEMPLATE {db_template};")

    node.safe_sql(
        "CREATE TABLE tab_db_after_create_1 (a INT);", dbname=db_template)

    # Flush the changes affecting the template database, then replay them.
    node.safe_sql("CHECKPOINT;")

    node.stop("immediate")
    node.start()
    result = node.safe_sql(
        "SELECT count(*) FROM pg_class WHERE relname LIKE 'tab_db_%';",
        dbname=db_template)
    assert result == "1", \
        "check that table exists on template after crash, with checkpoint"

    # The new database should have no tables.
    result = node.safe_sql(
        "SELECT count(*) FROM pg_class WHERE relname LIKE 'tab_db_%';",
        dbname=db_new)
    assert result == "0", \
        "check that there are no tables from template on new database after crash"
