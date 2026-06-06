# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test for point-in-time recovery (PITR) with prepared transactions."""


def test_023_pitr_prepared_xact(create_pg):
    # Initialize and start primary node with WAL archiving
    node_primary = create_pg(
        "primary", start=False, has_archiving=True, allows_streaming=True)
    node_primary.append_conf("""
max_prepared_transactions = 10""")
    node_primary.start()

    # Take backup
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Initialize node for PITR targeting a very specific restore point, just
    # after a PREPARE TRANSACTION is issued so as we finish with a promoted
    # node where this 2PC transaction needs an explicit COMMIT PREPARED.
    node_pitr = create_pg("node_pitr", start=False)
    node_pitr.init_from_backup(
        node_primary, backup_name,
        standby=False,
        has_restoring=True)
    node_pitr.append_conf("""
recovery_target_name = 'rp'
recovery_target_action = 'promote'""")

    # Workload with a prepared transaction and the target restore point.
    # The prepared transaction is issued as its own command because libpq
    # wraps a multi-statement query in a single implicit transaction.
    node_primary.safe_sql("CREATE TABLE foo(i int)")
    node_primary.safe_sql("""
BEGIN;
INSERT INTO foo VALUES(1);
PREPARE TRANSACTION 'fooinsert';""")
    node_primary.safe_sql("SELECT pg_create_restore_point('rp')")
    node_primary.safe_sql("INSERT INTO foo VALUES(2)")

    # Find next WAL segment to be archived
    walfile_to_be_archived = node_primary.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_lsn());")

    # Make WAL segment eligible for archival
    node_primary.safe_sql("SELECT pg_switch_wal()")

    # Wait until the WAL segment has been archived.
    archive_wait_query = (
        f"SELECT '{walfile_to_be_archived}' <= last_archived_wal "
        "FROM pg_stat_archiver;"
    )
    assert node_primary.poll_query_until(archive_wait_query), \
        "Timed out while waiting for WAL segment to be archived"

    # Now start the PITR node.
    node_pitr.start()

    # Wait until the PITR node exits recovery.
    assert node_pitr.poll_query_until("SELECT pg_is_in_recovery() = 'f';"), \
        "Timed out while waiting for PITR promotion"

    # Commit the prepared transaction in the latest timeline and check its
    # result.  There should only be one row in the table, coming from the
    # prepared transaction.  The row from the INSERT after the restore point
    # should not show up, since our recovery target was older than the second
    # INSERT done.
    node_pitr.safe_sql("COMMIT PREPARED 'fooinsert';")
    result = node_pitr.safe_sql("SELECT * FROM foo;")
    assert result == "1", "check table contents after COMMIT PREPARED"

    # Insert more data and do a checkpoint.  These should be generated on the
    # timeline chosen after the PITR promotion.
    node_pitr.safe_sql("""
INSERT INTO foo VALUES(3);
CHECKPOINT;""")

    # Enforce recovery, the checkpoint record generated previously should
    # still be found.
    node_pitr.stop("immediate")
    node_pitr.start()
