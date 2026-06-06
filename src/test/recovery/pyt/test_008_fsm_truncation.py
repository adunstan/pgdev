# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test FSM-driven INSERT just after truncation clears FSM slots indicating
free space in removed blocks.

The FSM mustn't return a page that doesn't exist (anymore).
"""


def test_008_fsm_truncation(create_pg):
    node_primary = create_pg("primary", start=False, allows_streaming=True)

    node_primary.append_conf(
        """
wal_log_hints = on
max_prepared_transactions = 5
autovacuum = off
"""
    )

    # Create a primary node and its standby, initializing both with some data
    # at the same time.
    node_primary.start()

    node_primary.backup("primary_backup")
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_primary, "primary_backup", has_streaming=True)
    node_standby.start()

    node_primary.safe_sql(
        """
create table testtab (a int, b char(100));
insert into testtab select generate_series(1,1000), 'foo';
insert into testtab select generate_series(1,1000), 'foo';
delete from testtab where ctid > '(8,0)';
"""
    )

    # Take a lock on the table to prevent following vacuum from truncating it
    node_primary.safe_sql(
        """
begin;
lock table testtab in row share mode;
prepare transaction 'p1';
"""
    )

    # Vacuum, update FSM without truncation
    node_primary.safe_sql("vacuum verbose testtab")

    # Force a checkpoint
    node_primary.safe_sql("checkpoint")

    # Now do some more insert/deletes, another vacuum to ensure full-page writes
    # are done
    node_primary.safe_sql(
        """
insert into testtab select generate_series(1,1000), 'foo';
delete from testtab where ctid > '(8,0)';
"""
    )
    node_primary.safe_sql("vacuum verbose testtab;")

    # Ensure all buffers are now clean on the standby
    node_standby.safe_sql("checkpoint")

    # Release the lock, vacuum again which should lead to truncation
    node_primary.safe_sql("rollback prepared 'p1';")
    node_primary.safe_sql("vacuum verbose testtab;")

    node_primary.safe_sql("checkpoint")
    until_lsn = node_primary.safe_sql("SELECT pg_current_wal_lsn();")

    # Wait long enough for standby to receive and apply all WAL
    caughtup_query = f"SELECT '{until_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    assert node_standby.poll_query_until(
        caughtup_query
    ), "Timed out while waiting for standby to catch up"

    # Promote the standby
    node_standby.promote()
    node_standby.safe_sql("checkpoint")

    # Restart to discard in-memory copy of FSM
    node_standby.restart()

    # Insert should work on standby
    node_standby.safe_sql("insert into testtab select generate_series(1,1000), 'foo';")
