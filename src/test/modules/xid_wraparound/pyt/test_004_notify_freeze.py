# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test freezing XIDs in the async notification queue.

This isn't really wraparound-related, but the test depends on the
consume_xids() helper function.
"""

import os

import pytest

if "xid_wraparound" not in os.environ.get("PG_TEST_EXTRA", ""):
    pytest.skip(
        "test xid_wraparound not enabled in PG_TEST_EXTRA",
        allow_module_level=True,
    )


def test_004_notify_freeze(create_pg):
    node = create_pg("node")

    # Setup
    node.safe_sql("CREATE EXTENSION xid_wraparound")
    node.safe_sql("ALTER DATABASE template0 WITH ALLOW_CONNECTIONS true")

    # Start Session 1 and leave it idle in transaction
    session1 = node.connect("postgres")
    session1.do("LISTEN s")
    session1.do("BEGIN")

    # Send some notifys from other sessions
    for i in range(1, 11):
        node.safe_sql(f"NOTIFY s, '{i}'")

    # Consume enough XIDs to trigger truncation, and one more with
    # 'txid_current' to bump up the freeze horizon.
    node.safe_sql("select consume_xids(10000000);")
    node.safe_sql("select txid_current()")

    # Remember current datfrozenxid before vacuum freeze so that we can
    # check that it is advanced. (Taking the min() this way assumes that
    # XID wraparound doesn't happen.)
    datafronzenxid = int(
        node.safe_sql("select min(datfrozenxid::text::bigint) from pg_database")
    )

    # Execute vacuum freeze on all databases
    node.command_ok(
        ["vacuumdb", "--all", "--freeze", "--port", str(node.port)],
        "vacuumdb --all --freeze",
    )

    # Check that vacuumdb advanced datfrozenxid
    datafronzenxid_freeze = int(
        node.safe_sql("select min(datfrozenxid::text::bigint) from pg_database")
    )
    assert datafronzenxid_freeze > datafronzenxid, "datfrozenxid advanced"

    # On Session 1, commit and ensure that all the notifications are
    # received. This depends on correctly freezing the XIDs in the pending
    # notification entries.
    session1.do("COMMIT")

    notifications = session1.get_all_notifications()
    assert len(notifications) == 10, "received all committed notifications"

    expected_payload = 1
    for notify in notifications:
        assert (
            notify["channel"] == "s"
        ), f"notification {expected_payload} has correct channel"
        assert notify["payload"] == str(
            expected_payload
        ), f"notification {expected_payload} has correct payload"
        expected_payload += 1

    session1.close()
