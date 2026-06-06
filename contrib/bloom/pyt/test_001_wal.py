# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that generic xlog records correctly replicate bloom indexes."""


def _test_index_replay(
    node_primary, node_standby, session_primary, session_standby, test_name
):
    # Wait for standby to catch up
    node_primary.wait_for_catchup(node_standby)

    queries = (
        "SELECT * FROM tst WHERE i = 0",
        "SELECT * FROM tst WHERE i = 3",
        "SELECT * FROM tst WHERE t = 'b'",
        "SELECT * FROM tst WHERE t = 'f'",
        "SELECT * FROM tst WHERE i = 3 AND t = 'c'",
        "SELECT * FROM tst WHERE i = 7 AND t = 'e'",
    )

    # Run test queries and compare their result
    primary_result = session_primary.query_tuples(*queries)
    standby_result = session_standby.query_tuples(*queries)

    assert primary_result == standby_result, f"{test_name}: query result matches"


def test_001_wal(create_pg):
    # Initialize primary node
    node_primary = create_pg("primary", allows_streaming=True)
    backup_name = "my_backup"

    # Take backup
    node_primary.backup(backup_name)

    # Create streaming standby linking to primary
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.start()

    # Create and initialize the sessions
    session_primary = node_primary.connect()
    session_standby = node_standby.connect()
    initset = """
   SET enable_seqscan=off;
   SET enable_bitmapscan=on;
   SET enable_indexscan=on;
"""
    session_primary.query_safe(initset)
    session_standby.query_safe(initset)

    # Create some bloom index on primary
    session_primary.query_safe("CREATE EXTENSION bloom;")
    session_primary.query_safe("CREATE TABLE tst (i int4, t text);")
    session_primary.query_safe(
        "INSERT INTO tst SELECT i%10, substr(encode(sha256(i::text::bytea), "
        "'hex'), 1, 1) FROM generate_series(1,10000) i;"
    )
    session_primary.query_safe(
        "CREATE INDEX bloomidx ON tst USING bloom (i, t) WITH (col1 = 3);"
    )

    # Test that queries give same result
    _test_index_replay(
        node_primary, node_standby, session_primary, session_standby, "initial"
    )

    # Run 10 cycles of table modification. Run test queries after each
    # modification.
    for i in range(1, 11):
        node_primary.safe_sql(f"DELETE FROM tst WHERE i = {i};")
        _test_index_replay(
            node_primary, node_standby, session_primary, session_standby, f"delete {i}"
        )
        node_primary.safe_sql("VACUUM tst;")
        _test_index_replay(
            node_primary, node_standby, session_primary, session_standby, f"vacuum {i}"
        )
        start = 100001 + (i - 1) * 10000
        end = 100000 + i * 10000
        node_primary.safe_sql(
            "INSERT INTO tst SELECT i%10, substr(encode(sha256(i::text::bytea), "
            f"'hex'), 1, 1) FROM generate_series({start},{end}) i;"
        )
        _test_index_replay(
            node_primary, node_standby, session_primary, session_standby, f"insert {i}"
        )
