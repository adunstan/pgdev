# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test replay of tablespace/database creation/drop."""

import shutil
import time

from pypg.util import TIMEOUT_DEFAULT


def _run_script(node, script):
    """Run a multi-statement SQL script as separate statements.

    The in-process libpq session sends a multi-statement string as a single
    implicit transaction, which CREATE DATABASE / CREATE TABLESPACE reject.
    Split on ';' and run each statement separately
    on the node's cached session so that session GUCs (e.g.
    allow_in_place_tablespaces) persist across statements.
    """
    for stmt in script.split(";"):
        stmt = stmt.strip()
        if stmt:
            node.safe_sql(stmt)


def _test_tablespace(create_pg, strategy):
    node_primary = create_pg(f"primary1_{strategy}", allows_streaming=True)
    _run_script(
        node_primary,
        """
            SET allow_in_place_tablespaces=on;
            CREATE TABLESPACE dropme_ts1 LOCATION '';
            CREATE TABLESPACE dropme_ts2 LOCATION '';
            CREATE TABLESPACE source_ts  LOCATION '';
            CREATE TABLESPACE target_ts  LOCATION '';
            CREATE DATABASE template_db IS_TEMPLATE = true;
            SELECT pg_create_physical_replication_slot('slot', true);
        """,
    )
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    node_standby = create_pg(f"standby2_{strategy}", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.append_conf("allow_in_place_tablespaces = on")
    node_standby.append_conf("primary_slot_name = slot")
    node_standby.start()

    # Make sure the connection is made
    node_primary.wait_for_catchup(node_standby, "write")

    # Do immediate shutdown just after a sequence of CREATE DATABASE / DROP
    # DATABASE / DROP TABLESPACE. This causes CREATE DATABASE WAL records
    # to be applied to already-removed directories.
    query = """
        CREATE DATABASE dropme_db1 WITH TABLESPACE dropme_ts1 STRATEGY=<STRATEGY>;
        CREATE TABLE t (a int) TABLESPACE dropme_ts2;
        CREATE DATABASE dropme_db2 WITH TABLESPACE dropme_ts2 STRATEGY=<STRATEGY>;
        CREATE DATABASE moveme_db TABLESPACE source_ts STRATEGY=<STRATEGY>;
        ALTER DATABASE moveme_db SET TABLESPACE target_ts;
        CREATE DATABASE newdb TEMPLATE template_db STRATEGY=<STRATEGY>;
        ALTER DATABASE template_db IS_TEMPLATE = false;
        DROP DATABASE dropme_db1;
        DROP TABLE t;
        DROP DATABASE dropme_db2; DROP TABLESPACE dropme_ts2;
        DROP TABLESPACE source_ts;
        DROP DATABASE template_db;
    """
    query = query.replace("<STRATEGY>", strategy)

    _run_script(node_primary, query)
    node_primary.wait_for_catchup(node_standby, "write")

    # show "create missing directory" log message
    node_standby.safe_sql("ALTER SYSTEM SET log_min_messages TO debug1;")
    node_standby.stop("immediate")
    # Should restart ignoring directory creation error.
    started = True
    try:
        node_standby.start()
    except Exception:
        started = False
    assert started, f"standby node started for {strategy}"
    node_standby.stop("immediate")


def test_033_replay_tsp_drops(create_pg):
    _test_tablespace(create_pg, "FILE_COPY")
    _test_tablespace(create_pg, "WAL_LOG")

    # Ensure that a missing tablespace directory during create database
    # replay immediately causes panic if the standby has already reached
    # consistent state (archive recovery is in progress).  This is
    # effective only for CREATE DATABASE WITH STRATEGY=FILE_COPY.

    node_primary = create_pg("primary2", allows_streaming=True)

    # Create tablespace
    _run_script(
        node_primary,
        """
            SET allow_in_place_tablespaces=on;
            CREATE TABLESPACE ts1 LOCATION ''
        """,
    )
    node_primary.safe_sql(
        "CREATE DATABASE db1 WITH TABLESPACE ts1 STRATEGY=FILE_COPY")

    # Take backup
    backup_name = "my_backup"
    node_primary.backup(backup_name)
    node_standby = create_pg("standby3", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.append_conf("allow_in_place_tablespaces = on")
    node_standby.start()

    # Make sure standby reached consistency and starts accepting connections
    assert node_standby.poll_query_until("SELECT 1", "1")

    # Remove standby tablespace directory so it will be missing when
    # replay resumes.
    tspoid = node_standby.safe_sql(
        "SELECT oid FROM pg_tablespace WHERE spcname = 'ts1';")
    tspdir = node_standby.data_dir + f"/pg_tblspc/{tspoid}"
    shutil.rmtree(tspdir)

    logstart = node_standby.log_position()

    # Create a database in the tablespace and a table in default tablespace
    _run_script(
        node_primary,
        """
            CREATE TABLE should_not_replay_insertion(a int);
            CREATE DATABASE db2 WITH TABLESPACE ts1 STRATEGY=FILE_COPY;
            INSERT INTO should_not_replay_insertion VALUES (1);
        """,
    )

    # Standby should fail and should not silently skip replaying the wal
    # In this test, PANIC turns into WARNING by allow_in_place_tablespaces.
    # Check the log messages instead of confirming standby failure.
    max_attempts = TIMEOUT_DEFAULT * 10
    detected = False
    while max_attempts >= 0:
        if node_standby.log_contains(
                r"WARNING: ( [A-Z0-9]+:)? creating missing directory: pg_tblspc/",
                offset=logstart):
            detected = True
            break
        max_attempts -= 1
        time.sleep(0.1)
    assert detected, "invalid directory creation is detected"
