# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test whether WAL summaries are complete such that incremental backup
can be performed after promoting a standby at an arbitrary LSN.
"""

import os
import shutil


def _combine_backup(node, root_node, prior_backups, final_backup,
                    combine_mode=None):
    """Combine *prior_backups* + *final_backup* into *node*'s data dir.

    Runs ``pg_combinebackup`` with the prior backup paths and the final
    (incremental) backup path, writing the reconstructed cluster into *node*'s
    data directory, then writes this node's port/socket configuration so it can
    start.

    The framework's PostgresServer.init_from_backup does not support
    incremental/combine restores, so this helper drives pg_combinebackup
    directly instead.
    """
    data_path = node.data_dir
    # create_pg already ran initdb into data_dir; pg_combinebackup requires a
    # fresh output directory, so remove it first.
    if os.path.isdir(data_path):
        shutil.rmtree(data_path)

    prior_paths = [os.path.join(root_node.backup_dir, name)
                   for name in prior_backups]
    final_path = os.path.join(root_node.backup_dir, final_backup)

    combineargs = ["pg_combinebackup", "--debug"]
    if combine_mode is not None:
        combineargs.append(combine_mode)
    combineargs += prior_paths + [final_path, "--output", data_path]
    node.command_ok(combineargs, "combine backup for node " + node.name)

    # Mirror init_from_backup's base configuration for this node.
    node.append_conf("\n".join([
        "",
        f"port = {node.port}",
        "listen_addresses = ''",
        f"unix_socket_directories = '{node.host}'",
        "",
    ]))


def test_008_promote(create_pg):
    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_COMBINEBACKUP_MODE", "--copy")

    print(f"# testing using mode {mode}")

    # Set up a new database instance.
    node1 = create_pg("node1", start=False,
                      has_archiving=True, allows_streaming=True)
    node1.append_conf("summarize_wal = on")
    node1.append_conf("log_min_messages = debug1")
    node1.start()

    # Create a table and insert a test row into it.
    node1.safe_sql(
        "CREATE TABLE mytable (a int, b text);\n"
        "INSERT INTO mytable VALUES (1, 'avocado');\n")

    # Take a full backup.
    backup1path = os.path.join(node1.backup_dir, "backup1")
    node1.command_ok(
        ["pg_basebackup",
         "--pgdata", backup1path,
         "--no-sync",
         "--checkpoint", "fast"],
        "full backup from node1")

    # Checkpoint and record LSN after.
    node1.safe_sql("CHECKPOINT")
    lsn = node1.safe_sql("SELECT pg_current_wal_insert_lsn()")

    # Insert a second row on the original node.
    node1.safe_sql("INSERT INTO mytable VALUES (2, 'beetle');\n")

    # Now create a second node. We want this to stream from the first node and
    # then stop recovery at some arbitrary LSN, not just when it hits the end
    # of WAL, so use a recovery target.
    node2 = create_pg("node2", start=False)
    node2.init_from_backup(node1, "backup1", has_streaming=True)
    node2.append_conf(
        f"recovery_target_lsn = '{lsn}'\n"
        "recovery_target_action = 'pause'\n")
    node2.start()

    # Wait until recovery pauses, then promote.
    node2.poll_query_until(
        "SELECT pg_get_wal_replay_pause_state() = 'paused';")
    node2.safe_sql("SELECT pg_promote()")

    # Once promotion occurs, insert a second row on the new node.
    node2.poll_query_until("SELECT pg_is_in_recovery() = 'f';")
    node2.safe_sql("INSERT INTO mytable VALUES (2, 'blackberry');\n")

    # Now take an incremental backup. If WAL summarization didn't follow the
    # timeline change correctly, something should break at this point.
    backup2path = os.path.join(node1.backup_dir, "backup2")
    node2.command_ok(
        ["pg_basebackup",
         "--pgdata", backup2path,
         "--no-sync",
         "--checkpoint", "fast",
         "--incremental", os.path.join(backup1path, "backup_manifest")],
        "incremental backup from node2")

    # Restore the incremental backup and use it to create a new node.
    node3 = create_pg("node3", start=False)
    _combine_backup(node3, node1, ["backup1"], "backup2")
    node3.start()
