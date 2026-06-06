# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that pg_combinebackup fails cleanly when an incremental file has no
corresponding full file in the prior backup.
"""

import os
import shutil


def test_009_no_full_file(create_pg, tmp_path):
    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_COMBINEBACKUP_MODE", "--copy")

    print(f"# testing using mode {mode}")

    # Set up a new database instance.
    primary = create_pg("primary", has_archiving=True, allows_streaming=True)
    primary.append_conf("summarize_wal = on")
    primary.restart()

    # Take a full backup.
    backup1path = str(tmp_path / "backup1")
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup1path,
            "--no-sync",
            "--checkpoint", "fast",
        ],
        "full backup")

    # Take an incremental backup.
    backup2path = str(tmp_path / "backup2")
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup2path,
            "--no-sync",
            "--checkpoint", "fast",
            "--incremental", backup1path + "/backup_manifest",
        ],
        "incremental backup")

    # Find an incremental file in the incremental backup for which there is a
    # full file in the full backup. When we find one, replace the full file
    # with an incremental file.
    filelist = [f for f in os.listdir(f"{backup2path}/base/1")
                if f.startswith("INCREMENTAL.")]
    success = 0
    for iname in filelist:
        name = iname[len("INCREMENTAL."):]

        if os.path.isfile(f"{backup1path}/base/1/{name}"):
            shutil.copy(f"{backup2path}/base/1/{iname}",
                        f"{backup1path}/base/1/{iname}")
            os.unlink(f"{backup1path}/base/1/{name}")
            success = 1
            break

    assert success, "found a file to replace"

    # pg_combinebackup should fail.
    outpath = str(tmp_path / "out")
    primary.command_fails_like(
        [
            "pg_combinebackup", backup1path,
            backup2path, "--output", outpath,
        ],
        r"full backup contains unexpected incremental file",
        "pg_combinebackup fails")
