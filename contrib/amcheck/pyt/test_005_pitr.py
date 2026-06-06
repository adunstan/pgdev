# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test integrity of intermediate states by PITR to those states."""

import re


def test_005_pitr(create_pg):
    # origin node: generate WAL records of interest.
    origin = create_pg("origin", start=False, has_archiving=True,
                       allows_streaming=True)
    origin.append_conf("autovacuum = off")
    origin.start()
    origin.backup("my_backup")

    # Create a table with each of 6 PK values spanning 1/4 of a block.  Delete
    # the first four, so one index leaf is eligible for deletion.  Make a
    # replication slot just so pg_walinspect will always have access to later
    # WAL.
    setup = """
BEGIN;
CREATE EXTENSION amcheck;
CREATE EXTENSION pg_walinspect;
CREATE TABLE not_leftmost (c text STORAGE PLAIN);
INSERT INTO not_leftmost
  SELECT repeat(n::text, database_block_size / 4)
  FROM generate_series(1,6) t(n), pg_control_init();
ALTER TABLE not_leftmost ADD CONSTRAINT not_leftmost_pk PRIMARY KEY (c);
DELETE FROM not_leftmost WHERE c ~ '^[1-4]';
SELECT pg_create_physical_replication_slot('for_walinspect', true, false);
COMMIT;
"""
    origin.safe_sql(setup)
    before_vacuum_lsn = origin.safe_sql("SELECT pg_current_wal_lsn()")

    # VACUUM to delete the aforementioned leaf page.  Force an XLogFlush() by
    # dropping a permanent table.  That way, the XLogReader infrastructure can
    # always see VACUUM's records, even under synchronous_commit=off.  Finally,
    # find the LSN of that VACUUM's last UNLINK_PAGE record.  The statements
    # are run individually (as psql does when fed a script) so that VACUUM,
    # which cannot run inside a transaction block, executes standalone.
    sess = origin.session()
    sess.do("SET synchronous_commit = off")
    sess.do("VACUUM (VERBOSE, INDEX_CLEANUP ON) not_leftmost")
    sess.do("CREATE TABLE XLogFlush ()")
    sess.do("DROP TABLE XLogFlush")
    unlink_lsn = sess.query_safe(f"""
SELECT max(start_lsn)
  FROM pg_get_wal_records_info('{before_vacuum_lsn}', 'FFFFFFFF/FFFFFFFF')
  WHERE resource_manager = 'Btree' AND record_type = 'UNLINK_PAGE'""")
    origin.stop()
    assert unlink_lsn, "did not find UNLINK_PAGE record"

    # replica node: amcheck at notable points in the WAL stream
    replica = create_pg("replica", start=False)
    replica.init_from_backup(origin, "my_backup", has_restoring=True)
    replica.append_conf(f"recovery_target_lsn = '{unlink_lsn}'")
    replica.append_conf("recovery_target_inclusive = off")
    replica.append_conf("recovery_target_action = promote")
    replica.start()
    assert replica.poll_query_until("SELECT pg_is_in_recovery() = 'f'"), \
        "Timed out while waiting for PITR promotion"

    # recovery done; run amcheck
    debug = "SET client_min_messages = 'debug1'"

    # bt_index_parent_check should pass and report the interrupted page
    # deletion via a debug message.  Run on a dedicated session so the
    # debug1 messages are captured through the notice processor (the
    # in-process equivalent of psql's stderr).
    sess = replica.connect()
    try:
        sess.do(debug)
        sess.clear_stderr()
        res = sess.query("SELECT bt_index_parent_check('not_leftmost_pk', true)")
        stderr = sess.get_stderr()
        print(stderr)
        assert res.error_message is None, "bt_index_parent_check passes"
        assert re.search(r"interrupted page deletion detected", stderr), \
            "bt_index_parent_check: interrupted page deletion detected"

        sess.clear_stderr()
        res = sess.query("SELECT bt_index_check('not_leftmost_pk', true)")
        stderr = sess.get_stderr()
        print(stderr)
        assert res.error_message is None, "bt_index_check passes"
    finally:
        sess.close()
