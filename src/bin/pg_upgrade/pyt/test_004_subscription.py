# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test for pg_upgrade of logical subscription."""

# Test for pg_upgrade of logical subscription. Note that after the successful
# upgrade test, we can't use the old cluster to prevent failing in --link mode.

import os
import re
import shutil


def _find_file(root, name_re):
    """Return the path of a file under *root* whose name matches *name_re*."""
    pattern = re.compile(name_re)
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if pattern.search(filename):
                return os.path.join(dirpath, filename)
    return None


def test_004_subscription(create_pg, tmp_path):
    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_UPGRADE_MODE") or "--copy"

    # Initialize publisher node
    publisher = create_pg("publisher", allows_streaming="logical")

    # Initialize the old subscriber node
    old_sub = create_pg("old_sub", allows_streaming="physical")
    oldbindir = old_sub.bindir

    # Initialize the new subscriber
    new_sub = create_pg("new_sub", allows_streaming="physical", start=False)
    newbindir = new_sub.bindir

    # In a VPATH build, we'll be started in the source directory, but we want
    # to run pg_upgrade in the build directory so that any files generated
    # finish in it, like delete_old_cluster.{sh,bat}.
    os.chdir(str(tmp_path))

    # Remember a connection string for the publisher node. It would be used
    # several times.
    connstr = f"host={publisher.host} port={publisher.port} dbname=postgres"

    # ------------------------------------------------------
    # Check that pg_upgrade fails when max_active_replication_origins configured
    # in the new cluster is less than the number of subscriptions in the old
    # cluster.
    # ------------------------------------------------------
    # It is sufficient to use disabled subscription to test upgrade failure.
    publisher.safe_sql("CREATE PUBLICATION regress_pub1")
    old_sub.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub1 CONNECTION '{connstr}' "
        "PUBLICATION regress_pub1 WITH (enabled = false)"
    )

    old_sub.stop()

    new_sub.append_conf("max_active_replication_origins = 0")

    # pg_upgrade will fail because the new cluster has insufficient
    # max_active_replication_origins.
    new_sub.command_checks_all(
        [
            "pg_upgrade",
            "--no-sync",
            "--old-datadir", old_sub.data_dir,
            "--new-datadir", new_sub.data_dir,
            "--old-bindir", oldbindir,
            "--new-bindir", newbindir,
            "--socketdir", new_sub.host,
            "--old-port", old_sub.port,
            "--new-port", new_sub.port,
            mode,
            "--check",
        ],
        1,
        [
            r'"max_active_replication_origins" \(0\) must be greater than or '
            r"equal to the number of subscriptions \(1\) on the old cluster"
        ],
        [r""],
        "run of pg_upgrade where the new cluster has insufficient "
        "max_active_replication_origins",
    )

    # Reset max_active_replication_origins
    new_sub.append_conf("max_active_replication_origins = 10")

    # Cleanup
    publisher.safe_sql("DROP PUBLICATION regress_pub1")
    old_sub.start()
    old_sub.safe_sql("DROP SUBSCRIPTION regress_sub1;")

    # ------------------------------------------------------
    # Check that pg_upgrade fails when max_replication_slots configured in the
    # new cluster is less than the number of logical slots in the old cluster +
    # 1 when subscription's retain_dead_tuples option is enabled.
    # ------------------------------------------------------
    # It is sufficient to use disabled subscription to test upgrade failure.

    publisher.safe_sql("CREATE PUBLICATION regress_pub1")
    old_sub.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub1 CONNECTION '{connstr}' "
        "PUBLICATION regress_pub1 WITH (enabled = false, retain_dead_tuples = true)"
    )

    old_sub.stop()

    new_sub.append_conf("max_replication_slots = 0")

    # pg_upgrade will fail because the new cluster has insufficient
    # max_replication_slots.
    new_sub.command_checks_all(
        [
            "pg_upgrade",
            "--no-sync",
            "--old-datadir", old_sub.data_dir,
            "--new-datadir", new_sub.data_dir,
            "--old-bindir", oldbindir,
            "--new-bindir", newbindir,
            "--socketdir", new_sub.host,
            "--old-port", old_sub.port,
            "--new-port", new_sub.port,
            mode,
            "--check",
        ],
        1,
        [
            r'"max_replication_slots" \(0\) must be greater than or equal to '
            r"the number of logical replication slots on the old cluster plus "
            r"one additional slot required for retaining conflict detection "
            r"information \(1\)"
        ],
        [r""],
        "run of pg_upgrade where the new cluster has insufficient "
        "max_replication_slots",
    )

    # Reset max_replication_slots
    new_sub.append_conf("max_replication_slots = 10")

    # Cleanup
    publisher.safe_sql("DROP PUBLICATION regress_pub1")
    old_sub.start()
    old_sub.safe_sql("DROP SUBSCRIPTION regress_sub1;")

    # ------------------------------------------------------
    # Check that pg_upgrade refuses to run if:
    # a) there's a subscription with tables in a state other than 'r' (ready) or
    #    'i' (init) and/or
    # b) the subscription has no replication origin.
    # ------------------------------------------------------
    publisher.safe_sql(
        """
            CREATE TABLE tab_primary_key(id serial PRIMARY KEY);
            INSERT INTO tab_primary_key values(1);
            CREATE PUBLICATION regress_pub2 FOR TABLE tab_primary_key;
    """
    )

    # Insert the same value that is already present in publisher to the primary
    # key column of subscriber so that the table sync will fail.
    old_sub.safe_sql(
        """
            CREATE TABLE tab_primary_key(id serial PRIMARY KEY);
            INSERT INTO tab_primary_key values(1);
    """
    )
    old_sub.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub2 CONNECTION '{connstr}' "
        "PUBLICATION regress_pub2"
    )

    # Table will be in 'd' (data is being copied) state as table sync will fail
    # because of primary key constraint error.
    started_query = (
        "SELECT count(1) = 1 FROM pg_subscription_rel WHERE srsubstate = 'd'"
    )
    assert old_sub.poll_query_until(started_query), \
        "Timed out while waiting for the table state to become 'd' (datasync)"

    # Setup another logical replication and drop the subscription's replication
    # origin.
    publisher.safe_sql("CREATE PUBLICATION regress_pub3")
    old_sub.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub3 CONNECTION '{connstr}' "
        "PUBLICATION regress_pub3 WITH (enabled = false)"
    )
    sub_oid = old_sub.safe_sql(
        "SELECT oid FROM pg_subscription WHERE subname = 'regress_sub3'"
    )
    replorigin = "pg_" + sub_oid
    old_sub.safe_sql(f"SELECT pg_replication_origin_drop('{replorigin}')")

    old_sub.stop()

    new_sub.command_checks_all(
        [
            "pg_upgrade",
            "--no-sync",
            "--old-datadir", old_sub.data_dir,
            "--new-datadir", new_sub.data_dir,
            "--old-bindir", oldbindir,
            "--new-bindir", newbindir,
            "--socketdir", new_sub.host,
            "--old-port", old_sub.port,
            "--new-port", new_sub.port,
            mode,
            "--check",
        ],
        1,
        [
            re.escape(
                "Your installation contains subscriptions without origin or "
                "having relations not in i (initialize) or r (ready) state"
            )
        ],
        [],
        "run of pg_upgrade --check for old instance with relation in 'd' "
        "datasync(invalid) state and missing replication origin",
    )

    # Verify the reason why the subscriber cannot be upgraded.
    #
    # Find a txt file that contains a list of tables that cannot be upgraded. We
    # cannot predict the file's path because the output directory contains a
    # milliseconds timestamp.
    sub_relstate_filename = _find_file(
        new_sub.data_dir + "/pg_upgrade_output.d", r"subs_invalid\.txt"
    )

    with open(sub_relstate_filename, "r", encoding="utf-8", errors="replace") as fh:
        sub_relstate_content = fh.read()

    # Check the file content which should have tab_primary_key table in an
    # invalid state.
    assert re.search(
        r'The table sync state "d" is not allowed for database:"postgres" '
        r'subscription:"regress_sub2" schema:"public" relation:"tab_primary_key"',
        sub_relstate_content,
        re.MULTILINE,
    ), "the previous test failed due to subscription table in invalid state"

    # Check the file content which should have regress_sub3 subscription.
    assert re.search(
        r'The replication origin is missing for database:"postgres" '
        r'subscription:"regress_sub3"',
        sub_relstate_content,
        re.MULTILINE,
    ), "the previous test failed due to missing replication origin"

    # Cleanup
    old_sub.start()
    publisher.safe_sql(
        """
            DROP PUBLICATION regress_pub2;
            DROP PUBLICATION regress_pub3;
            DROP TABLE tab_primary_key;
    """
    )
    old_sub.safe_sql("DROP SUBSCRIPTION regress_sub2")
    old_sub.safe_sql("DROP SUBSCRIPTION regress_sub3")
    old_sub.safe_sql("DROP TABLE tab_primary_key")
    shutil.rmtree(new_sub.data_dir + "/pg_upgrade_output.d", ignore_errors=True)

    # Verify that the upgrade should be successful with tables in 'ready'/'init'
    # state along with retaining the replication origin's remote lsn,
    # subscription's running status, failover option, and retain_dead_tuples
    # option. Use multiple tables to verify deterministic pg_dump ordering
    # of subscription relations during --binary-upgrade.
    publisher.safe_sql(
        """
            CREATE TABLE tab_upgraded(id int);
            CREATE TABLE tab_upgraded1(id int);
            CREATE PUBLICATION regress_pub4 FOR TABLE tab_upgraded, tab_upgraded1;
    """
    )

    old_sub.safe_sql(
        """
            CREATE TABLE tab_upgraded(id int);
            CREATE TABLE tab_upgraded1(id int);
    """
    )
    old_sub.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub4 CONNECTION '{connstr}' "
        "PUBLICATION regress_pub4 WITH (failover = true, retain_dead_tuples = true)"
    )

    # Wait till the tables tab_upgraded and tab_upgraded1 reach 'ready' state
    synced_query = (
        "SELECT count(1) = 2 FROM pg_subscription_rel WHERE srsubstate = 'r'"
    )
    assert old_sub.poll_query_until(synced_query), \
        "Timed out while waiting for the table to reach ready state"

    publisher.safe_sql("INSERT INTO tab_upgraded1 VALUES (generate_series(1,50))")
    publisher.wait_for_catchup("regress_sub4")

    # Change configuration to prepare a subscription table in init state
    old_sub.append_conf("max_logical_replication_workers = 0")
    old_sub.restart()

    # Setup another logical replication
    publisher.safe_sql(
        """
            CREATE TABLE tab_upgraded2(id int);
            CREATE PUBLICATION regress_pub5 FOR TABLE tab_upgraded2;
    """
    )
    old_sub.safe_sql("CREATE TABLE tab_upgraded2(id int)")
    old_sub.safe_sql(
        f"CREATE SUBSCRIPTION regress_sub5 CONNECTION '{connstr}' "
        "PUBLICATION regress_pub5"
    )

    # The table tab_upgraded2 will be in the init state as the subscriber's
    # configuration for max_logical_replication_workers is set to 0.
    result = old_sub.safe_sql(
        "SELECT count(1) = 1 FROM pg_subscription_rel WHERE srsubstate = 'i'"
    )
    assert result == "t", "Check that the table is in init state"

    # Get the replication origin's remote_lsn of the old subscriber
    remote_lsn = old_sub.safe_sql(
        "SELECT remote_lsn FROM pg_replication_origin_status os, pg_subscription s "
        "WHERE os.external_id = 'pg_' || s.oid AND s.subname = 'regress_sub4'"
    )
    # Have the subscription in disabled state before upgrade
    old_sub.safe_sql("ALTER SUBSCRIPTION regress_sub5 DISABLE")

    tab_upgraded_oid = old_sub.safe_sql(
        "SELECT oid FROM pg_class WHERE relname = 'tab_upgraded'"
    )
    tab_upgraded1_oid = old_sub.safe_sql(
        "SELECT oid FROM pg_class WHERE relname = 'tab_upgraded1'"
    )
    tab_upgraded2_oid = old_sub.safe_sql(
        "SELECT oid FROM pg_class WHERE relname = 'tab_upgraded2'"
    )

    old_sub.stop()

    # Change configuration so that initial table sync does not get started
    # automatically
    new_sub.append_conf("max_logical_replication_workers = 0")

    # ------------------------------------------------------
    # Check that pg_upgrade is successful when all tables are in ready or in
    # init state (tab_upgraded and tab_upgraded1 tables are in ready state and
    # tab_upgraded2 table is in init state) along with retaining the replication
    # origin's remote lsn, subscription's running status, failover option, and
    # retain_dead_tuples option.
    # ------------------------------------------------------
    new_sub.command_ok(
        [
            "pg_upgrade",
            "--no-sync",
            "--old-datadir", old_sub.data_dir,
            "--new-datadir", new_sub.data_dir,
            "--old-bindir", oldbindir,
            "--new-bindir", newbindir,
            "--socketdir", new_sub.host,
            "--old-port", old_sub.port,
            "--new-port", new_sub.port,
            mode,
        ],
        "run of pg_upgrade for old instance when the subscription tables are "
        "in init/ready state",
    )
    assert not os.path.isdir(new_sub.data_dir + "/pg_upgrade_output.d"), \
        "pg_upgrade_output.d/ removed after successful pg_upgrade"

    # ------------------------------------------------------
    # Check that the data inserted to the publisher when the new subscriber is
    # down will be replicated once it is started. Also check that the old
    # subscription states and relations origins are all preserved, and that the
    # conflict detection slot is created.
    # ------------------------------------------------------
    publisher.safe_sql(
        """
            INSERT INTO tab_upgraded1 VALUES(51);
            INSERT INTO tab_upgraded2 VALUES(1);
    """
    )

    new_sub.start()

    # The subscription's running status, failover option, and
    # retain_dead_tuples option should be preserved in the upgraded instance.
    # So regress_sub4 should still have subenabled, subfailover, and
    # subretaindeadtuples set to true, while regress_sub5 should have both set
    # to false.
    result = new_sub.safe_sql(
        "SELECT subname, subenabled, subfailover, subretaindeadtuples "
        "FROM pg_subscription ORDER BY subname"
    )
    assert result == "regress_sub4|t|t|t\nregress_sub5|f|f|f", \
        "check that the subscription's running status, failover, and " \
        "retain_dead_tuples are preserved"

    # Subscription relations should be preserved
    result = new_sub.safe_sql(
        "SELECT srrelid, srsubstate FROM pg_subscription_rel ORDER BY srrelid"
    )
    assert result == (
        f"{tab_upgraded_oid}|r\n{tab_upgraded1_oid}|r\n{tab_upgraded2_oid}|i"
    ), (
        "there should be 3 rows in pg_subscription_rel(representing "
        "tab_upgraded, tab_upgraded1 and tab_upgraded2)"
    )

    # The replication origin's remote_lsn should be preserved
    sub_oid = new_sub.safe_sql(
        "SELECT oid FROM pg_subscription WHERE subname = 'regress_sub4'"
    )
    result = new_sub.safe_sql(
        "SELECT remote_lsn FROM pg_replication_origin_status "
        f"WHERE external_id = 'pg_' || {sub_oid}"
    )
    assert result == remote_lsn, "remote_lsn should have been preserved"

    # The conflict detection slot should be created
    result = new_sub.safe_sql(
        "SELECT xmin IS NOT NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    )
    assert result == "t", "conflict detection slot exists"

    # Resume the initial sync and wait until all tables of subscription
    # 'regress_sub5' are synchronized
    new_sub.append_conf("max_logical_replication_workers = 10")
    new_sub.restart()
    new_sub.safe_sql("ALTER SUBSCRIPTION regress_sub5 ENABLE")
    new_sub.wait_for_subscription_sync(publisher, "regress_sub5")

    # wait for regress_sub4 to catchup as well
    publisher.wait_for_catchup("regress_sub4")

    # Rows on tab_upgraded1 and tab_upgraded2 should have been replicated
    result = new_sub.safe_sql("SELECT count(*) FROM tab_upgraded1")
    assert result == "51", "check replicated inserts on new subscriber"
    result = new_sub.safe_sql("SELECT count(*) FROM tab_upgraded2")
    assert result == "1", \
        "check the data is synced after enabling the subscription for the " \
        "table that was in init state"
