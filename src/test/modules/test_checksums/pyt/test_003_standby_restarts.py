# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test enabling data checksums in an online cluster with streaming replication.

Toggles data checksums on the primary and verifies the state propagates to the
standby across restarts and promotions.
"""

import os
import re


def _build_conninfo(primary, standby):
    """Build an unquoted primary_conninfo string pointing at *primary*.

    The standby's application_name is set to its node name so wait_for_catchup
    can locate it in pg_stat_replication.
    """
    return (
        f"host={primary.host} port={primary.port} "
        f"dbname=postgres application_name={standby.name}"
    )


def test_003_standby_restarts(create_pg, checksums):
    # Initialize primary node
    node_primary = create_pg(
        "primary",
        start=False,
        allows_streaming=True,
        initdb_extra=["--no-data-checksums"],
    )
    node_primary.start()

    slotname = "physical_slot"
    node_primary.safe_sql(
        f"SELECT pg_create_physical_replication_slot('{slotname}')"
    )

    # Take backup
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Create streaming standby linking to primary
    node_standby = create_pg("standby", start=False)
    node_standby.init_from_backup(node_primary, backup_name)
    node_standby.append_conf(
        f"\nprimary_conninfo='{_build_conninfo(node_primary, node_standby)}'\n"
    )
    node_standby.append_conf(f"\nprimary_slot_name = '{slotname}'\n")
    node_standby.set_standby_mode()
    node_standby.start()

    # Create some content on the primary to have un-checksummed data
    node_primary.safe_sql(
        "CREATE TABLE t AS SELECT generate_series(1,10000) AS a;"
    )

    # Wait for standby to catch up
    node_primary.wait_for_catchup(node_standby, "replay", node_primary.lsn("insert"))

    # Check that checksums are turned off on all nodes
    checksums.test_checksum_state(node_primary, "off")
    checksums.test_checksum_state(node_standby, "off")

    # -----------------------------------------------------------------------
    # Enable checksums for the cluster, and make sure that both the primary
    # and standby change state.

    # Initiate enabling of checksums and ensure that the primary switches to
    # either "inprogress-on" or "on"
    checksums.enable_data_checksums(node_primary)
    assert node_primary.poll_query_until(
        "SELECT setting = 'off' FROM pg_catalog.pg_settings "
        "WHERE name = 'data_checksums';",
        "f",
    ), "ensure primary has transitioned from off"
    # Wait for checksum enable to be replayed
    node_primary.wait_for_catchup(node_standby, "replay")

    # Ensure that the standby has switched to "inprogress-on" or "on".
    # Normally it would be "inprogress-on", but it is theoretically possible
    # for the primary to complete the checksum enabling *and* have the standby
    # replay that record before we reach the check below.
    assert node_standby.poll_query_until(
        "SELECT setting = 'off' FROM pg_catalog.pg_settings "
        "WHERE name = 'data_checksums';",
        "f",
    ), "ensure standby has absorbed the inprogress-on barrier"
    result = node_standby.safe_sql(
        "SELECT setting FROM pg_catalog.pg_settings "
        "WHERE name = 'data_checksums';"
    )
    assert result in ("inprogress-on", "on"), (
        "ensure checksums are on, or in progress, on standby_1"
    )

    # Insert some more data which should be checksummed on INSERT
    node_primary.safe_sql("INSERT INTO t VALUES (generate_series(1, 10000));")

    # Wait for checksums enabled on the primary and standby
    checksums.wait_for_checksum_state(node_primary, "on")
    checksums.wait_for_checksum_state(node_standby, "on")

    result = node_primary.safe_sql("SELECT count(a) FROM t WHERE a > 1")
    assert result == "19998", "ensure we can safely read all data with checksums"

    assert node_primary.poll_query_until(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE backend_type LIKE 'datachecksums%';",
        "0",
    ), "await datachecksums worker/launcher termination"

    # -----------------------------------------------------------------------
    # Disable checksums and ensure it's propagated to standby and that we can
    # still read all data

    # Disable checksums and wait for the operation to be replayed
    checksums.disable_data_checksums(node_primary)
    node_primary.wait_for_catchup(node_standby, "replay")
    # Ensure that the primary and standby has switched to off
    checksums.wait_for_checksum_state(node_primary, "off")
    checksums.wait_for_checksum_state(node_standby, "off")
    # Double-check reading data without errors
    result = node_primary.safe_sql("SELECT count(a) FROM t WHERE a > 1")
    assert result == "19998", "ensure we can safely read all data without checksums"

    # -----------------------------------------------------------------------
    # Test that enabling checksums does not emit WAL for unlogged relations.
    # Unlogged relations are wiped on recovery, so FPIs for them would be
    # pointless and waste WAL traffic / standby I/O.
    #
    # Additionally, exercise standby promotion to ensure the init fork of an
    # unlogged relation is still WAL-logged during checksum enable -- otherwise
    # the standby keeps a stale init fork and the post-promotion main fork
    # fails verification on every read (see ResetUnloggedRelations()).  Both
    # tables must exist BEFORE enable_data_checksums() so that their init forks
    # get re-checksummed during the enable sweep.

    node_primary.safe_sql(
        "CREATE UNLOGGED TABLE unlogged_tbl AS "
        "SELECT generate_series(1,1000) AS a;"
    )
    # Use a btree index so the init fork is non-trivial (one metapage).
    node_primary.safe_sql(
        """
        CREATE UNLOGGED TABLE unlogged_promo (id int PRIMARY KEY,
                                              payload text);
        INSERT INTO unlogged_promo
          SELECT g, repeat('x', 100) FROM generate_series(1, 1000) g;
        CREATE INDEX unlogged_promo_payload_idx ON unlogged_promo (payload);
        """
    )
    node_primary.wait_for_catchup(node_standby, "replay", node_primary.lsn("insert"))

    # Get the relfilenode and database OID so we can inspect the filesystem
    unlogged_rfn = node_primary.safe_sql(
        "SELECT relfilenode FROM pg_class WHERE relname = 'unlogged_tbl';"
    )
    db_oid = node_primary.safe_sql(
        "SELECT oid FROM pg_database WHERE datname = 'postgres';"
    )

    # Verify the standby only has the init fork (no main fork)
    standby_datadir = node_standby.data_dir
    main_fork = os.path.join(standby_datadir, "base", db_oid, unlogged_rfn)
    assert not os.path.isfile(main_fork), (
        "standby has no main fork for unlogged table before enable"
    )

    # Re-enable data checksums
    checksums.enable_data_checksums(node_primary, wait="on")
    checksums.wait_for_checksum_state(node_standby, "on")

    # After standby replays, the unlogged main file must still not exist.
    # If the bug were present, FPI replay would materialize the full table.
    node_primary.wait_for_catchup(node_standby, "replay", node_primary.lsn("insert"))
    assert not os.path.isfile(main_fork), (
        "standby has no main fork for unlogged table after enable"
    )

    # Verify unlogged relation size is 0 on the standby (main fork missing)
    standby_size = node_standby.safe_sql(
        "SELECT pg_relation_size('unlogged_tbl', 'main');"
    )
    assert standby_size == "0", (
        "unlogged table has zero size on standby after checksum enable"
    )

    # Unlogged table should still be readable on primary
    result = node_primary.safe_sql("SELECT count(*) FROM unlogged_tbl;")
    assert result == "1000", (
        "unlogged table readable on primary after checksum enable"
    )

    # Alter persistence to logged, and make sure we can read it on both the
    # primary and standby without any page verification errors in the logfiles.
    node_primary.safe_sql("ALTER TABLE unlogged_tbl SET logged;")
    node_primary.wait_for_catchup(node_standby, "replay", node_primary.lsn("insert"))

    result = node_primary.safe_sql("SELECT sum(a) FROM unlogged_tbl;")
    assert result == "500500", "previously unlogged table can be read on primary"
    result = node_standby.safe_sql("SELECT sum(a) FROM unlogged_tbl;")
    assert result == "500500", "previously unlogged table can be read on standby"

    # -----------------------------------------------------------------------
    # Promote the standby and verify the unlogged_promo relation (created above
    # before the enable sweep) is still usable.  Without the init-fork WAL fix,
    # every read of the index would fail with "page verification failed,
    # calculated checksum X but expected 0".
    node_primary.stop()
    node_standby.promote()

    result = node_standby.safe_sql("SELECT count(*) FROM unlogged_promo;")
    assert result == "0", (
        "unlogged table readable on promoted standby (truncated as expected)"
    )

    node_standby.safe_sql(
        "INSERT INTO unlogged_promo "
        "SELECT g, repeat('y',100) FROM generate_series(1,100) g;"
    )
    result = node_standby.safe_sql(
        "SET enable_seqscan = off; "
        "SELECT id FROM unlogged_promo WHERE id = 50;"
    )
    assert result == "50", (
        "indexed lookup on promoted standby returns expected row"
    )

    node_standby.stop()

    # Perform one final pass over the logs and hunt for unexpected errors
    page_verify_re = re.compile(r"page verification failed,.+\d$", re.MULTILINE)
    log = node_primary.log_content()
    assert not page_verify_re.search(log), (
        "no checksum validation errors in primary log"
    )
    log = node_standby.log_content()
    assert not page_verify_re.search(log), (
        "no checksum validation errors in standby log"
    )

    # -----------------------------------------------------------------------
    # Test that enforced state transitions during promotion (via StartupXLOG)
    # are performed as expected.  When the primary crashes during inprogress-on
    # the standby should revert to off at promotion.  In order to check the
    # transition the test keeps an open session with the standby during
    # promotion.

    # The cluster is currently broken down from the previous test.  Start up
    # the primary as primary, disable checksums and create a new standby from
    # that state.
    node_standby.teardown()
    node_primary.start()
    checksums.disable_data_checksums(node_primary, wait="off")

    # Re-create a new streaming standby linking to primary.  The replication
    # slot name is reused from earlier but a fresh backup is taken.
    backup_name = "my_new_backup"
    node_primary.backup(backup_name)
    node_standby = create_pg("standby2", start=False)
    node_standby.init_from_backup(node_primary, backup_name)
    node_standby.append_conf(
        f"\nprimary_conninfo='{_build_conninfo(node_primary, node_standby)}'\n"
    )
    node_standby.append_conf(f"\nprimary_slot_name = '{slotname}'\n")
    node_standby.set_standby_mode()
    node_standby.start()
    node_primary.wait_for_catchup(node_standby, "replay")

    # Open a background connection on the primary and inject a barrier to block
    # progress to keep the state from advancing past inprogress-on.
    node_primary_bpsql = node_primary.connect("postgres")
    node_primary_bpsql.query_safe("CREATE TEMPORARY TABLE tt (a integer);")
    # Also open a background connection to the standby to make sure we have an
    # active backend during promotion.
    node_standby_bpsql = node_standby.connect("postgres")

    # Start to enable checksums and wait until both primary and standby have
    # moved to the inprogress-on state.  Processing will block here as the
    # temporary rel barrier will block the primary from finishing.
    checksums.enable_data_checksums(node_primary, wait="inprogress-on")
    node_primary.wait_for_catchup(node_standby, "replay")
    checksums.test_checksum_state(node_standby, "inprogress-on")

    # Crash the primary before checksums are enabled and promote the standby.
    # The new primary node will now revert the state to 'off' since checksums
    # weren't fully enabled during the crash.
    node_primary.stop("immediate")
    node_standby.promote()
    checksums.wait_for_checksum_state(node_standby, "off")

    # Ensure that any backend which was active before, and during, promotion
    # sees the new state.
    result = node_standby_bpsql.query_safe("SHOW data_checksums;")
    assert result == "off", (
        "ensure checksums are set to off after promotion during inprogress-on"
    )

    # The primary's session was kept open only to hold the blocking temp table;
    # close it explicitly (its backend is already gone after the crash).
    node_primary_bpsql.close()
    node_standby_bpsql.close()
    node_standby.stop()
