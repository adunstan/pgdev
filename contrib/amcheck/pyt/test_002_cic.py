# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test CREATE INDEX CONCURRENTLY with concurrent modifications."""

import os

from pypg.util import TIMEOUT_DEFAULT


def test_002_cic(create_pg, tmp_path):
    #
    # Test set-up
    #
    node = create_pg("CIC_test", start=False)
    node.append_conf(
        "lock_timeout = " + str(1000 * TIMEOUT_DEFAULT) + "\n"
    )
    node.start()
    node.safe_sql("CREATE EXTENSION amcheck")
    node.safe_sql("CREATE TABLE tbl(i int, j jsonb)")
    node.safe_sql("CREATE INDEX idx ON tbl(i)")
    node.safe_sql("CREATE INDEX ginidx ON tbl USING gin(j)")

    #
    # Stress CIC with pgbench.
    #
    # pgbench might try to launch more than one instance of the CIC
    # transaction concurrently.  That would deadlock, so use an advisory
    # lock to ensure only one CIC runs at a time.
    #
    scripts = {
        "002_pgbench_concurrent_transaction": (
            "BEGIN;\n"
            "INSERT INTO tbl VALUES(0, '{\"a\":[[\"b\",{\"x\":1}],"
            "[\"b\",{\"x\":2}]],\"c\":3}');\n"
            "COMMIT;\n"
        ),
        "002_pgbench_concurrent_transaction_savepoints": (
            "BEGIN;\n"
            "SAVEPOINT s1;\n"
            "INSERT INTO tbl VALUES(0, '[[14,2,3]]');\n"
            "COMMIT;\n"
        ),
        "002_pgbench_concurrent_cic": (
            "SELECT pg_try_advisory_lock(42)::integer AS gotlock \\gset\n"
            "\\if :gotlock\n"
            "\tDROP INDEX CONCURRENTLY idx;\n"
            "\tCREATE INDEX CONCURRENTLY idx ON tbl(i);\n"
            "\tDROP INDEX CONCURRENTLY ginidx;\n"
            "\tCREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j);\n"
            "\tSELECT bt_index_check('idx',true);\n"
            "\tSELECT gin_index_check('ginidx');\n"
            "\tSELECT pg_advisory_unlock(42);\n"
            "\\endif\n"
        ),
    }
    # Files are ordered for determinism, matching _pgbench_make_files.
    file_opts = []
    for fn in sorted(scripts):
        path = os.path.join(str(tmp_path), fn)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(scripts[fn])
        file_opts += ["--file", path]

    node.command_checks_all(
        ["pgbench", "--no-vacuum", "--client=5", "--transactions=100",
         *file_opts],
        0,
        [r"actually processed"],
        [r"^$"],
        "concurrent INSERTs and CIC",
    )

    # Test bt_index_parent_check() with indexes created with
    # CREATE INDEX CONCURRENTLY.
    node.safe_sql("CREATE TABLE quebec(i int primary key)")
    # Insert two rows into index
    node.safe_sql(
        "INSERT INTO quebec SELECT i FROM generate_series(1, 2) s(i);"
    )

    # start background transaction
    in_progress_h = node.connect("postgres")
    in_progress_h.do("BEGIN", "SELECT pg_current_xact_id();")

    # delete one row from table, while background transaction is in progress
    node.safe_sql("DELETE FROM quebec WHERE i = 1;")
    # create index concurrently, which will skip the deleted row
    node.safe_sql("CREATE INDEX CONCURRENTLY oscar ON quebec(i);")

    # check index using bt_index_parent_check
    result = node.safe_sql(
        "SELECT bt_index_parent_check('oscar', heapallindexed => true)"
    )
    assert result == "", "bt_index_parent_check for CIC after removed row"

    in_progress_h.close()

    node.stop()
