# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""This test aims to validate that restoring an incremental backup works
properly even when the reference backup is on a different timeline.
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


def test_003_timeline(create_pg, tmp_path):
    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_COMBINEBACKUP_MODE", "--copy")

    print(f"# testing using mode {mode}")

    # Set up a new database instance.
    node1 = create_pg("node1", start=False,
                      has_archiving=True, allows_streaming=True)
    node1.append_conf("summarize_wal = on")
    node1.start()

    # Create a table and insert a test row into it.
    node1.safe_sql(
        "CREATE TABLE mytable (a int, b text);\n"
        "INSERT INTO mytable VALUES (1, 'aardvark');\n")

    # Take a full backup.
    backup1path = os.path.join(node1.backup_dir, "backup1")
    node1.command_ok(
        ["pg_basebackup",
         "--pgdata", backup1path,
         "--no-sync",
         "--checkpoint", "fast"],
        "full backup from node1")

    # Insert a second row on the original node.
    node1.safe_sql("INSERT INTO mytable VALUES (2, 'beetle');\n")

    # Now take an incremental backup.
    backup2path = os.path.join(node1.backup_dir, "backup2")
    node1.command_ok(
        ["pg_basebackup",
         "--pgdata", backup2path,
         "--no-sync",
         "--checkpoint", "fast",
         "--incremental", os.path.join(backup1path, "backup_manifest")],
        "incremental backup from node1")

    # Restore the incremental backup and use it to create a new node.
    node2 = create_pg("node2", start=False)
    _combine_backup(node2, node1, ["backup1"], "backup2")
    node2.start()

    # Insert rows on both nodes.
    node1.safe_sql("INSERT INTO mytable VALUES (3, 'crab');\n")
    node2.safe_sql("INSERT INTO mytable VALUES (4, 'dingo');\n")

    # Take another incremental backup, from node2, based on backup2 from node1.
    backup3path = os.path.join(node1.backup_dir, "backup3")
    node2.command_ok(
        ["pg_basebackup",
         "--pgdata", backup3path,
         "--no-sync",
         "--checkpoint", "fast",
         "--incremental", os.path.join(backup2path, "backup_manifest")],
        "incremental backup from node2")

    # Restore the incremental backup and use it to create a new node.
    node3 = create_pg("node3", start=False)
    _combine_backup(node3, node1, ["backup1", "backup2"], "backup3",
                    combine_mode=mode)
    node3.start()

    # Let's insert one more row.
    node3.safe_sql("INSERT INTO mytable VALUES (5, 'elephant');\n")

    # Now check that we have the expected rows.
    result = node3.safe_sql(
        "select string_agg(a::text, ':'), string_agg(b, ':') from mytable;\n")
    assert result == "1:2:4:5|aardvark:beetle:dingo:elephant"

    # Let's also verify all the backups.
    for backup_name in ("backup1", "backup2", "backup3"):
        node1.command_ok(
            ["pg_verifybackup", os.path.join(node1.backup_dir, backup_name)],
            f"verify backup {backup_name}")
