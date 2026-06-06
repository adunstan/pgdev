# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test pg_createsubscriber, converting a standby server into a subscriber."""

import glob
import os
import re

from libpq import Session


# pg_createsubscriber is the tool under test.  It is invoked through the
# command_* helpers / PgBin (running the binary is allowed); SQL is run
# in-process via libpq Sessions.


def _connstr(node, dbname=None):
    """Build a connection string for *node*.

    Returns ``port=N host=H`` when *dbname* is None, otherwise appends a
    properly-escaped ``dbname='...'`` (only backslashes and single quotes need
    escaping).
    """
    base = f"port={node.port} host={node.host}"
    if dbname is None:
        return base
    escaped = dbname.replace("\\", "\\\\").replace("'", "\\'")
    return f"{base} dbname='{escaped}'"


# The framework's PostgresServer.connstr() / safe_sql() do not escape the
# database name, which breaks for the exotic ASCII database name (db1) used
# below.  Open a fresh, properly-escaped libpq Session per call so no
# connection lingers across stop().
def _safe_sql(node, dbname, query):
    """safe_sql against a database whose name may contain special characters."""
    sess = Session(connstr=_connstr(node, dbname), libdir=node.libdir)
    try:
        return sess.query_safe(query)
    finally:
        sess.close()


def _generate_db(node, prefix, from_char, to_char, suffix):
    """Generate a database with a name made of a range of ASCII characters."""
    dbname = prefix
    for i in range(from_char, to_char + 1):
        if i in (7, 10, 13):  # skip BEL, LF, and CR
            continue
        dbname += chr(i)
    dbname += suffix

    node.command_ok(
        ["createdb", dbname],
        f"created database with ASCII characters from {from_char} to {to_char}",
    )
    return dbname


def _comment_out_conf(node, key):
    """Remove a setting from postgresql.conf by commenting it out."""
    path = os.path.join(node.data_dir, "postgresql.conf")
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    out = [ln for ln in lines if not re.match(rf"\s*{re.escape(key)}\s*=", ln)]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(out)


def test_040_pg_createsubscriber(pg_bin, create_pg, tmp_path):
    datadir = str(tmp_path / "datadir")
    os.makedirs(datadir, exist_ok=True)
    logdir = str(tmp_path / "logdir")
    os.makedirs(logdir, exist_ok=True)

    pg_bin.program_help_ok("pg_createsubscriber")
    pg_bin.program_version_ok("pg_createsubscriber")
    pg_bin.program_options_handling_ok("pg_createsubscriber")

    #
    # Test mandatory options
    pg_bin.command_fails(
        ["pg_createsubscriber"], "no subscriber data directory specified"
    )
    pg_bin.command_fails(
        ["pg_createsubscriber", "--pgdata", datadir],
        "no publisher connection string specified",
    )
    pg_bin.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--pgdata",
            datadir,
            "--publisher-server",
            "port=5432",
        ],
        "no database name specified",
    )
    pg_bin.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--pgdata",
            datadir,
            "--publisher-server",
            "port=5432",
            "--database",
            "pg1",
            "--database",
            "pg1",
        ],
        "duplicate database name",
    )
    pg_bin.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--pgdata",
            datadir,
            "--publisher-server",
            "port=5432",
            "--publication",
            "foo1",
            "--publication",
            "foo1",
            "--database",
            "pg1",
            "--database",
            "pg2",
        ],
        "duplicate publication name",
    )
    pg_bin.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--pgdata",
            datadir,
            "--publisher-server",
            "port=5432",
            "--publication",
            "foo1",
            "--database",
            "pg1",
            "--database",
            "pg2",
        ],
        "wrong number of publication names",
    )
    pg_bin.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--pgdata",
            datadir,
            "--publisher-server",
            "port=5432",
            "--publication",
            "foo1",
            "--publication",
            "foo2",
            "--subscription",
            "bar1",
            "--database",
            "pg1",
            "--database",
            "pg2",
        ],
        "wrong number of subscription names",
    )
    pg_bin.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--pgdata",
            datadir,
            "--publisher-server",
            "port=5432",
            "--publication",
            "foo1",
            "--publication",
            "foo2",
            "--subscription",
            "bar1",
            "--subscription",
            "bar2",
            "--replication-slot",
            "baz1",
            "--database",
            "pg1",
            "--database",
            "pg2",
        ],
        "wrong number of replication slot names",
    )

    # Set up node P as primary
    node_p = create_pg("node_p", start=False, allows_streaming="logical")
    pconnstr = _connstr(node_p)
    # Disable autovacuum to avoid generating xid during stats update as
    # otherwise the new XID could then be replicated to standby at some random
    # point making slots at primary lag behind standby during slot sync.
    node_p.append_conf("autovacuum = off")
    node_p.start()

    # Set up node F as about-to-fail node
    # Force it to initialize a new cluster instead of copying a previously
    # initdb'd cluster.  New cluster has a different system identifier so we can
    # test if the target cluster is a copy of the source cluster.  (create_pg
    # always runs a fresh initdb, so node_f naturally gets a distinct system
    # identifier.)
    node_f = create_pg("node_f", start=False, allows_streaming="logical")

    # On node P
    # - create databases
    # - create test tables
    # - insert a row
    # - create a physical replication slot
    db1 = _generate_db(node_p, 'regression\\"\\', 1, 45, '\\\\"\\\\\\')
    db2 = _generate_db(node_p, "regression", 46, 90, "")

    _safe_sql(node_p, db1, "CREATE TABLE tbl1 (a text)")
    _safe_sql(node_p, db1, "INSERT INTO tbl1 VALUES('first row')")
    _safe_sql(node_p, db2, "CREATE TABLE tbl2 (a text)")
    slotname = "physical_slot"
    _safe_sql(
        node_p,
        db2,
        f"SELECT pg_create_physical_replication_slot('{slotname}')",
    )

    # Set up node S as standby linking to node P
    node_p.backup("backup_1")
    node_s = create_pg("node_s", start=False, allows_streaming="logical")
    node_s.init_from_backup(node_p, "backup_1", has_streaming=True)
    node_s.append_conf(
        f"""
primary_slot_name = '{slotname}'
primary_conninfo = '{pconnstr} dbname=postgres'
hot_standby_feedback = on
"""
    )
    sconnstr = _connstr(node_s)
    node_s.set_standby_mode()
    node_s.start()

    # Set up node T as standby linking to node P then promote it
    node_t = create_pg("node_t", start=False, allows_streaming="logical")
    node_t.init_from_backup(node_p, "backup_1", has_streaming=True)
    node_t.set_standby_mode()
    node_t.start()
    node_t.promote()
    node_t.stop()

    # Run pg_createsubscriber on a promoted server
    node_t.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--pgdata",
            node_t.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_t.host,
            "--subscriber-port",
            node_t.port,
            "--database",
            db1,
            "--database",
            db2,
        ],
        "target server is not in recovery",
    )

    # Run pg_createsubscriber when standby is running
    node_s.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--database",
            db1,
            "--database",
            db2,
        ],
        "standby is up and running",
    )

    # Run pg_createsubscriber on about-to-fail node F
    node_f.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--pgdata",
            node_f.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_f.host,
            "--subscriber-port",
            node_f.port,
            "--database",
            db1,
            "--database",
            db2,
        ],
        "subscriber data directory is not a copy of the source database cluster",
    )

    # Set up node C as standby linking to node S
    node_s.backup("backup_2")
    node_c = create_pg("node_c", start=False, allows_streaming="logical")
    node_c.init_from_backup(node_s, "backup_2", has_streaming=True)
    _comment_out_conf(node_c, "primary_slot_name")
    node_c.set_standby_mode()

    # Run pg_createsubscriber on node C (P -> S -> C)
    node_c.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--pgdata",
            node_c.data_dir,
            "--publisher-server",
            _connstr(node_s, db1),
            "--socketdir",
            node_c.host,
            "--subscriber-port",
            node_c.port,
            "--database",
            db1,
            "--database",
            db2,
        ],
        "primary server is in recovery",
    )

    # Check some unmet conditions on node P
    node_p.append_conf(
        """
max_replication_slots = 1
max_wal_senders = 1
max_worker_processes = 2
"""
    )
    node_p.restart()
    node_s.stop()
    node_s.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--database",
            db1,
            "--database",
            db2,
        ],
        "primary contains unmet conditions on node P",
    )
    # Restore default settings here but only apply it after testing standby.
    # Some standby settings should not be a lower setting than on the primary.
    node_p.append_conf(
        """
max_replication_slots = 10
max_wal_senders = 10
max_worker_processes = 8
"""
    )

    # Check some unmet conditions on node S
    node_s.append_conf(
        """
max_active_replication_origins = 1
max_logical_replication_workers = 1
max_worker_processes = 2
"""
    )
    node_s.command_fails(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--database",
            db1,
            "--database",
            db2,
        ],
        "standby contains unmet conditions on node S",
    )
    node_s.append_conf(
        """
max_active_replication_origins = 10
max_logical_replication_workers = 4
max_worker_processes = 8
"""
    )
    # Restore default settings on both servers
    node_p.restart()

    # Create failover slot to test its removal
    fslotname = "failover_slot"
    _safe_sql(
        node_p,
        db1,
        f"SELECT pg_create_logical_replication_slot('{fslotname}', 'pgoutput', false, false, true)",
    )
    node_s.start()
    # Wait for the standby to catch up so that the standby is not lagging behind
    # the failover slot.
    node_p.wait_for_replay_catchup(node_s)
    node_s.safe_sql("SELECT pg_sync_replication_slots()")
    result = node_s.safe_sql(
        "SELECT slot_name FROM pg_replication_slots "
        f"WHERE slot_name = '{fslotname}' AND synced AND NOT temporary"
    )
    assert result == "failover_slot", "failover slot is synced"

    # Insert another row on node P and wait node S to catch up.  We
    # intentionally performed this insert after syncing logical slot as
    # otherwise the local slot's (created during synchronization of slot) xmin
    # on standby could be ahead of the remote slot leading to failure in
    # synchronization.
    _safe_sql(node_p, db1, "INSERT INTO tbl1 VALUES('second row')")
    node_p.wait_for_replay_catchup(node_s)

    # Create subscription to test its removal
    dummy_sub = "regress_sub_dummy"
    _safe_sql(
        node_p,
        db1,
        f"CREATE SUBSCRIPTION {dummy_sub} CONNECTION 'dbname=dummy' "
        "PUBLICATION pub_dummy WITH (connect=false)",
    )
    node_p.wait_for_replay_catchup(node_s)

    # Create user-defined publications, wait for streaming replication to sync
    # them to the standby, then verify that '--clean' removes them.
    _safe_sql(
        node_p,
        db1,
        "CREATE PUBLICATION test_pub1 FOR ALL TABLES;"
        "CREATE PUBLICATION test_pub2 FOR ALL TABLES;",
    )

    node_p.wait_for_replay_catchup(node_s)

    assert (
        _safe_sql(node_s, db1, "SELECT COUNT(*) FROM pg_publication") == "2"
    ), "two pre-existing publications on subscriber"

    node_s.stop()

    # dry run mode on node S
    node_s.command_ok(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--recovery-timeout",
            "180",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--publication",
            "pub1",
            "--publication",
            "pub2",
            "--subscription",
            "sub1",
            "--subscription",
            "sub2",
            "--database",
            db1,
            "--database",
            db2,
            "--logdir",
            logdir,
        ],
        "run pg_createsubscriber --dry-run on node S",
    )

    # Check that the log files were created
    server_log_files = glob.glob(f"{logdir}/*/pg_createsubscriber_server.log")
    assert len(server_log_files) == 1, "pg_createsubscriber_server.log file was created"
    server_log_file_size = os.path.getsize(server_log_files[0])
    assert server_log_file_size != 0, "pg_createsubscriber_server.log file not empty"
    with open(server_log_files[0], "r", encoding="utf-8", errors="replace") as fh:
        server_log = fh.read()
    assert re.search(
        r"consistent recovery state reached", server_log
    ), "server reached consistent recovery state"

    internal_log_files = glob.glob(f"{logdir}/*/pg_createsubscriber_internal.log")
    assert (
        len(internal_log_files) == 1
    ), "pg_createsubscriber_internal.log file was created"
    internal_log_file_size = os.path.getsize(internal_log_files[0])
    assert (
        internal_log_file_size != 0
    ), "pg_createsubscriber_internal.log file not empty"
    with open(internal_log_files[0], "r", encoding="utf-8", errors="replace") as fh:
        internal_log = fh.read()
    assert re.search(
        r"target server reached the consistent state", internal_log
    ), "log shows consistent state reached"

    # Check if node S is still a standby
    node_s.start()
    assert (
        node_s.safe_sql("SELECT pg_catalog.pg_is_in_recovery()") == "t"
    ), "standby is in recovery"
    node_s.stop()

    # pg_createsubscriber can run without --databases option
    node_s.command_ok(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--replication-slot",
            "replslot1",
        ],
        "run pg_createsubscriber without --databases",
    )

    # run pg_createsubscriber with '--database' and '--all' without '--dry-run'
    # and verify the failure
    node_s.command_fails_like(
        [
            "pg_createsubscriber",
            "--verbose",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--database",
            db1,
            "--all",
        ],
        r"options --database and -a/--all cannot be used together",
        "fail if --database is used with --all",
    )

    # run pg_createsubscriber with '--publication' and '--all' and verify the
    # failure
    node_s.command_fails_like(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--all",
            "--publication",
            "pub1",
        ],
        r"options --publication and -a/--all cannot be used together",
        "fail if --publication is used with --all",
    )

    # run pg_createsubscriber with '--all' option
    res = node_s.pg_bin.command_ok(
        [
            "pg_createsubscriber",
            "--verbose",
            "--dry-run",
            "--recovery-timeout",
            "180",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--all",
        ],
        "run pg_createsubscriber with --all",
    )
    stderr = res.stderr

    # Verify that the required logical replication objects are output.
    # The expected count 3 refers to postgres, db1 and db2 databases.
    assert (
        len(re.findall(r"would create publication", stderr)) == 3
    ), "verify publications are created for all databases"
    assert (
        len(re.findall(r"would create the replication slot", stderr)) == 3
    ), "verify replication slots are created for all databases"
    assert (
        len(re.findall(r"would create subscription", stderr)) == 3
    ), "verify subscriptions are created for all databases"

    # Create a user-defined publication, and a table that is not a member of
    # that publication.
    _safe_sql(
        node_p,
        db1,
        "CREATE PUBLICATION test_pub3 FOR TABLE tbl1;"
        "CREATE TABLE not_replicated (a int);",
    )

    # Run pg_createsubscriber on node S.  --verbose is used twice to show more
    # information.
    #
    # Test two phase and clean options.  Use pre-existing publication.
    node_s.command_ok(
        [
            "pg_createsubscriber",
            "--verbose",
            "--verbose",
            "--recovery-timeout",
            "180",
            "--pgdata",
            node_s.data_dir,
            "--publisher-server",
            _connstr(node_p, db1),
            "--socketdir",
            node_s.host,
            "--subscriber-port",
            node_s.port,
            "--publication",
            "test_pub3",
            "--publication",
            "pub2",
            "--replication-slot",
            "replslot1",
            "--replication-slot",
            "replslot2",
            "--database",
            db1,
            "--database",
            db2,
            "--enable-two-phase",
            "--clean",
            "publications",
        ],
        "run pg_createsubscriber on node S",
    )

    # Check that included file is renamed after success.
    node_s_datadir = node_s.data_dir
    assert os.path.isfile(
        os.path.join(node_s_datadir, "pg_createsubscriber.conf.disabled")
    ), "pg_createsubscriber.conf.disabled exists in node S"

    # Confirm the physical replication slot has been removed
    result = _safe_sql(
        node_p,
        db1,
        f"SELECT count(*) FROM pg_replication_slots WHERE slot_name = '{slotname}'",
    )
    assert (
        result == "0"
    ), "the physical replication slot used as primary_slot_name has been removed"

    # Insert rows on P
    _safe_sql(node_p, db1, "INSERT INTO tbl1 VALUES('third row')")
    _safe_sql(node_p, db2, "INSERT INTO tbl2 VALUES('row 1')")
    _safe_sql(node_p, db1, "INSERT INTO not_replicated VALUES(0)")

    # Start subscriber
    node_s.start()

    # Confirm publications are removed from the subscriber node
    assert (
        _safe_sql(node_s, db1, "SELECT COUNT(*) FROM pg_publication") == "0"
    ), "all publications were removed from db1"
    assert (
        _safe_sql(node_s, db2, "SELECT COUNT(*) FROM pg_publication") == "0"
    ), "all publications were removed from db2"

    # Verify that all subtwophase states are pending or enabled, e.g. there are
    # no subscriptions where subtwophase is disabled ('d')
    assert (
        node_s.safe_sql(
            "SELECT count(1) = 0 FROM pg_subscription WHERE subtwophasestate = 'd'"
        )
        == "t"
    ), "subscriptions are created with the two-phase option enabled"

    # Confirm the pre-existing subscription has been removed
    result = node_s.safe_sql(
        f"SELECT count(*) FROM pg_subscription WHERE subname = '{dummy_sub}'"
    )
    assert result == "0", "pre-existing subscription was dropped"

    # Get subscription names
    result = node_s.safe_sql(
        "SELECT subname FROM pg_subscription WHERE subname ~ '^pg_createsubscriber_'"
    )
    subnames = result.split("\n")

    # Wait subscriber to catch up
    node_s.wait_for_subscription_sync(node_p, subnames[0])
    node_s.wait_for_subscription_sync(node_p, subnames[1])

    # Confirm the failover slot has been removed
    result = _safe_sql(
        node_s,
        db1,
        f"SELECT count(*) FROM pg_replication_slots WHERE slot_name = '{fslotname}'",
    )
    assert result == "0", "failover slot was removed"

    # Check result in database db1
    result = _safe_sql(node_s, db1, "SELECT * FROM tbl1")
    assert (
        result == "first row\nsecond row\nthird row"
    ), "logical replication works in database db1"
    result = _safe_sql(node_s, db1, "SELECT * FROM not_replicated")
    assert result == "", "table is not replicated in database db1"

    # Check result in database db2
    result = _safe_sql(node_s, db2, "SELECT * FROM tbl2")
    assert result == "row 1", "logical replication works in database db2"

    # Different system identifier?
    sysid_p = node_p.safe_sql("SELECT system_identifier FROM pg_control_system()")
    sysid_s = node_s.safe_sql("SELECT system_identifier FROM pg_control_system()")
    assert sysid_p != sysid_s, "system identifier was changed"

    # Verify that pub2 was created in db2
    assert (
        _safe_sql(
            node_p, db2, "SELECT COUNT(*) FROM pg_publication WHERE pubname = 'pub2'"
        )
        == "1"
    ), "publication pub2 was created in db2"

    # Get subscription and publication names
    result = node_s.safe_sql(
        "SELECT subname, subpublications FROM pg_subscription "
        "WHERE subname ~ '^pg_createsubscriber_' "
        "ORDER BY subpublications;"
    )
    # re.VERBOSE (free-spacing) mode is used below, so the literal whitespace
    # and indentation between the two lines is insignificant; only the \n and
    # the escaped tokens match.
    assert re.search(
        r"""^pg_createsubscriber_\d+_[0-9a-f]+ \|\{pub2\}\n
            pg_createsubscriber_\d+_[0-9a-f]+ \|\{test_pub3\}$""",
        result,
        re.VERBOSE,
    ), "subscription and publication names are ok"

    # Verify that the correct publications are being used
    result = node_s.safe_sql(
        "SELECT d.datname, s.subpublications "
        "FROM pg_subscription s "
        "JOIN pg_database d ON d.oid = s.subdbid "
        "WHERE subname ~ '^pg_createsubscriber_' "
        "ORDER BY s.subdbid"
    )
    assert (
        result == f"{db1}|{{test_pub3}}\n{db2}|{{pub2}}"
    ), "subscriptions use the correct publications"

    # Verify that node K, set as a standby, is able to start correctly without
    # the recovery configuration written by pg_createsubscriber interfering.
    # This node is created from node S, where pg_createsubscriber has been run.

    # Create a physical standby from the promoted subscriber
    node_s.safe_sql(f"SELECT pg_create_physical_replication_slot('{slotname}');")

    # Create backup from promoted subscriber
    node_s.backup("backup_3")

    # Initialize new physical standby
    node_k = create_pg("node_k", start=False, allows_streaming="logical")
    node_k.init_from_backup(node_s, "backup_3", has_streaming=True)

    node_k_datadir = node_k.data_dir
    assert os.path.isfile(
        os.path.join(node_k_datadir, "pg_createsubscriber.conf.disabled")
    ), "pg_createsubscriber.conf.disabled exists in node K"

    # Configure the new standby
    node_k.append_conf(
        f"""
primary_slot_name = '{slotname}'
primary_conninfo = '{sconnstr} dbname=postgres'
hot_standby_feedback = on
"""
    )

    node_k.set_standby_mode()
    node_k_name = node_s.name
    node_k.command_ok(
        [
            "pg_ctl",
            "--wait",
            "--pgdata",
            node_k.data_dir,
            "--log",
            node_k.logfile,
            "--options",
            f"--cluster-name={node_k_name}",
            "start",
        ],
        "node K has started",
    )

    # Note that this uses a direct pg_ctl command rather than a teardown(),
    # because node_k.stop() would not work due to the node's postmaster PID not
    # being tracked, something that is set within node_k.start().
    node_k.pg_bin.result(["pg_ctl", "stop", "--pgdata", node_k.data_dir])

    # clean up: explicit teardown of the nodes (also handled by the fixture).
    node_p.teardown()
    node_s.teardown()
    node_t.teardown()
    node_f.teardown()
