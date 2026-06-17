# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test that one session cannot read or modify another session's temp table.

Each session keeps its temp data in its own local buffer pool, and a different
backend has no visibility into those buffers, so any command that needs to look
at the data must be rejected.

DROP TABLE is intentionally allowed: it does not touch the table's
contents, and autovacuum relies on this to clean up orphaned temp
relations left behind by a crashed backend.

A regression caught here typically means a new buffer-access entry
point bypasses the RELATION_IS_OTHER_TEMP() check.  See
ReadBuffer_common(), StartReadBuffersImpl(), and read_stream_begin_impl()
for the existing checks.  When adding a new command or buffer-access
path, also add a corresponding case below.
"""

import os
import re


def _probe_stderr(probe, sql):
    """Run *sql* in the probing session and return its combined stderr.

    Returns both notices and any error message produced by the statement, with
    the leading notice/error severity prefixes preserved.  The probing session keeps a
    single backend across calls (distinct from the owner backend), which is
    fine because these probes never create persistent temp state.
    """
    probe.clear_stderr()
    probe.query(sql)
    return probe.get_stderr()


def test_013_temp_obj_multisession(create_pg):
    node = create_pg("temp_lock", start=False)
    node.start()

    # Owner session.  Created as a persistent libpq session (separate backend)
    # so it stays alive while the second session probes its temp objects.  Its
    # temp objects must persist for the duration of the test, so it must not
    # share the cached safe_sql backend.
    psql1 = node.connect()

    # Probing session: a dedicated backend, distinct from both the owner and
    # the cached safe_sql session, used to attempt cross-session access.
    probe = node.connect()

    try:
        # Initially create the table without an index, so read paths go
        # straight through the read-stream / buffer-manager entry points
        # without being masked by an index scan that would hit
        # ReadBuffer_common from nbtree.
        assert psql1.do("CREATE TEMP TABLE foo AS SELECT 42 AS val;")

        # Resolve the owner's temp schema so the probing session can refer to
        # the table by a fully-qualified name.
        tempschema = node.safe_sql(
            """
          SELECT n.nspname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE relname = 'foo' AND relpersistence = 't';
        """
        )
        assert re.match(r"^pg_temp_\d+$", tempschema), f"got temp schema: {tempschema}"

        # DML and SELECT have to read the table's data and therefore go
        # through the buffer manager.  With no index on the table, the planner
        # cannot use index access, so SELECT/UPDATE/DELETE/MERGE/COPY all run
        # through the read-stream path and are caught by
        # read_stream_begin_impl().

        stderr = _probe_stderr(probe, f"SELECT val FROM {tempschema}.foo;")
        assert re.search(
            r"cannot access temporary tables of other sessions", stderr
        ), f"SELECT (seqscan via read_stream): {stderr}"

        # INSERT goes through hio.c which calls ReadBufferExtended() to find a
        # page with free space; that hits the existing check before any data
        # is written.
        stderr = _probe_stderr(probe, f"INSERT INTO {tempschema}.foo VALUES (73);")
        assert re.search(
            r"cannot access temporary tables of other sessions", stderr
        ), f"INSERT (caught via hio.c): {stderr}"

        stderr = _probe_stderr(probe, f"UPDATE {tempschema}.foo SET val = NULL;")
        assert re.search(
            r"cannot access temporary tables of other sessions", stderr
        ), f"UPDATE: {stderr}"

        stderr = _probe_stderr(probe, f"DELETE FROM {tempschema}.foo;")
        assert re.search(
            r"cannot access temporary tables of other sessions", stderr
        ), f"DELETE: {stderr}"

        stderr = _probe_stderr(
            probe,
            f"MERGE INTO {tempschema}.foo USING (VALUES (42)) AS s(val) "
            "ON foo.val = s.val WHEN MATCHED THEN DELETE;",
        )
        assert re.search(
            r"cannot access temporary tables of other sessions", stderr
        ), f"MERGE: {stderr}"

        # We want to probe "COPY foo TO STDOUT".  The in-process libpq Session
        # has no COPY-out handling, and COPY TO STDOUT puts the connection
        # into copy-out mode before the relation's data is read, so it cannot
        # be driven through Session.query().  "COPY ... TO <file>" exercises
        # the same read path: the RELATION_IS_OTHER_TEMP() buffer-manager
        # check fires before any output file is touched, yielding the same
        # error as a normal command result.
        copy_out = os.path.join(node.data_dir, "t013_copyout.txt")
        stderr = _probe_stderr(probe, f"COPY {tempschema}.foo TO '{copy_out}';")
        assert re.search(
            r"cannot access temporary tables of other sessions", stderr
        ), f"COPY: {stderr}"

        # DDL and maintenance commands have their own command-specific checks
        # (older than the buffer-manager check above), so they fail with
        # command-specific error messages.  Verifying them here documents the
        # expected behaviour and guards against accidental removal of those
        # checks.

        stderr = _probe_stderr(probe, f"TRUNCATE TABLE {tempschema}.foo;")
        assert re.search(
            r"cannot truncate temporary tables of other sessions", stderr
        ), f"TRUNCATE: {stderr}"

        stderr = _probe_stderr(
            probe,
            f"ALTER TABLE {tempschema}.foo ALTER COLUMN val TYPE bigint;",
        )
        assert re.search(
            r"cannot alter temporary tables of other sessions", stderr
        ), f"ALTER TABLE: {stderr}"

        # VACUUM silently skips other sessions' temp tables (vacuum_rel()
        # returns without warning to avoid noise during database-wide VACUUM).
        # Verify that no error is reported, and that no buffer-access path is
        # hit.
        stderr = _probe_stderr(probe, f"VACUUM {tempschema}.foo;")
        assert stderr == "", f"VACUUM is silently skipped: {stderr}"

        stderr = _probe_stderr(probe, f"CLUSTER {tempschema}.foo;")
        assert re.search(
            r"cannot execute CLUSTER on temporary tables of other sessions",
            stderr,
        ), f"CLUSTER: {stderr}"

        # Now create an index to exercise the index-scan path.  nbtree calls
        # ReadBuffer (which is ReadBufferExtended -> ReadBuffer_common), so
        # this exercises a different chain of buffer-manager entry points.
        assert psql1.do("CREATE INDEX ON foo(val);")

        stderr = _probe_stderr(
            probe,
            "SET enable_seqscan = off; "
            f"SELECT val FROM {tempschema}.foo WHERE val = 42;",
        )
        assert re.search(
            r"cannot access temporary tables of other sessions", stderr
        ), f"index scan (ReadBuffer_common via nbtree): {stderr}"

        # ALTER INDEX goes through the same CheckAlterTableIsSafe() path as
        # ALTER TABLE, so it produces the same error.
        stderr = _probe_stderr(
            probe,
            f"ALTER INDEX {tempschema}.foo_val_idx SET (fillfactor = 50);",
        )
        assert re.search(
            r"cannot alter temporary tables of other sessions", stderr
        ), f"ALTER INDEX: {stderr}"

        # A function created by the owner in its own pg_temp using its own
        # row type can be observed via the catalog by a separate session.
        # ALTER FUNCTION and DROP FUNCTION on it must work as catalog
        # operations -- they don't read the underlying table -- which
        # documents the boundary between catalog and data access for temp
        # objects.
        assert psql1.do(
            "CREATE FUNCTION pg_temp.foo_id(r foo) RETURNS int LANGUAGE SQL "
            "AS 'SELECT r.val';"
        )

        stderr = _probe_stderr(
            probe,
            f"ALTER FUNCTION {tempschema}.foo_id({tempschema}.foo) "
            "SET search_path = pg_catalog;",
        )
        assert (
            stderr == ""
        ), f"ALTER FUNCTION on function over other session's row type: {stderr}"

        stderr = _probe_stderr(
            probe,
            f"DROP FUNCTION {tempschema}.foo_id({tempschema}.foo);",
        )
        assert (
            stderr == ""
        ), f"DROP FUNCTION on function over other session's row type: {stderr}"

        # DROP TABLE on another session's temp table is intentionally
        # permitted.  DROP doesn't touch the table's contents, and autovacuum
        # relies on this to remove temp relations orphaned by a crashed
        # backend.  Verify that the bare DROP succeeds without error.
        stderr = _probe_stderr(probe, f"DROP TABLE {tempschema}.foo;")
        assert stderr == "", f"DROP TABLE is allowed: {stderr}"

        # Cross-session CREATE FUNCTION scenario.  The owner creates a fresh
        # temp table foo2 in its pg_temp namespace, and a separate session
        # then creates a function whose argument type is that row type.
        # PostgreSQL allows this and emits a NOTICE: the function is moved
        # into the creator's pg_temp namespace with an auto-dependency on
        # the borrowed type, so it disappears together with the session that
        # created it.
        assert psql1.do("CREATE TEMP TABLE foo2 AS SELECT 42 AS val;")

        stderr = _probe_stderr(
            probe,
            f"CREATE FUNCTION public.cross_session_func(r {tempschema}.foo2) "
            "RETURNS int LANGUAGE SQL AS 'SELECT 1';",
        )
        assert re.search(
            r'function "cross_session_func" will be effectively temporary',
            stderr,
        ), (
            "CREATE FUNCTION using other session's row type is effectively "
            f"temporary: {stderr}"
        )

        # A bare DROP TABLE on foo2 now fails because cross_session_func
        # depends on its row type.  This is normal SQL dependency behaviour
        # and documents that DROP itself is not blocked by buffer-manager
        # checks -- we get a catalog-level error instead.
        stderr = _probe_stderr(probe, f"DROP TABLE {tempschema}.foo2;")
        assert re.search(
            r"cannot drop table .*\.foo2 because other objects depend on it",
            stderr,
        ), f"DROP TABLE blocked by cross-session dependency: {stderr}"

        foo2_oid = node.safe_sql("SELECT oid FROM pg_class WHERE relname='foo2';")

        # Cross-session LOCK TABLE scenario.  Ensure that LockRelationOid is
        # working properly for other temp tables since this mechanism is also
        # used by autovacuum during orphaned tables cleanup.
        psql2 = node.connect()
        try:
            assert psql2.do(
                "BEGIN;",
                f"LOCK TABLE {tempschema}.foo2 IN ACCESS SHARE MODE;",
            )

            # When the owner session ends, its temp objects are dropped via
            # the normal session-exit cleanup, which cascades through
            # DEPENDENCY_NORMAL and also removes the cross-session function
            # that depended on the temp row type.  This is the same mechanism
            # autovacuum relies on to clean up temp relations left behind by a
            # crashed backend.
            # Access share lock on the foo2 will block session-exit cleanup,
            # because an owner will try to acquire deletion lock all its temp
            # objects via findDependentObjects.
            log_offset = node.log_position()
            psql1.close()

            # Check whether session-exit cleanup is blocked.
            node.wait_for_log(
                rf"waiting for AccessExclusiveLock on relation {foo2_oid}",
                log_offset,
            )

            # Release lock on foo2 and allow session-exit cleanup to finish.
            assert psql2.do("COMMIT;")
        finally:
            psql2.close()

        # After releasing the lock, the owner can finally acquire
        # AccessExclusiveLock on foo2 and finish session-exit cleanup.  Verify
        # directly that both foo2 (the locked temp table) and
        # cross_session_func (which depended on its row type) have been
        # dropped.  Both being gone confirms the owner's cleanup got past the
        # blocked findDependentObjects() call and completed normally.
        assert node.poll_query_until(
            f"SELECT NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = {foo2_oid})"
        ), "foo2 was not cleaned up after owner session exit"

        assert (
            node.safe_sql(
                "SELECT count(*) FROM pg_proc WHERE proname = 'cross_session_func'"
            )
            == "0"
        ), "cross_session_func cleaned up by cascade from foo2"
    finally:
        probe.close()
        psql1.close()
