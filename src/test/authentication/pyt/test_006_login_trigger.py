# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests of authentication via login event trigger.

Mostly for rejection via exception, because this scenario cannot be covered
with *.sql/*.out regress tests.

Setup statements that run before the trigger fires use the cached session via
safe_sql, while every post-enable check uses connect_ok, which opens a fresh
libpq connection (firing the login trigger each time) and captures the
connection-time NOTICE on stderr.

These tests require Unix-domain sockets; this framework is always
Unix-socket-only, so no skip is needed.
"""


def test_006_login_trigger(create_pg):
    node = create_pg("main", start=False)
    node.append_conf(
        "wal_level = 'logical'\n"
        "max_replication_slots = 4\n"
        "max_wal_senders = 4\n"
    )
    node.start()

    # Create temporary roles and log table (trigger not yet present, so these
    # run via the cached session and fire nothing).
    node.safe_sql(
        "CREATE ROLE regress_alice WITH LOGIN;"
        "CREATE ROLE regress_mallory WITH LOGIN;"
        "CREATE TABLE user_logins(id serial, who text);"
        "GRANT SELECT ON user_logins TO public;"
    )

    # Create login event function and trigger.
    node.safe_sql(
        """CREATE FUNCTION on_login_proc() RETURNS event_trigger AS $$
BEGIN
  INSERT INTO user_logins (who) VALUES (SESSION_USER);
  IF SESSION_USER = 'regress_mallory' THEN
    RAISE EXCEPTION 'Hello %! You are NOT welcome here!', SESSION_USER;
  END IF;
  RAISE NOTICE 'Hello %! You are welcome!', SESSION_USER;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;"""
    )

    # CREATE EVENT TRIGGER: the cached session's connection logged in before
    # the trigger existed, so nothing fires here.
    node.safe_sql(
        "CREATE EVENT TRIGGER on_login_trigger "
        "ON login EXECUTE PROCEDURE on_login_proc();"
    )

    # From here on every command opens a fresh connection that fires the login
    # trigger (inserting one row and raising "You are welcome").

    # ALTER EVENT TRIGGER ... ENABLE ALWAYS -- insert #1 (postgres).
    node.connect_ok(
        "",
        "alter event trigger",
        sql="ALTER EVENT TRIGGER on_login_trigger ENABLE ALWAYS;",
        expected_stderr=r"You are welcome",
    )

    # Check the two requests were logged via login trigger -- insert #2.
    node.connect_ok(
        "",
        "select count",
        sql="SELECT COUNT(*) FROM user_logins;",
        expected_stdout=r"^2$",
        expected_stderr=r"You are welcome",
    )

    # Try to login as allowed Alice.  We don't check the Mallory login, because
    # a FATAL error could cause a timing-dependent panic.  Insert #3 (alice).
    node.connect_ok(
        "user=regress_alice",
        "try regress_alice",
        sql="SELECT 1;",
        expected_stdout=r"^1$",
        expected_stderr=r"You are welcome",
    )

    # We also need Alice's stderr to NOT match "You are NOT welcome";
    # connect_ok already required stderr to be exactly the welcome notice (no
    # extra stderr is permitted unless it matches expected_stderr), so the
    # rejection path is excluded.  The NOT-welcome branch is only reachable for
    # mallory, who is intentionally never connected (a FATAL there could cause a
    # timing-dependent panic).

    # Check that Alice's login record is here -- insert #4 (postgres).
    node.connect_ok(
        "",
        "select *",
        sql="SELECT * FROM user_logins ORDER BY id;",
        expected_stdout=r"3\|regress_alice",
        expected_stderr=r"You are welcome",
    )
    # And that mallory never appears.
    rows = node.safe_sql("SELECT * FROM user_logins ORDER BY id;")
    assert "regress_mallory" not in rows, "mallory never logged in"

    # Check total number of successful logins so far -- insert #5.
    node.connect_ok(
        "",
        "select count",
        sql="SELECT COUNT(*) FROM user_logins;",
        expected_stdout=r"^5$",
        expected_stderr=r"You are welcome",
    )

    # Cleanup the temporary stuff -- DROP fires the trigger one last time.
    node.connect_ok(
        "",
        "drop event trigger",
        sql="DROP EVENT TRIGGER on_login_trigger;",
        expected_stderr=r"You are welcome",
    )

    # With the trigger gone, the cached session (or a fresh one) fires nothing.
    node.safe_sql(
        "DROP TABLE user_logins;"
        "DROP FUNCTION on_login_proc;"
        "DROP ROLE regress_mallory;"
        "DROP ROLE regress_alice;"
    )
