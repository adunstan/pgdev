# Copyright (c) 2026, PostgreSQL Global Development Group
#
# Verify that connecting to a standby still works after a login event
# trigger has been created and dropped on the primary.
#
# CREATE EVENT TRIGGER ... ON login sets pg_database.dathasloginevt to
# true on the primary, but DROP EVENT TRIGGER does not clear it -- the
# next login event trigger pass clears the flag lazily on the primary.
# That dangling flag replicates to the standby.  Before the
# RecoveryInProgress() guard in EventTriggerOnLogin(), the standby
# tried to clear the flag itself, which requires AccessExclusiveLock
# on the database object; that lock mode is forbidden during recovery,
# so the new connection died with FATAL.
#
# To keep the test robust the event trigger is set up in a dedicated
# database (regress_login_evt).  All synchronisation helpers below --
# wait_for_replay_catchup() and friends -- connect to "postgres" on
# the primary; if the trigger were created in "postgres" itself, that
# probe connection would enter the cleanup branch on the primary and
# silently clear the flag before the test even runs, making the
# scenario unreproducible.

from libpq import LibpqError


def test_053_standby_login_event_trigger(create_pg):
    # Set up primary and a streaming standby.
    primary = create_pg("primary", allows_streaming=True)

    backup_name = "login_evt_backup"
    primary.backup(backup_name)

    standby = create_pg("standby", start=False)
    standby.init_from_backup(primary, backup_name, has_streaming=True)
    standby.start()

    # A dedicated database isolates the dangling dathasloginevt flag from
    # any helper that connects to the default "postgres" database.
    primary.safe_sql("CREATE DATABASE regress_login_evt")
    primary.wait_for_replay_catchup(standby)

    # Sanity check: the standby can connect to the new database before
    # the trigger machinery has touched it.
    standby.safe_sql("SELECT 1", dbname="regress_login_evt")

    # Create and drop a login event trigger inside the dedicated database
    # in a single session.  CREATE EVENT TRIGGER sets
    # pg_database.dathasloginevt = true for regress_login_evt; mark it
    # ENABLE ALWAYS so the scenario matches the original bug report.
    # After DROP the flag remains set on disk until a subsequent login on
    # the primary clears it; since later helpers only touch the
    # "postgres" database, regress_login_evt's flag stays set and
    # replicates that way to the standby.
    primary.safe_sql(
        """
CREATE FUNCTION init_session() RETURNS event_trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE NOTICE 'init_session'; END $$;
CREATE EVENT TRIGGER init_session ON login
    EXECUTE FUNCTION init_session();
ALTER EVENT TRIGGER init_session ENABLE ALWAYS;
DROP EVENT TRIGGER init_session;
DROP FUNCTION init_session();
""",
        dbname="regress_login_evt",
    )

    # Wait for the standby to replay the CREATE/DROP catalog state.  This
    # probes "postgres", not regress_login_evt, so it does not disturb
    # the dangling flag.
    primary.wait_for_replay_catchup(standby)

    # The flag remains set in regress_login_evt on both sides.
    assert (
        primary.safe_sql(
            "SELECT dathasloginevt FROM pg_database "
            "WHERE datname = 'regress_login_evt'"
        )
        == "t"
    ), "dathasloginevt remains set on primary after DROP EVENT TRIGGER"
    assert (
        standby.safe_sql(
            "SELECT dathasloginevt FROM pg_database "
            "WHERE datname = 'regress_login_evt'"
        )
        == "t"
    ), "dathasloginevt replicated to standby"

    # A new connection to regress_login_evt on the standby exercises
    # EventTriggerOnLogin()'s cleanup branch.  With the
    # RecoveryInProgress() guard it succeeds; without it the session
    # aborts with a FATAL about AccessExclusiveLock.  A failure surfaces
    # either as a connection error at login time or as a query error.
    try:
        session = standby.connect("regress_login_evt")
    except LibpqError as exc:
        assert "cannot acquire lock mode AccessExclusiveLock" not in str(
            exc
        ), "no AccessExclusiveLock FATAL on standby login"
        raise AssertionError(
            "standby accepts connection to database with dangling "
            f"dathasloginevt: {exc}"
        ) from exc
    try:
        res = session.query("SELECT 1")
        assert res.error_message is None, (
            "standby accepts connection to database with dangling "
            f"dathasloginevt: {res.error_message}"
        )
        assert "cannot acquire lock mode AccessExclusiveLock" not in (
            res.error_message or ""
        ), "no AccessExclusiveLock FATAL on standby login"
        assert res.psqlout == "1"
    finally:
        session.close()

    # Finally exercise the primary-side cleanup that the standby is meant
    # to defer to.  Opening a fresh session against regress_login_evt on
    # the primary enters EventTriggerOnLogin()'s cleanup branch with the
    # trigger list empty; AccessExclusiveLock is allowed outside recovery,
    # so the flag is cleared in place.  The in-place update emits a
    # XLOG_HEAP_INPLACE record but does not assign an xid or write a
    # commit record, so the WAL is not auto-flushed -- force a flush via
    # pg_switch_wal() so the record reaches the standby.
    # A fresh login requires a brand-new backend.  safe_sql reuses a
    # cached per-database session, and the CREATE/DROP block above already
    # opened (and cached) a regress_login_evt session on the primary -- so a
    # plain safe_sql here would not trigger a new login.  Open a fresh
    # connection to force the login-event cleanup branch to run.
    cleanup = primary.connect("regress_login_evt")
    try:
        cleanup.query_safe("SELECT 1")
    finally:
        cleanup.close()
    assert (
        primary.safe_sql(
            "SELECT dathasloginevt FROM pg_database "
            "WHERE datname = 'regress_login_evt'"
        )
        == "f"
    ), "primary clears dathasloginevt on next login after DROP"

    primary.safe_sql("SELECT pg_switch_wal()")
    primary.wait_for_replay_catchup(standby)
    assert (
        standby.safe_sql(
            "SELECT dathasloginevt FROM pg_database "
            "WHERE datname = 'regress_login_evt'"
        )
        == "f"
    ), "cleared dathasloginevt replicates to standby"
