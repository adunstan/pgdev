# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test restartpoints during archive recovery."""


def test_045_archive_restartpoint(create_pg):
    archive_max_mb = 320
    wal_segsize = 1

    # Initialize primary node
    node_primary = create_pg(
        "primary",
        start=False,
        initdb_extra=["--wal-segsize", str(wal_segsize)],
        has_archiving=True,
        allows_streaming=True,
    )
    node_primary.start()
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    node_primary.safe_sql(
        "DO $$BEGIN FOR i IN 1.."
        + str(archive_max_mb // wal_segsize)
        + " LOOP CHECKPOINT; PERFORM pg_switch_wal(); END LOOP; END$$;"
    )

    # Force archiving of WAL file containing recovery target
    until_lsn = node_primary.lsn("write")
    node_primary.safe_sql("SELECT pg_switch_wal()")
    node_primary.stop()

    # Archive recovery
    node_restore = create_pg("restore", start=False)
    node_restore.init_from_backup(node_primary, backup_name, has_restoring=True)
    node_restore.append_conf(f"recovery_target_lsn = '{until_lsn}'")
    node_restore.append_conf("recovery_target_action = pause")
    node_restore.append_conf(f"max_wal_size = {2 * wal_segsize}")
    node_restore.append_conf("log_checkpoints = on")

    node_restore.start()

    # Wait until restore has replayed enough data
    caughtup_query = f"SELECT '{until_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    assert node_restore.poll_query_until(
        caughtup_query
    ), "Timed out while waiting for restore to catch up"

    node_restore.stop()
    assert True, "restore caught up"
