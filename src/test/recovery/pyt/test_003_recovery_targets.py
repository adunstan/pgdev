# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test for recovery targets: name, timestamp, XID."""

import os
import re

from pypg.util import TIMEOUT_DEFAULT, poll_until, slurp_file


def test_003_recovery_targets(create_pg):
    # Create and test a standby from given backup, with a certain recovery
    # target.  Choose until_lsn later than the transaction commit that causes
    # the row count to reach num_rows, yet not later than the recovery target.
    def test_recovery_standby(test_name, node_name, node_primary,
                              recovery_params, num_rows, until_lsn):
        node_standby = create_pg(node_name, start=False)
        node_standby.init_from_backup(node_primary, "my_backup",
                                      has_restoring=True)

        for param_item in recovery_params:
            node_standby.append_conf(param_item)

        node_standby.start()

        # Wait until standby has replayed enough data
        caughtup_query = \
            f"SELECT '{until_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
        assert node_standby.poll_query_until(caughtup_query), \
            "Timed out while waiting for standby to catch up"

        # Create some content on primary and check its presence in standby
        result = node_standby.safe_sql("SELECT count(*) FROM tab_int")
        assert result == str(num_rows), \
            f"check standby content for {test_name}"

        # Stop standby node
        node_standby.stop()

    # Initialize primary node
    node_primary = create_pg("primary", start=False,
                             has_archiving=True, allows_streaming=True)

    # Bump the transaction ID epoch.  This is useful to stress the portability
    # of recovery_target_xid parsing.
    node_primary.pg_bin.command_ok(
        ["pg_resetwal", "--epoch", "1", node_primary.data_dir])

    # Start it
    node_primary.start()

    # Create data before taking the backup, aimed at testing
    # recovery_target = 'immediate'
    node_primary.safe_sql(
        "CREATE TABLE tab_int AS SELECT generate_series(1,1000) AS a")
    lsn1 = node_primary.safe_sql("SELECT pg_current_wal_lsn();")

    # Take backup from which all operations will be run
    node_primary.backup("my_backup")

    # Insert some data with used as a replay reference, with a recovery
    # target TXID.
    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(1001,2000))")
    ret = node_primary.safe_sql(
        "SELECT pg_current_wal_lsn(), pg_current_xact_id();")
    lsn2, recovery_txid = ret.split("|")

    # More data, with recovery target timestamp
    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(2001,3000))")
    lsn3 = node_primary.safe_sql("SELECT pg_current_wal_lsn();")
    recovery_time = node_primary.safe_sql("SELECT now()")

    # Even more data, this time with a recovery target name
    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(3001,4000))")
    recovery_name = "my_target"
    lsn4 = node_primary.safe_sql("SELECT pg_current_wal_lsn();")
    node_primary.safe_sql(
        f"SELECT pg_create_restore_point('{recovery_name}');")

    # And now for a recovery target LSN
    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(4001,5000))")
    lsn5 = recovery_lsn = node_primary.safe_sql(
        "SELECT pg_current_wal_lsn()")

    node_primary.safe_sql(
        "INSERT INTO tab_int VALUES (generate_series(5001,6000))")

    # Force archiving of WAL file
    node_primary.safe_sql("SELECT pg_switch_wal()")

    # Test recovery targets
    recovery_params = ["recovery_target = 'immediate'"]
    test_recovery_standby("immediate target", "standby_1", node_primary,
                          recovery_params, "1000", lsn1)
    recovery_params = [f"recovery_target_xid = '{recovery_txid}'"]
    test_recovery_standby("XID", "standby_2", node_primary, recovery_params,
                          "2000", lsn2)
    recovery_params = [f"recovery_target_time = '{recovery_time}'"]
    test_recovery_standby("time", "standby_3", node_primary, recovery_params,
                          "3000", lsn3)
    recovery_params = [f"recovery_target_name = '{recovery_name}'"]
    test_recovery_standby("name", "standby_4", node_primary, recovery_params,
                          "4000", lsn4)
    recovery_params = [f"recovery_target_lsn = '{recovery_lsn}'"]
    test_recovery_standby("LSN", "standby_5", node_primary, recovery_params,
                          "5000", lsn5)

    # Multiple targets
    #
    # Multiple conflicting settings are not allowed, but setting the same
    # parameter multiple times or unsetting a parameter and setting a
    # different one is allowed.
    recovery_params = [
        f"recovery_target_name = '{recovery_name}'",
        "recovery_target_name = ''",
        f"recovery_target_time = '{recovery_time}'",
    ]
    test_recovery_standby("multiple overriding settings", "standby_6",
                          node_primary, recovery_params, "3000", lsn3)

    node_standby = create_pg("standby_7", start=False)
    node_standby.init_from_backup(node_primary, "my_backup",
                                  has_restoring=True)
    node_standby.append_conf(
        f"recovery_target_name = '{recovery_name}'\n"
        f"recovery_target_time = '{recovery_time}'")

    # Start the server directly with pg_ctl (no -w wait) because this start is
    # expected to fail, so a waited start would never report success.
    res = node_standby.pg_bin.result([
        "pg_ctl",
        "--pgdata", node_standby.data_dir,
        "--log", node_standby.logfile,
        "start",
    ])
    assert res.returncode != 0, "invalid recovery startup fails"

    logfile = slurp_file(node_standby.logfile)
    assert re.search(r"multiple recovery targets specified", logfile), \
        "multiple conflicting settings"

    # Check behavior when recovery ends before target is reached
    node_standby = create_pg("standby_8", start=False)
    node_standby.init_from_backup(node_primary, "my_backup",
                                  has_restoring=True, standby=False)
    node_standby.append_conf("recovery_target_name = 'does_not_exist'")

    node_standby.pg_bin.result([
        "pg_ctl",
        "--pgdata", node_standby.data_dir,
        "--log", node_standby.logfile,
        "start",
    ])

    # wait for postgres to terminate
    pidfile = os.path.join(node_standby.data_dir, "postmaster.pid")
    poll_until(lambda: not os.path.isfile(pidfile), timeout=TIMEOUT_DEFAULT)

    logfile = slurp_file(node_standby.logfile)
    assert re.search(
        r"FATAL: .* recovery ended before configured recovery target "
        r"was reached", logfile), \
        "recovery end before target reached is a fatal error"

    # Invalid recovery_target_timeline tests
    res = node_primary.sql(
        "ALTER SYSTEM SET recovery_target_timeline TO 'bogus'")
    assert re.search(r"is not a valid number", res.error_message or ""), \
        "invalid recovery_target_timeline (bogus value)"

    res = node_primary.sql(
        "ALTER SYSTEM SET recovery_target_timeline TO '0'")
    assert re.search(r"must be between 1 and 4294967295",
                     res.error_message or ""), \
        "invalid recovery_target_timeline (lower bound check)"

    res = node_primary.sql(
        "ALTER SYSTEM SET recovery_target_timeline TO '4294967296'")
    assert re.search(r"must be between 1 and 4294967295",
                     res.error_message or ""), \
        "invalid recovery_target_timeline (upper bound check)"

    # Invalid recovery_target_xid tests
    res = node_primary.sql(
        "ALTER SYSTEM SET recovery_target_xid TO 'bogus'")
    assert re.search(r"is not a valid number", res.error_message or ""), \
        "invalid recovery_target_xid (bogus value)"

    res = node_primary.sql(
        "ALTER SYSTEM SET recovery_target_xid TO '-1'")
    assert re.search(r"is not a valid number", res.error_message or ""), \
        "invalid recovery_target_xid (negative)"

    res = node_primary.sql(
        "ALTER SYSTEM SET recovery_target_xid TO '0'")
    assert re.search(r"without epoch must be greater than or equal to 3",
                     res.error_message or ""), \
        "invalid recovery_target_xid (lower bound check)"
