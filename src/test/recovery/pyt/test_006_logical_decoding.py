# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Testing of logical decoding using SQL interface and/or pg_recvlogical."""

# Most logical decoding tests are in contrib/test_decoding. This module
# is for work that doesn't fit well there, like where server restarts
# are required.

import re
import subprocess
import sys

from libpq import Session
from pypg.util import TIMEOUT_DEFAULT


def test_006_logical_decoding(create_pg):
    # Initialize primary node
    node_primary = create_pg("primary", allows_streaming=True, start=False)
    node_primary.append_conf("wal_level = logical")
    node_primary.start()

    node_primary.safe_sql("CREATE TABLE decoding_test(x integer, y text);")

    node_primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('test_slot', 'test_decoding');"
    )

    libdir = node_primary.libdir
    connstr_db = node_primary.connstr("template1") + " replication=database"
    connstr_phys = node_primary.connstr("template1") + " replication=true"

    # Cover walsender error shutdown code
    with Session(connstr=connstr_db, libdir=libdir) as sess:
        res = sess.query("START_REPLICATION SLOT test_slot LOGICAL 0/0")
        assert res.error_message is not None and re.search(
            r'replication slot "test_slot" was not created in this database',
            res.error_message,
        ), "Logical decoding correctly fails to start"

    with Session(connstr=connstr_db, libdir=libdir) as sess:
        res = sess.query("READ_REPLICATION_SLOT test_slot;")
        assert res.error_message is not None and re.search(
            r"cannot use READ_REPLICATION_SLOT with a logical replication slot",
            res.error_message,
        ), "READ_REPLICATION_SLOT not supported for logical slots"

    # Check case of walsender not using a database connection.  Logical
    # decoding should not be allowed.
    with Session(connstr=connstr_phys, libdir=libdir) as sess:
        res = sess.query("START_REPLICATION SLOT s1 LOGICAL 0/1")
        assert res.error_message is not None and re.search(
            r"ERROR:  logical decoding requires a database connection",
            res.error_message,
        ), "Logical decoding fails on non-database connection"

    node_primary.safe_sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text "
        "FROM generate_series(1,10) s;"
    )

    # Basic decoding works
    result = node_primary.safe_sql(
        "SELECT pg_logical_slot_get_changes('test_slot', NULL, NULL);"
    )
    assert len(result.splitlines()) == 12, "Decoding produced 12 rows inc BEGIN/COMMIT"

    # If we immediately crash the server we might lose the progress we just made
    # and replay the same changes again. But a clean shutdown should never repeat
    # the same changes when we use the SQL decoding interface.
    node_primary.restart()

    # There are no new writes, so the result should be empty.
    result = node_primary.safe_sql(
        "SELECT pg_logical_slot_get_changes('test_slot', NULL, NULL);"
    )
    assert result == "", "Decoding after fast restart repeats no rows"

    # Insert some rows and verify that we get the same results from pg_recvlogical
    # and the SQL interface.
    node_primary.safe_sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text "
        "FROM generate_series(1,4) s;"
    )

    expected = (
        "BEGIN\n"
        "table public.decoding_test: INSERT: x[integer]:1 y[text]:'1'\n"
        "table public.decoding_test: INSERT: x[integer]:2 y[text]:'2'\n"
        "table public.decoding_test: INSERT: x[integer]:3 y[text]:'3'\n"
        "table public.decoding_test: INSERT: x[integer]:4 y[text]:'4'\n"
        "COMMIT"
    )

    stdout_sql = node_primary.safe_sql(
        "SELECT data FROM pg_logical_slot_peek_changes('test_slot', NULL, NULL, "
        "'include-xids', '0', 'skip-empty-xacts', '1');"
    )
    assert stdout_sql == expected, "got expected output from SQL decoding session"

    endpos = node_primary.safe_sql(
        "SELECT lsn FROM pg_logical_slot_peek_changes('test_slot', NULL, NULL) "
        "ORDER BY lsn DESC LIMIT 1;"
    )
    print(f"waiting to replay {endpos}")

    # Insert some rows after $endpos, which we won't read.
    node_primary.safe_sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text "
        "FROM generate_series(5,50) s;"
    )

    stdout_recv = node_primary.pg_recvlogical_upto(
        "postgres",
        "test_slot",
        endpos,
        TIMEOUT_DEFAULT,
        **{"include-xids": "0", "skip-empty-xacts": "1"},
    ).stdout
    stdout_recv = stdout_recv.rstrip("\n")
    assert (
        stdout_recv == expected
    ), "got same expected output from pg_recvlogical decoding session"

    assert node_primary.poll_query_until(
        "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
        "WHERE slot_name = 'test_slot' AND active_pid IS NULL)"
    ), "slot never became inactive"

    stdout_recv = node_primary.pg_recvlogical_upto(
        "postgres",
        "test_slot",
        endpos,
        TIMEOUT_DEFAULT,
        **{"include-xids": "0", "skip-empty-xacts": "1"},
    ).stdout
    stdout_recv = stdout_recv.rstrip("\n")
    assert stdout_recv == "", "pg_recvlogical acknowledged changes"

    node_primary.safe_sql("CREATE DATABASE otherdb")

    assert (
        node_primary.sql(
            "SELECT lsn FROM pg_logical_slot_peek_changes('test_slot', NULL, NULL) "
            "ORDER BY lsn DESC LIMIT 1;",
            "otherdb",
        ).error_message
        is not None
    ), "replaying logical slot from another database fails"

    node_primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('otherdb_slot', 'test_decoding');",
        "otherdb",
    )

    # make sure you can't drop a slot while active
    if sys.platform == "win32":
        # some Windows Perls at least don't like IPC::Run's start/kill_kill regime.
        pass
    else:
        # Terminated explicitly by kill()/wait() in the finally block.
        pg_recvlogical = subprocess.Popen(  # pylint: disable=consider-using-with
            [
                node_primary.resolve("pg_recvlogical"),
                "--dbname",
                node_primary.connstr("otherdb"),
                "--slot",
                "otherdb_slot",
                "--file",
                "-",
                "--start",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert node_primary.poll_query_until(
                "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
                "WHERE slot_name = 'otherdb_slot' AND active_pid IS NOT NULL)",
                dbname="otherdb",
            ), "slot never became active"
            assert (
                node_primary.sql("DROP DATABASE otherdb").error_message is not None
            ), "dropping a DB with active logical slots fails"
        finally:
            pg_recvlogical.kill()
            pg_recvlogical.wait()
        assert (
            node_primary.slot("otherdb_slot")["plugin"] == "test_decoding"
        ), "logical slot still exists"

    assert node_primary.poll_query_until(
        "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
        "WHERE slot_name = 'otherdb_slot' AND active_pid IS NULL)",
        dbname="otherdb",
    ), "slot never became inactive"

    # Wait out the killed walsender backend so DROP DATABASE is not blocked by
    # lingering connections to otherdb.
    assert node_primary.poll_query_until(
        "SELECT NOT EXISTS (SELECT 1 FROM pg_stat_activity "
        "WHERE datname = 'otherdb')"
    ), "connections to otherdb never went away"

    assert (
        node_primary.sql("DROP DATABASE otherdb").error_message is None
    ), "dropping a DB with inactive logical slots succeeds"
    assert (
        node_primary.slot("otherdb_slot")["plugin"] == ""
    ), "logical slot was actually dropped with DB"

    # Test logical slot advancing and its durability.
    # Passing failover=true (last arg) should not have any impact on advancing.
    logical_slot = "logical_slot"
    node_primary.safe_sql(
        f"SELECT pg_create_logical_replication_slot('{logical_slot}', "
        "'test_decoding', false, false, true);"
    )
    node_primary.safe_sql("CREATE TABLE tab_logical_slot (a int);")
    node_primary.safe_sql(
        "INSERT INTO tab_logical_slot VALUES (generate_series(1,10));"
    )
    current_lsn = node_primary.safe_sql("SELECT pg_current_wal_lsn();")
    assert (
        node_primary.sql(
            f"SELECT pg_replication_slot_advance('{logical_slot}', "
            f"'{current_lsn}'::pg_lsn);"
        ).error_message
        is None
    ), "slot advancing with logical slot"
    logical_restart_lsn_pre = node_primary.safe_sql(
        "SELECT restart_lsn from pg_replication_slots "
        f"WHERE slot_name = '{logical_slot}';"
    )
    # Slot advance should persist across clean restarts.
    node_primary.restart()
    logical_restart_lsn_post = node_primary.safe_sql(
        "SELECT restart_lsn from pg_replication_slots "
        f"WHERE slot_name = '{logical_slot}';"
    )
    assert (
        logical_restart_lsn_pre == logical_restart_lsn_post
    ), "logical slot advance persists across restarts"

    stats_test_slot1 = "test_slot"
    stats_test_slot2 = "logical_slot"

    # Test that reset works for pg_stat_replication_slots

    # Stats exist for stats test slot 1
    assert (
        node_primary.safe_sql(
            "SELECT total_bytes > 0, stats_reset IS NULL "
            "FROM pg_stat_replication_slots "
            f"WHERE slot_name = '{stats_test_slot1}'"
        )
        == "t|t"
    ), f"Total bytes is > 0 and stats_reset is NULL for slot '{stats_test_slot1}'."

    # Do reset of stats for stats test slot 1
    node_primary.safe_sql(
        f"SELECT pg_stat_reset_replication_slot('{stats_test_slot1}')"
    )

    # Get reset value after reset
    reset1 = node_primary.safe_sql(
        "SELECT stats_reset FROM pg_stat_replication_slots "
        f"WHERE slot_name = '{stats_test_slot1}'"
    )

    # Do reset again
    node_primary.safe_sql(
        f"SELECT pg_stat_reset_replication_slot('{stats_test_slot1}')"
    )

    assert (
        node_primary.safe_sql(
            f"SELECT stats_reset > '{reset1}'::timestamptz, total_bytes = 0 "
            "FROM pg_stat_replication_slots "
            f"WHERE slot_name = '{stats_test_slot1}'"
        )
        == "t|t"
    ), (
        "Check that reset timestamp is later after the second reset of stats "
        f"for slot '{stats_test_slot1}' and confirm total_bytes was set to 0."
    )

    # Check that test slot 2 has NULL in reset timestamp
    assert (
        node_primary.safe_sql(
            "SELECT stats_reset IS NULL FROM pg_stat_replication_slots "
            f"WHERE slot_name = '{stats_test_slot2}'"
        )
        == "t"
    ), f"Stats_reset is NULL for slot '{stats_test_slot2}' before reset."

    # Get reset value again for test slot 1
    reset1 = node_primary.safe_sql(
        "SELECT stats_reset FROM pg_stat_replication_slots "
        f"WHERE slot_name = '{stats_test_slot1}'"
    )

    # Reset stats for all replication slots
    node_primary.safe_sql("SELECT pg_stat_reset_replication_slot(NULL)")

    # Check that test slot 2 reset timestamp is no longer NULL after reset
    assert (
        node_primary.safe_sql(
            "SELECT stats_reset IS NOT NULL FROM pg_stat_replication_slots "
            f"WHERE slot_name = '{stats_test_slot2}'"
        )
        == "t"
    ), f"Stats_reset is not NULL for slot '{stats_test_slot2}' after reset all."

    assert (
        node_primary.safe_sql(
            f"SELECT stats_reset > '{reset1}'::timestamptz "
            "FROM pg_stat_replication_slots "
            f"WHERE slot_name = '{stats_test_slot1}'"
        )
        == "t"
    ), (
        "Check that reset timestamp is later after resetting stats "
        f"for slot '{stats_test_slot1}' again."
    )

    # done with the node
    node_primary.stop()
