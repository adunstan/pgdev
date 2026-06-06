# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for already-propagated WAL segments ending in incomplete WAL records."""

import os


def test_026_overwrite_contrecord(create_pg):
    # Test: Create a physical replica that's missing the last WAL file,
    # then restart the primary to create a divergent WAL file and observe
    # that the replica replays the "overwrite contrecord" from that new
    # file and the standby promotes successfully.

    node = create_pg("primary", start=False, allows_streaming=True)
    # We need these settings for stability of WAL behavior.
    node.append_conf("""
autovacuum = off
wal_keep_size = 1GB
""")
    node.start()

    node.safe_sql("create table filler (a int, b text)")

    # Now consume all remaining room in the current WAL segment, leaving
    # space enough only for the start of a largish record.
    node.safe_sql(r"""
DO $$
DECLARE
    wal_segsize int := setting::int FROM pg_settings WHERE name = 'wal_segment_size';
    remain int;
    iters  int := 0;
BEGIN
    LOOP
        INSERT into filler
        select g, repeat(encode(sha256(g::text::bytea), 'hex'), (random() * 15 + 1)::int)
        from generate_series(1, 10) g;

        remain := wal_segsize - (pg_current_wal_insert_lsn() - '0/0') % wal_segsize;
        IF remain < 2 * setting::int from pg_settings where name = 'block_size' THEN
            RAISE log 'exiting after % iterations, % bytes to end of WAL segment', iters, remain;
            EXIT;
        END IF;
        iters := iters + 1;
    END LOOP;
END
$$;
""")

    initfile = node.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
    node.safe_sql(
        "SELECT pg_logical_emit_message(true, 'test 026', repeat('xyzxz', 123456))")

    endfile = node.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
    assert initfile != endfile, f"{initfile} differs from {endfile}"

    # Now stop abruptly, to avoid a stop checkpoint.  We can remove the tail
    # file afterwards, and on startup the large message should be overwritten
    # with new contents
    node.stop("immediate")

    os.unlink(os.path.join(node.data_dir, "pg_wal", endfile))

    # OK, create a standby at this spot.
    node.backup_fs_cold("backup")
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node, "backup", has_streaming=True)

    node_standby.start()
    node.start()

    node.safe_sql("create table foo (a text); insert into foo values ('hello')")
    node.safe_sql(
        "SELECT pg_logical_emit_message(true, 'test 026', 'AABBCC')")

    until_lsn = node.safe_sql("SELECT pg_current_wal_lsn()")
    caughtup_query = f"SELECT '{until_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    assert node_standby.poll_query_until(caughtup_query), \
        "Timed out while waiting for standby to catch up"

    assert node_standby.safe_sql("select * from foo") == "hello", \
        "standby replays past overwritten contrecord"

    # Verify message appears in standby's log
    assert node_standby.log_contains(
        r"successfully skipped missing contrecord at"), \
        "found log line in standby"

    # Verify promotion is successful
    node_standby.promote()

    node.stop()
    node_standby.stop()
