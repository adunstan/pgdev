# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test CREATE INDEX CONCURRENTLY with concurrent prepared-xact modifications."""

import os

from libpq import connect


def _pgbench(node, opts, expected_ret, stdout_res, stderr_res, msg, scripts,
             tmp_path):
    """Run pgbench against this node, checking its exit code and output.

    Writes each named script in *scripts* (a dict of name -> SQL body) into a
    temp file and runs pgbench with a -f for each, plus the split *opts*,
    against this node's postgres database, then checks the exit code and the
    stdout/stderr regex lists.
    """
    args = []
    for name, content in scripts.items():
        fnam = os.path.join(str(tmp_path), name)
        with open(fnam, "w", encoding="utf-8") as fh:
            fh.write(content)
        args += ["-f", fnam]

    cmd = ["pgbench"] + opts.split() + args
    node.pg_bin.command_checks_all(
        cmd, expected_ret, stdout_res, stderr_res, msg
    )


def test_003_cic_2pc(create_pg, tmp_path):
    #
    # Test set-up
    #
    node = create_pg("CIC_2PC_test", start=False)
    node.append_conf("max_prepared_transactions = 10")
    # lock_timeout = 1000 * timeout_default (timeout_default defaults to 180s).
    timeout_default = int(os.environ.get("PG_TEST_TIMEOUT_DEFAULT", "180"))
    node.append_conf(f"lock_timeout = {1000 * timeout_default}")
    node.start()
    node.safe_sql("CREATE EXTENSION amcheck")
    node.safe_sql("CREATE TABLE tbl(i int, j jsonb)")

    #
    # Run 3 overlapping 2PC transactions with CIC
    #
    # We have two concurrent background sessions: main_h for INSERTs and cic_h
    # for CIC.  Also, we use a non-background (safe_sql) session for some
    # COMMIT PREPARED statements.
    #

    main_h = connect(node=node)

    main_h.do_async(
        """
BEGIN;
INSERT INTO tbl VALUES(0, '[[14,2,3]]');
""")

    cic_h = connect(node=node)

    cic_h.setnonblocking(1)

    cic_h.enterPipelineMode()

    cic_h.do_pipeline(
        """
CREATE INDEX CONCURRENTLY idx ON tbl(i)
""")

    cic_h.pipelineSync()

    cic_h.do_pipeline(
        """
CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j)
""")

    cic_h.pipelineSync()

    main_h.wait_for_completion()
    main_h.do_async(
        """
PREPARE TRANSACTION 'a';
""")

    main_h.wait_for_completion()
    main_h.do_async(
        """
BEGIN;
INSERT INTO tbl VALUES(0, '[[14,2,3]]');
""")

    node.safe_sql("COMMIT PREPARED 'a';")

    main_h.wait_for_completion()
    main_h.do_async(
        """
PREPARE TRANSACTION 'b';
BEGIN;
INSERT INTO tbl VALUES(0, '"mary had a little lamb"');
""")

    # Our in-process safe_sql reuses a live connection, so without an explicit
    # wait it could fire COMMIT PREPARED before the already-flushed async PREPARE
    # lands.  Wait for 'b' to become a visible prepared xact before committing it.
    assert node.poll_query_until(
        "SELECT count(*) = 1 FROM pg_prepared_xacts WHERE gid = 'b'"
    ), "prepared transaction 'b' did not appear"
    node.safe_sql("COMMIT PREPARED 'b';")

    main_h.wait_for_completion()
    main_h.do(
        "PREPARE TRANSACTION 'c';",
        "COMMIT PREPARED 'c';")

    main_h.close()

    # called twice out of an abundance of caution about pipeline mode
    cic_h.wait_for_completion()
    cic_h.wait_for_completion()
    cic_h.close()

    result = node.safe_sql("SELECT bt_index_check('idx',true)")
    assert result == "", "bt_index_check after overlapping 2PC"

    result = node.safe_sql("SELECT gin_index_check('ginidx')")
    assert result == "", "gin_index_check after overlapping 2PC"

    #
    # Server restart shall not change whether prepared xact blocks CIC
    #

    node.safe_sql("""
BEGIN;
INSERT INTO tbl VALUES(0, '{"a":[["b",{"x":1}],["b",{"x":2}]],"c":3}');
PREPARE TRANSACTION 'spans_restart';
BEGIN;
CREATE TABLE unused ();
PREPARE TRANSACTION 'persists_forever';
""")
    node.restart()

    reindex_h = connect(node=node)
    reindex_h.do_async(
        """
DROP INDEX CONCURRENTLY idx;
CREATE INDEX CONCURRENTLY idx ON tbl(i);
DROP INDEX CONCURRENTLY ginidx;
CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j);
""")

    node.safe_sql("COMMIT PREPARED 'spans_restart'")
    reindex_h.wait_for_completion()
    reindex_h.close()
    result = node.safe_sql("SELECT bt_index_check('idx',true)")
    assert result == "", "bt_index_check after 2PC and restart"
    result = node.safe_sql("SELECT gin_index_check('ginidx')")
    assert result == "", "gin_index_check after 2PC and restart"

    #
    # Stress CIC+2PC with pgbench
    #
    # pgbench might try to launch more than one instance of the CIC
    # transaction concurrently.  That would deadlock, so use an advisory
    # lock to ensure only one CIC runs at a time.

    # Fix broken index first
    node.safe_sql("REINDEX TABLE tbl;")

    # Run pgbench.
    _pgbench(
        node,
        "--no-vacuum --client=5 --transactions=100",
        0,
        [r"actually processed"],
        [r"^$"],
        "concurrent INSERTs w/ 2PC and CIC",
        {
            "003_pgbench_concurrent_2pc": """
                BEGIN;
                INSERT INTO tbl VALUES(0,'null');
                PREPARE TRANSACTION 'c:client_id';
                COMMIT PREPARED 'c:client_id';
              """,
            "003_pgbench_concurrent_2pc_savepoint": """
                BEGIN;
                SAVEPOINT s1;
                INSERT INTO tbl VALUES(0,'[false, "jnvaba", -76, 7, {"_": [1]}, 9]');
                PREPARE TRANSACTION 'c:client_id';
                COMMIT PREPARED 'c:client_id';
              """,
            "003_pgbench_concurrent_cic": """
                SELECT pg_try_advisory_lock(42)::integer AS gotlock \\gset
                \\if :gotlock
                    DROP INDEX CONCURRENTLY idx;
                    CREATE INDEX CONCURRENTLY idx ON tbl(i);
                    SELECT bt_index_check('idx',true);
                    SELECT pg_advisory_unlock(42);
                \\endif
              """,
            "004_pgbench_concurrent_ric": """
                SELECT pg_try_advisory_lock(42)::integer AS gotlock \\gset
                \\if :gotlock
                    REINDEX INDEX CONCURRENTLY idx;
                    SELECT bt_index_check('idx',true);
                    SELECT pg_advisory_unlock(42);
                \\endif
              """,
            "005_pgbench_concurrent_cic": """
                SELECT pg_try_advisory_lock(42)::integer AS gotginlock \\gset
                \\if :gotginlock
                    DROP INDEX CONCURRENTLY ginidx;
                    CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j);
                    SELECT gin_index_check('ginidx');
                    SELECT pg_advisory_unlock(42);
                \\endif
              """,
            "006_pgbench_concurrent_ric": """
                SELECT pg_try_advisory_lock(42)::integer AS gotginlock \\gset
                \\if :gotginlock
                    REINDEX INDEX CONCURRENTLY ginidx;
                    SELECT gin_index_check('ginidx');
                    SELECT pg_advisory_unlock(42);
                \\endif
              """,
        },
        tmp_path,
    )

    node.stop()
