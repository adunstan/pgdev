# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""This test aims to validate that taking an incremental backup fails when
wal_level has been changed to minimal between the full backup and the
attempted incremental backup.  With wal_level=minimal, WAL summarization is
disabled, so the summaries required to compute the incremental are missing.
"""

import os


def test_007_wal_level_minimal(create_pg):
    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_COMBINEBACKUP_MODE") or "--copy"
    print(f"# testing using mode {mode}")

    # Set up a new database instance.
    node1 = create_pg("node1", start=False, allows_streaming=True)
    node1.append_conf(
        "\n".join(
            [
                "summarize_wal = on",
                "wal_keep_size = '1GB'",
                "",
            ]
        )
    )
    node1.start()

    # Create a table and insert a test row into it.
    node1.safe_sql(
        "CREATE TABLE mytable (a int, b text);\n"
        "INSERT INTO mytable VALUES (1, 'finch');"
    )

    # Take a full backup.
    backup1path = os.path.join(node1.backup_dir, "backup1")
    node1.command_ok(
        ["pg_basebackup", "--pgdata", backup1path, "--no-sync", "--checkpoint", "fast"],
        "full backup",
    )

    # Switch to wal_level=minimal, which also requires max_wal_senders=0 and
    # summarize_wal=off
    # Each ALTER SYSTEM must run as its own top-level statement (the
    # in-process Session wraps a multi-statement string in one transaction,
    # and ALTER SYSTEM cannot run inside a transaction block).
    node1.safe_sql("ALTER SYSTEM SET wal_level = minimal;")
    node1.safe_sql("ALTER SYSTEM SET max_wal_senders = 0;")
    node1.safe_sql("ALTER SYSTEM SET summarize_wal = off;")
    node1.restart()

    # Insert a second row on the original node.
    node1.safe_sql("INSERT INTO mytable VALUES (2, 'gerbil');")

    # Revert configuration changes
    node1.safe_sql("ALTER SYSTEM RESET wal_level;")
    node1.safe_sql("ALTER SYSTEM RESET max_wal_senders;")
    node1.safe_sql("ALTER SYSTEM RESET summarize_wal;")
    node1.restart()

    # Now take an incremental backup.
    backup2path = os.path.join(node1.backup_dir, "backup2")
    node1.command_fails_like(
        [
            "pg_basebackup",
            "--pgdata",
            backup2path,
            "--no-sync",
            "--checkpoint",
            "fast",
            "--incremental",
            os.path.join(backup1path, "backup_manifest"),
        ],
        r"(?s)WAL summaries are required on timeline 1 from.*are incomplete",
        "incremental backup fails",
    )
