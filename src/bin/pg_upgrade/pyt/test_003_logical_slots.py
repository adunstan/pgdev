# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Tests for upgrading logical replication slots."""

# Tests for upgrading logical replication slots

import os
import re


def _find_file(root, name_re):
    """Return the first path under *root* whose name matches *name_re*."""
    regex = re.compile(name_re)
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if regex.search(filename):
                return os.path.join(dirpath, filename)
    return None


def _slurp_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_003_logical_slots(create_pg, pg_bin):
    # Can be changed to test the other modes
    mode = os.environ.get("PG_TEST_PG_UPGRADE_MODE", "--copy")

    # Initialize old cluster
    oldpub = create_pg("oldpub", start=False, allows_streaming="logical")
    oldpub.append_conf("autovacuum = off")

    # Initialize new cluster
    newpub = create_pg("newpub", start=False, allows_streaming="logical")

    # During upgrade, when pg_restore performs CREATE DATABASE, bgwriter or
    # checkpointer may flush buffers and hold a file handle for the system
    # table.  So, if later due to some reason we need to re-create the file
    # with the same name like a TRUNCATE command on the same table, then the
    # command will fail if OS (such as older Windows versions) doesn't remove
    # an unlinked file completely till it is open.  The probability of seeing
    # this behavior is higher in this test because we use wal_level as logical
    # via allows_streaming => 'logical' which in turn set shared_buffers as
    # 1MB.
    newpub.append_conf(
        "bgwriter_lru_maxpages = 0\n"
        "checkpoint_timeout = 1h\n"
    )

    # Setup a common pg_upgrade command to be used by all the test cases
    pg_upgrade_cmd = [
        "pg_upgrade", "--no-sync",
        "--old-datadir", oldpub.data_dir,
        "--new-datadir", newpub.data_dir,
        "--old-bindir", oldpub.bindir,
        "--new-bindir", newpub.bindir,
        "--socketdir", newpub.host,
        "--old-port", str(oldpub.port),
        "--new-port", str(newpub.port),
        mode,
    ]

    # In a VPATH build, we'll be started in the source directory, but we want
    # to run pg_upgrade in the build directory so that any files generated
    # finish in it, like delete_old_cluster.{sh,bat}.
    tmp_cwd = os.path.join(newpub.basedir, "pg_upgrade_cwd")
    os.makedirs(tmp_cwd, exist_ok=True)
    os.chdir(tmp_cwd)

    # ------------------------------
    # TEST: Confirm pg_upgrade fails when the new cluster has wrong GUC values

    # Preparations for the subsequent test:
    # 1. Create two slots on the old cluster
    oldpub.start()
    oldpub.safe_sql(
        "SELECT pg_create_logical_replication_slot('test_slot1', 'test_decoding');"
        "SELECT pg_create_logical_replication_slot('test_slot2', 'test_decoding');"
        "SELECT pg_create_logical_replication_slot('test_slot3', 'test_decoding');"
    )
    oldpub.stop()

    # 2. Set 'max_replication_slots' to be less than the number of slots (2)
    #    present on the old cluster.
    newpub.append_conf("max_replication_slots = 1")

    # pg_upgrade will fail because the new cluster has insufficient
    # max_replication_slots
    pg_bin.command_checks_all(
        pg_upgrade_cmd,
        1,
        [
            r'"max_replication_slots" \(1\) must be greater than or equal to '
            r"the number of logical replication slots \(3\) on the old cluster"
        ],
        [r""],
        'run of pg_upgrade where the new cluster has insufficient '
        '"max_replication_slots"',
    )
    assert os.path.isdir(
        os.path.join(newpub.data_dir, "pg_upgrade_output.d")
    ), "pg_upgrade_output.d/ not removed after pg_upgrade failure"

    # Set 'max_replication_slots' to match the number of slots (3) present on
    # the old cluster.  Both slots will be used for subsequent tests.
    newpub.append_conf("max_replication_slots = 3")

    # ------------------------------
    # TEST: Confirm pg_upgrade fails when the slot still has unconsumed WAL
    # records

    # Preparations for the subsequent test:
    # 1. Generate extra WAL records. At this point none of the slots has
    #    consumed them.
    #
    # 2. Advance the slot test_slot2 up to the current WAL location, but
    #    test_slot1 still has unconsumed WAL records.
    #
    # 3. Emit a non-transactional message. This will cause test_slot2 to detect
    #    the unconsumed WAL record.
    #
    # 4. Advance the slot test_slots3 up to the current WAL location.
    oldpub.start()
    oldpub.safe_sql(
        "CREATE TABLE tbl AS SELECT generate_series(1, 10) AS a")
    oldpub.safe_sql(
        "SELECT pg_replication_slot_advance('test_slot2', pg_current_wal_lsn())")
    oldpub.safe_sql(
        "SELECT count(*) FROM pg_logical_emit_message('false', 'prefix', "
        "'This is a non-transactional message', true)")
    oldpub.safe_sql(
        "SELECT pg_replication_slot_advance('test_slot3', pg_current_wal_lsn())")
    oldpub.stop()

    # pg_upgrade will fail because there are slots still having unconsumed WAL
    # records
    pg_bin.command_checks_all(
        pg_upgrade_cmd,
        1,
        [
            r"Your installation contains logical replication slots that "
            r"cannot be upgraded\."
        ],
        [r""],
        "run of pg_upgrade of old cluster with slots having unconsumed WAL "
        "records",
    )

    # Verify the reason why the logical replication slot cannot be upgraded.
    # Find a txt file that contains a list of logical replication slots that
    # cannot be upgraded.  We cannot predict the file's path because the output
    # directory contains a milliseconds timestamp.
    slots_filename = _find_file(
        os.path.join(newpub.data_dir, "pg_upgrade_output.d"),
        r"invalid_logical_slots\.txt",
    )
    assert slots_filename is not None, "invalid_logical_slots.txt found"

    # Check the file content. While both test_slot1 and test_slot2 should be
    # reporting that they have unconsumed WAL records, test_slot3 should not be
    # reported as it has caught up.
    content = _slurp_file(slots_filename)
    assert re.search(
        r'The slot "test_slot1" has not consumed the WAL yet', content
    ), "the previous test failed due to unconsumed WALs"
    assert re.search(
        r'The slot "test_slot2" has not consumed the WAL yet', content
    ), "the previous test failed due to unconsumed WALs"
    assert not re.search(r"test_slot3", content), \
        "caught-up slot is not reported"

    # ------------------------------
    # TEST: Successful upgrade

    # Preparations for the subsequent test:
    # 1. Setup logical replication (first, cleanup slots from the previous
    #    tests)
    old_connstr = (
        f"host={oldpub.host} port={oldpub.port} dbname=postgres"
    )

    oldpub.start()
    oldpub.safe_sql(
        "SELECT * FROM pg_drop_replication_slot('test_slot1');"
        "SELECT * FROM pg_drop_replication_slot('test_slot2');"
        "SELECT * FROM pg_drop_replication_slot('test_slot3');"
        "CREATE PUBLICATION regress_pub FOR ALL TABLES;"
    )

    # Initialize subscriber cluster
    sub = create_pg("sub")

    sub.safe_sql("CREATE TABLE tbl (a int)")
    sub.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub CONNECTION '{old_connstr}' "
        "PUBLICATION regress_pub WITH (two_phase = 'true', failover = 'true')"
    )
    sub.wait_for_subscription_sync(oldpub, "regress_sub")

    # Also wait for two-phase to be enabled
    twophase_query = (
        "SELECT count(1) = 0 FROM pg_subscription "
        "WHERE subtwophasestate NOT IN ('e');"
    )
    assert sub.poll_query_until(twophase_query), \
        "Timed out while waiting for subscriber to enable twophase"

    # 2. Temporarily disable the subscription
    sub.safe_sql("ALTER SUBSCRIPTION regress_sub DISABLE")
    oldpub.stop()

    # pg_upgrade should be successful
    pg_bin.command_ok(pg_upgrade_cmd, "run of pg_upgrade of old cluster")

    # Check that the slot 'regress_sub' has migrated to the new cluster
    newpub.start()
    result = newpub.safe_sql(
        "SELECT slot_name, two_phase, failover FROM pg_replication_slots"
    )
    assert result == "regress_sub|t|t", "check the slot exists on new cluster"

    # Update the connection
    new_connstr = (
        f"host={newpub.host} port={newpub.port} dbname=postgres"
    )
    sub.safe_sql(
        f"ALTER SUBSCRIPTION regress_sub CONNECTION '{new_connstr}';"
        "ALTER SUBSCRIPTION regress_sub ENABLE;"
    )

    # Check whether changes on the new publisher get replicated to the
    # subscriber
    newpub.safe_sql("INSERT INTO tbl VALUES (generate_series(11, 20))")
    newpub.wait_for_catchup("regress_sub")
    result = sub.safe_sql("SELECT count(*) FROM tbl")
    assert result == "20", "check changes are replicated to the sub"

    # Clean up
    sub.stop()
    newpub.stop()
