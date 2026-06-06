# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Pytest harness for the pg_rewind tests.

Each test consists of a cycle where a new cluster is first created with
initdb, and a streaming replication standby is set up to follow the primary.
Then the primary is shut down and the standby is promoted, and finally
pg_rewind is used to rewind the old primary, using the standby as the source.

A test uses the ``rewind`` fixture, which yields a :class:`RewindTest`
object.  Its methods should be called in this sequence:

1. ``setup_cluster`` - creates a PostgreSQL cluster that runs as the primary

2. ``start_primary`` - starts the primary server

3. ``create_standby`` - runs pg_basebackup to initialize a standby server,
   and sets it up to follow the primary.

4. ``promote_standby`` - runs "pg_ctl promote" to promote the standby server.
   The old primary keeps running.

5. ``run_pg_rewind`` - stops the old primary (if it's still running) and runs
   pg_rewind to synchronize it with the now-promoted standby server.

6. ``clean_rewind_test`` - stops both servers used in the test, if they're
   still running (also handled automatically by the create_pg fixture
   teardown).

The helpers ``primary_psql`` and ``standby_psql`` run SQL against the primary
and standby servers, respectively, using the in-process libpq Session (never
forking psql).  ``check_query`` runs a query against the primary and checks
its output against the expected text.
"""

import os
import shutil

import pytest

from pypg.command import PgBin


class RewindTest:
    """Driver object exposed to tests through the ``rewind`` fixture."""

    def __init__(self, create_pg, bindir):
        self._create_pg = create_pg
        # pg_rewind / pg_ctl etc. are invoked with explicit data dirs and
        # connection strings, so a plain PgBin (no node env) is sufficient.
        self._pg_bin = PgBin(bindir)
        self.node_primary = None
        self.node_standby = None
        # Whether the cluster was initialized with group access (initdb -g),
        # which changes the expected data-directory file permissions.
        self._group_access = False

    # -- psql helpers --------------------------------------------------------

    def primary_psql(self, sql, dbname="postgres"):
        """Run *sql* against the primary; return trimmed text output."""
        return self.node_primary.safe_sql(sql, dbname=dbname)

    def standby_psql(self, sql, dbname="postgres"):
        """Run *sql* against the standby; return trimmed text output."""
        return self.node_standby.safe_sql(sql, dbname=dbname)

    def check_query(self, query, expected, test_name):
        """Run *query* against the primary and assert the output matches.

        The output is fetched in-process (libpq) with no formatting, which is
        the equivalent of psql --no-align --tuples-only.  *expected* is the
        text expected, with a trailing newline per row.
        """
        result = self.node_primary.sql(query)
        # Reproduce psql -At output: each row's columns joined by '|', rows
        # joined by newlines, with a trailing newline.
        lines = []
        for row in result.rows:
            lines.append("|".join("" if v is None else str(v) for v in row))
        stdout = "".join(line + "\n" for line in lines)
        assert stdout == expected, (
            f"{test_name}: query result matches\n"
            f"got:\n{stdout!r}\nexpected:\n{expected!r}"
        )

    # -- cluster lifecycle ---------------------------------------------------

    def setup_cluster(self, extra_name=None, extra=None):
        """Create the primary node; data checksums are on by default.

        *extra_name* differentiates clusters; *extra* is a list of extra
        arguments for initdb.
        """
        name = "primary" + (f"_{extra_name}" if extra_name else "")

        # Initialize primary.  Under the trust auth this framework uses, the
        # rewind_user role just needs to exist, which start_primary's SQL
        # ensures.
        self._group_access = bool(extra) and (
            "-g" in extra or "--allow-group-access" in extra
        )
        # Files the test itself writes into PGDATA (standby.signal,
        # postgresql.auto.conf, ...) must match the cluster's group-access
        # mode for the permission checks: 0027 -> 0640/0750 with group access,
        # 0077 -> 0600/0700 without.  (The server sets its own umask from the
        # data directory, but test-written files honor the process umask.)
        os.umask(0o027 if self._group_access else 0o077)
        self.node_primary = self._create_pg(
            name,
            start=False,
            allows_streaming=True,
            initdb_extra=extra,
        )

        # Set wal_keep_size to prevent WAL segment recycling after enforced
        # checkpoints in the tests.
        self.node_primary.append_conf(
            "\nwal_keep_size = 320MB\nallow_in_place_tablespaces = on\n"
        )

    def start_primary(self):
        """Start the primary and create the minimal-privilege rewind role."""
        self.node_primary.start()

        # Create custom role which is used to run pg_rewind, and adjust its
        # permissions to the minimum necessary.
        self.node_primary.safe_sql(
            """
            CREATE ROLE rewind_user LOGIN;
            GRANT EXECUTE ON function pg_catalog.pg_ls_dir(text, boolean, boolean)
              TO rewind_user;
            GRANT EXECUTE ON function pg_catalog.pg_stat_file(text, boolean)
              TO rewind_user;
            GRANT EXECUTE ON function pg_catalog.pg_read_binary_file(text)
              TO rewind_user;
            GRANT EXECUTE ON function pg_catalog.pg_read_binary_file(text, bigint, bigint, boolean)
              TO rewind_user;"""
        )

    def create_standby(self, extra_name=None):
        """pg_basebackup-initialize a standby that follows the primary."""
        name = "standby" + (f"_{extra_name}" if extra_name else "")
        self.node_standby = self._create_pg(name, start=False)

        self.node_primary.backup("my_backup")
        self.node_standby.init_from_backup(self.node_primary, "my_backup")

        # Build primary_conninfo without nested single quotes (the value is
        # itself single-quoted in postgresql.conf).  application_name is set to
        # the standby's node name so wait_for_catchup can locate it in
        # pg_stat_replication.
        connstr_primary = (
            f"host={self.node_primary.host} port={self.node_primary.port} "
            f"dbname=postgres application_name={self.node_standby.name}"
        )
        self.node_standby.append_conf(f"\nprimary_conninfo='{connstr_primary}'\n")
        self.node_standby.set_standby_mode()

        # Start standby
        self.node_standby.start()

        # The standby may have WAL to apply before it matches the primary.
        # That is fine, because no test examines the standby before promotion.

    def promote_standby(self):
        """Wait for the standby to catch up, then promote it."""
        # Wait for the standby to receive and write all WAL.
        self.node_primary.wait_for_catchup(self.node_standby, "write")

        # Now promote standby (the caller then diverges the two servers).
        self.node_standby.promote()

    def run_pg_rewind(self, test_mode):
        """Stop the old primary and rewind it onto the promoted standby.

        *test_mode* is one of 'local', 'remote' or 'archive'.
        """
        primary_pgdata = self.node_primary.data_dir
        standby_pgdata = self.node_standby.data_dir

        # Append the rewind-specific role to the connection string.
        standby_connstr = self.node_standby.connstr("postgres") + " user=rewind_user"

        # A scratch directory to stash the primary's postgresql.conf, which
        # would otherwise be overwritten during the rewind.
        tmp_folder = os.path.join(self.node_primary.basedir, "rewind_tmp")
        os.makedirs(tmp_folder, exist_ok=True)

        if test_mode == "archive":
            # pg_rewind is tested with --restore-target-wal by moving all
            # WAL files to a secondary location.  This leads to a failure in
            # ensureCleanShutdown(), forcing the use of --no-ensure-shutdown,
            # so stop the primary gracefully here.
            self.node_primary.stop()
        else:
            # Stop the primary and be ready to perform the rewind.  The
            # cluster needs recovery to finish once, and pg_rewind makes sure
            # that it happens automatically.
            self.node_primary.stop("immediate")

        # Keep a temporary postgresql.conf for primary node or it would be
        # overwritten during the rewind.
        saved_conf = os.path.join(tmp_folder, "primary-postgresql.conf.tmp")
        shutil.copy(os.path.join(primary_pgdata, "postgresql.conf"), saved_conf)

        # Now run pg_rewind
        if test_mode == "local":
            # Do rewind using a local pgdata as source.  Stop the standby
            # (source) first, as pg_rewind requires a cleanly stopped source.
            self.node_standby.stop()
            self._pg_bin.command_ok(
                [
                    "pg_rewind",
                    "--debug",
                    "--source-pgdata",
                    standby_pgdata,
                    "--target-pgdata",
                    primary_pgdata,
                    "--no-sync",
                    "--config-file",
                    saved_conf,
                ],
                "pg_rewind local",
            )
        elif test_mode == "remote":
            # Do rewind using a remote connection as source, generating
            # recovery configuration automatically.
            self._pg_bin.command_ok(
                [
                    "pg_rewind",
                    "--debug",
                    "--source-server",
                    standby_connstr,
                    "--target-pgdata",
                    primary_pgdata,
                    "--no-sync",
                    "--write-recovery-conf",
                    "--config-file",
                    saved_conf,
                ],
                "pg_rewind remote",
            )

            # Check that pg_rewind with dbname and --write-recovery-conf wrote
            # the dbname in the generated primary_conninfo value.
            with open(
                os.path.join(primary_pgdata, "postgresql.auto.conf"),
                encoding="utf-8",
            ) as fh:
                auto_conf = fh.read()
            assert "dbname=postgres" in auto_conf, "recovery conf file sets dbname"

            # Check that standby.signal is here as recovery configuration was
            # requested.
            assert os.path.exists(
                os.path.join(primary_pgdata, "standby.signal")
            ), "standby.signal created after pg_rewind"

            # Now, when pg_rewind apparently succeeded with minimal
            # permissions, add REPLICATION privilege.  So we could test that
            # the new standby is able to connect to the new primary with the
            # generated config.
            self.node_standby.safe_sql("ALTER ROLE rewind_user WITH REPLICATION;")
        elif test_mode == "archive":
            # Do rewind using a local pgdata as source and a specified
            # directory with the target WAL archive.  The old primary has to
            # be stopped at this point (done above).

            # Remove the existing archive directory and move all WAL segments
            # from the old primary to the archives.  These will be used by
            # pg_rewind.
            archive_dir = self.node_primary.archive_dir
            pg_wal = os.path.join(primary_pgdata, "pg_wal")
            if os.path.isdir(archive_dir):
                shutil.rmtree(archive_dir)
            shutil.copytree(pg_wal, archive_dir, symlinks=True)

            # Fast way to remove entire directory content.
            shutil.rmtree(pg_wal)
            os.mkdir(pg_wal)

            # Make sure that directories have the right umask as this is
            # required by a follow-up check on permissions.
            os.chmod(archive_dir, 0o700)
            os.chmod(pg_wal, 0o700)

            # Add an appropriate restore_command to the target cluster (from
            # the primary's own archive dir, in non-standby recovery mode).
            self.node_primary.enable_restoring(self.node_primary, standby=False)

            # Stop the new primary (source) and be ready to perform the rewind.
            self.node_standby.stop()

            # Note the use of --no-ensure-shutdown here.  WAL files are gone
            # in this mode and the primary has been stopped gracefully
            # already.  --config-file reuses the original postgresql.conf as
            # restore_command has been enabled above.
            self._pg_bin.command_ok(
                [
                    "pg_rewind",
                    "--debug",
                    "--source-pgdata",
                    standby_pgdata,
                    "--target-pgdata",
                    primary_pgdata,
                    "--no-sync",
                    "--no-ensure-shutdown",
                    "--restore-target-wal",
                    "--config-file",
                    os.path.join(primary_pgdata, "postgresql.conf"),
                ],
                "pg_rewind archive",
            )
        else:
            raise ValueError("Incorrect test mode specified")

        # Now move back postgresql.conf with old settings.
        shutil.move(saved_conf, os.path.join(primary_pgdata, "postgresql.conf"))
        # Restore the right permissions on the moved-back file: 0640 with
        # group access (initdb -g), 0600 otherwise.
        os.chmod(
            os.path.join(primary_pgdata, "postgresql.conf"),
            0o640 if self._group_access else 0o600,
        )

        # Plug-in the rewound node to the now-promoted standby node.
        if test_mode != "remote":
            self.node_primary.append_conf(
                "\nprimary_conninfo='host={host} port={port}'\n".format(
                    host=self.node_standby.host,
                    port=self.node_standby.port,
                )
            )
            self.node_primary.set_standby_mode()

        # Restart the primary to check that the rewind went correctly.
        self.node_primary.start()

    def clean_rewind_test(self):
        """Stop both servers, if they're still running."""
        if self.node_primary is not None:
            self.node_primary.teardown()
        if self.node_standby is not None:
            self.node_standby.teardown()


@pytest.fixture
def rewind(create_pg, bindir):
    """Yield a :class:`RewindTest` driver; tear down both nodes afterwards.

    Set umask(0077) so that files the test (and the server) create in PGDATA
    keep the default 0600/0700 permissions that 001_basic's
    check_mode_recursive() asserts on.  This covers files such as
    standby.signal and postgresql.auto.conf.
    """
    old_umask = os.umask(0o077)
    try:
        rt = RewindTest(create_pg, bindir)
        yield rt
        rt.clean_rewind_test()
    finally:
        os.umask(old_umask)
