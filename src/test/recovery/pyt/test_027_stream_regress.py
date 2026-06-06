# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Run the standard regression tests with streaming replication, then verify
that primary and standby produce identical logical and catalog dumps and
that pg_stat_statements gathered the expected query jumbling data.

pg_regress, pg_dumpall and pg_dump are binaries under test, so they are run
as subprocesses (pg_regress via the run_pg_regress helper; the dump tools via
the node-scoped pg_bin, which sets PGHOST/PGPORT).  All SQL the test issues on
its own behalf goes through the in-process Session (safe_sql / sql).
"""

import os

import pytest

from pypg.regress import pg_regress_available, run_pg_regress
from pypg.util import slurp_file

# This test exercises the full regression suite via pg_regress, which is only
# available under the meson/make harness (PG_REGRESS in the environment).
pytestmark = pytest.mark.skipif(
    not pg_regress_available(),
    reason="pg_regress is not available (PG_REGRESS not set)",
)

# Repository root: this file lives at src/test/recovery/pyt, four levels up.
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
_REGRESS_DIR = os.path.join(_REPO_ROOT, "src", "test", "regress")


def _compare_files(a, b, msg):
    """Assert that files *a* and *b* have identical contents.

    On mismatch, show a small unified-ish diff to make the failure
    diagnosable.
    """
    content_a = slurp_file(a)
    content_b = slurp_file(b)
    if content_a == content_b:
        return

    import difflib

    diff = list(
        difflib.unified_diff(
            content_a.splitlines(keepends=True),
            content_b.splitlines(keepends=True),
            fromfile=a,
            tofile=b,
        )
    )
    # Keep the reported diff bounded so failures stay readable.
    snippet = "".join(diff[:200])
    raise AssertionError(f"{msg}\n{snippet}")


def test_027_stream_regress(create_pg, tmp_path):
    # Initialize primary node
    node_primary = create_pg("primary", start=False, allows_streaming=True)

    # Increase some settings that init makes too low by default.  A later
    # setting in postgresql.conf overrides an earlier one, so appending
    # max_connections = 25 raises the default of 10.
    node_primary.append_conf("max_connections = 25")
    node_primary.append_conf("max_prepared_transactions = 10")

    # Enable pg_stat_statements to force tests to do query jumbling.
    # pg_stat_statements.max should be large enough to hold all the entries
    # of the regression database.
    node_primary.append_conf(
        "shared_preload_libraries = 'pg_stat_statements'\n"
        "pg_stat_statements.max = 50000\n"
        "compute_query_id = 'regress'\n"
    )

    # We'll stick with the small default shared_buffers, but since that makes
    # synchronized seqscans more probable, it risks changing the results of
    # some test queries.  Disable synchronized seqscans to prevent that.
    node_primary.append_conf("synchronize_seqscans = off")

    # WAL consistency checking is resource intensive so require opt-in with the
    # PG_TEST_EXTRA environment variable.
    pg_test_extra = os.environ.get("PG_TEST_EXTRA", "")
    if "wal_consistency_checking" in pg_test_extra.split():
        node_primary.append_conf("wal_consistency_checking = all")

    node_primary.start()
    res = node_primary.sql(
        "SELECT pg_create_physical_replication_slot('standby_1')")
    assert res.error_message is None, "physical slot created on primary"

    backup_name = "my_backup"

    # Take backup
    node_primary.backup(backup_name)

    # Create streaming standby linking to primary
    node_standby_1 = create_pg("standby_1", start=False)
    node_standby_1.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby_1.append_conf("primary_slot_name = standby_1")
    node_standby_1.append_conf("max_standby_streaming_delay = 600s")
    node_standby_1.start()

    outputdir = str(tmp_path)

    # Run the regression tests against the primary.
    result = run_pg_regress(
        node_primary,
        inputdir=_REGRESS_DIR,
        outputdir=outputdir,
        schedule=os.path.join(_REGRESS_DIR, "parallel_schedule"),
        max_concurrent_tests=20,
    )
    assert result.returncode == 0, (
        "regression tests pass\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    assert node_primary._postmaster_alive(), \
        "primary alive after regression test run"
    assert node_standby_1._postmaster_alive(), \
        "standby alive after regression test run"

    # Clobber all sequences with their next value, so that we don't have
    # differences between nodes due to caching.
    node_primary.safe_sql(
        "select setval(seqrelid, nextval(seqrelid)) from pg_sequence",
        dbname="regression")

    # Wait for standby to catch up
    node_primary.wait_for_replay_catchup(node_standby_1)

    # Perform a logical dump of primary and standby, and check that they match.
    primary_dump = os.path.join(outputdir, "primary.dump")
    standby_dump = os.path.join(outputdir, "standby.dump")
    node_primary.command_ok(
        [
            "pg_dumpall",
            "--file", primary_dump,
            "--no-sync", "--no-statistics",
            "--restrict-key", "test",
            "--port", str(node_primary.port),
            "--no-unlogged-table-data",  # if unlogged, standby has schema only
        ],
        "dump primary server")
    node_standby_1.command_ok(
        [
            "pg_dumpall",
            "--file", standby_dump,
            "--no-sync", "--no-statistics",
            "--restrict-key", "test",
            "--port", str(node_standby_1.port),
        ],
        "dump standby server")
    _compare_files(primary_dump, standby_dump,
                   "compare primary and standby dumps")

    # Likewise for the catalogs of the regression database, after disabling
    # autovacuum to make fields like relpages stop changing.
    node_primary.append_conf("autovacuum = off")
    node_primary.restart()
    node_primary.wait_for_replay_catchup(node_standby_1)

    primary_catalogs = os.path.join(outputdir, "catalogs_primary.dump")
    standby_catalogs = os.path.join(outputdir, "catalogs_standby.dump")
    node_primary.command_ok(
        [
            "pg_dump",
            "--schema", "pg_catalog",
            "--file", primary_catalogs,
            "--no-sync",
            "--restrict-key", "test",
            "--port", str(node_primary.port),
            "--no-unlogged-table-data",
            "regression",
        ],
        "dump catalogs of primary server")
    node_standby_1.command_ok(
        [
            "pg_dump",
            "--schema", "pg_catalog",
            "--file", standby_catalogs,
            "--no-sync",
            "--restrict-key", "test",
            "--port", str(node_standby_1.port),
            "regression",
        ],
        "dump catalogs of standby server")
    _compare_files(primary_catalogs, standby_catalogs,
                   "compare primary and standby catalog dumps")

    # Check some data from pg_stat_statements.
    node_primary.safe_sql("CREATE EXTENSION pg_stat_statements")
    # This gathers data based on the first characters for some common query
    # types, checking that reports are generated for SELECT, DMLs, and DDL
    # queries with CREATE.
    result = node_primary.safe_sql(
        """WITH select_stats AS
  (SELECT upper(substr(query, 1, 6)) AS select_query
     FROM pg_stat_statements
     WHERE upper(substr(query, 1, 6)) IN ('SELECT', 'UPDATE',
                                          'INSERT', 'DELETE',
                                          'CREATE'))
  SELECT select_query, count(select_query) > 1 AS some_rows
    FROM select_stats
    GROUP BY select_query ORDER BY select_query;""")
    assert result == "CREATE|t\nDELETE|t\nINSERT|t\nSELECT|t\nUPDATE|t", \
        "check contents of pg_stat_statements on regression database"

    node_standby_1.stop()
    node_primary.stop()
