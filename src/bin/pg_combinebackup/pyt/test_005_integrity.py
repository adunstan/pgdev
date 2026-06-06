# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""This test aims to validate that an incremental backup can be combined
with a valid prior backup and that it cannot be combined with an invalid
prior backup.

FRAMEWORK NOTES:

  * The test uses two clusters (node1, node2).  Every create_pg() runs a fresh
    initdb into its own data directory, so the two nodes already have different
    system identifiers.

  * All backups are taken into node1's backup directory, including those taken
    from node2.
"""

import os
import shutil


def test_005_integrity(create_pg):
    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_COMBINEBACKUP_MODE", "--copy")
    print(f"# testing using mode {mode}")

    # Set up a new database instance.
    node1 = create_pg("node1", start=False, has_archiving=True,
                      allows_streaming=True)
    node1.append_conf("summarize_wal = on")
    node1.start()

    # Create a file called INCREMENTAL.config in the root directory of the
    # first database instance. We only recognize INCREMENTAL.${original_name}
    # files under base and global and in tablespace directories, so this
    # shouldn't cause anything to fail.
    strangely_named_config_file = os.path.join(node1.data_dir,
                                               "INCREMENTAL.config")
    with open(strangely_named_config_file, "w", encoding="utf-8"):
        pass

    # Set up another new database instance.  A separate cluster is created
    # with a different system ID.
    node2 = create_pg("node2", start=False, has_archiving=True,
                      allows_streaming=True)
    node2.append_conf("summarize_wal = on")
    node2.start()

    # Take a full backup from node1.
    backup1path = os.path.join(node1.backup_dir, "backup1")
    node1.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup1path,
            "--no-sync",
            "--checkpoint", "fast",
        ],
        "full backup from node1")

    # Now take an incremental backup.
    backup2path = os.path.join(node1.backup_dir, "backup2")
    node1.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup2path,
            "--no-sync",
            "--checkpoint", "fast",
            "--incremental", os.path.join(backup1path, "backup_manifest"),
        ],
        "incremental backup from node1")

    # Now take another incremental backup.
    backup3path = os.path.join(node1.backup_dir, "backup3")
    node1.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup3path,
            "--no-sync",
            "--checkpoint", "fast",
            "--incremental", os.path.join(backup2path, "backup_manifest"),
        ],
        "another incremental backup from node1")

    # Take a full backup from node2.
    backupother1path = os.path.join(node1.backup_dir, "backupother1")
    node2.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backupother1path,
            "--no-sync",
            "--checkpoint", "fast",
        ],
        "full backup from node2")

    # Take an incremental backup from node2.
    backupother2path = os.path.join(node1.backup_dir, "backupother2")
    node2.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backupother2path,
            "--no-sync",
            "--checkpoint", "fast",
            "--incremental", os.path.join(backupother1path, "backup_manifest"),
        ],
        "incremental backup from node2")

    # Result directory.
    resultpath = os.path.join(node1.backup_dir, "result")

    # Can't combine 2 full backups.
    node1.command_fails_like(
        [
            "pg_combinebackup", backup1path, backup1path,
            "--output", resultpath,
            mode,
        ],
        r"is a full backup, but only the first backup should be a full backup",
        "can't combine full backups")

    # Can't combine 2 incremental backups.
    node1.command_fails_like(
        [
            "pg_combinebackup", backup2path, backup2path,
            "--output", resultpath,
            mode,
        ],
        r"is an incremental backup, but the first backup should be a full backup",
        "can't combine full backups")

    # Can't combine full backup with an incremental backup from a different
    # system.
    node1.command_fails_like(
        [
            "pg_combinebackup", backup1path, backupother2path,
            "--output", resultpath,
            mode,
        ],
        r"expected system identifier.*but found",
        "can't combine backups from different nodes")

    # Can't combine when different manifest system identifier
    os.rename(os.path.join(backup2path, "backup_manifest"),
              os.path.join(backup2path, "backup_manifest.orig"))
    shutil.copy(os.path.join(backupother2path, "backup_manifest"),
                os.path.join(backup2path, "backup_manifest"))
    node1.command_fails_like(
        [
            "pg_combinebackup", backup1path, backup2path, backup3path,
            "--output", resultpath,
            mode,
        ],
        r" manifest system identifier is .*, but control file has ",
        "can't combine backups with different manifest system identifier ")
    # Restore the backup state
    os.replace(os.path.join(backup2path, "backup_manifest.orig"),
               os.path.join(backup2path, "backup_manifest"))

    # Can't omit a required backup.
    node1.command_fails_like(
        [
            "pg_combinebackup", backup1path, backup3path,
            "--output", resultpath,
            mode,
        ],
        r"starts at LSN.*but expected",
        "can't omit a required backup")

    # Can't combine backups in the wrong order.
    node1.command_fails_like(
        [
            "pg_combinebackup", backup1path, backup3path, backup2path,
            "--output", resultpath,
            mode,
        ],
        r"starts at LSN.*but expected",
        "can't combine backups in the wrong order")

    # Can combine 3 backups that match up properly.
    node1.command_ok(
        [
            "pg_combinebackup", backup1path, backup2path, backup3path,
            "--output", resultpath,
            mode,
        ],
        "can combine 3 matching backups")
    shutil.rmtree(resultpath)

    # Can combine full backup with first incremental.
    synthetic12path = os.path.join(node1.backup_dir, "synthetic12")
    node1.command_ok(
        [
            "pg_combinebackup", backup1path, backup2path,
            "--output", synthetic12path,
            mode,
        ],
        "can combine 2 matching backups")

    # Can combine result of previous step with second incremental.
    node1.command_ok(
        [
            "pg_combinebackup", synthetic12path, backup3path,
            "--output", resultpath,
            mode,
        ],
        "can combine synthetic backup with later incremental")
    shutil.rmtree(resultpath)

    # Can't combine result of 1+2 with 2.
    node1.command_fails_like(
        [
            "pg_combinebackup", synthetic12path, backup2path,
            "--output", resultpath,
            mode,
        ],
        r"starts at LSN.*but expected",
        "can't combine synthetic backup with included incremental")
