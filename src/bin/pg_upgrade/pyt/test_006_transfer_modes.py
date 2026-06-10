# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Tests pg_upgrade across the various file transfer modes."""

import os
import re

import pytest

from pypg.util import enable_localhost_tcp


def check_extension(node, extension_name):
    """Return True if *extension_name* is available on *node*."""
    result = node.safe_sql(
        "SELECT count(*) > 0 FROM pg_available_extensions "
        f"WHERE name = '{extension_name}';"
    )
    return result == "t"


def command_ok_or_fails_like(pg_bin, cmd, expected_stdout, expected_stderr, test_name):
    """Run *cmd*; succeed, or fail with stdout/stderr matching the patterns.

    Returns True if the command succeeded, False otherwise (after asserting
    the output matches).
    """
    res = pg_bin.result(cmd)
    if res.returncode != 0:
        assert re.search(expected_stdout, res.stdout), (
            f"{test_name}: stdout matches /{expected_stdout}/\n{res.stdout}"
        )
        assert re.search(expected_stderr, res.stderr), (
            f"{test_name}: stderr matches /{expected_stderr}/\n{res.stderr}"
        )
        return False
    return True


@pytest.mark.parametrize(
    "mode",
    ["--clone", "--copy", "--copy-file-range", "--link", "--swap"],
)
def test_mode(create_pg, tmp_path, mode):
    old = create_pg("old", start=False)
    new = create_pg("new", start=False)
    # pg_upgrade connects to the clusters over localhost TCP on Windows.
    enable_localhost_tcp(old)
    enable_localhost_tcp(new)

    # --swap can't be used to upgrade from versions older than 10, but this
    # framework only ever runs against the current build, so the old cluster is
    # always new enough; no version-based skip is needed.
    #
    # The create_pg fixture has already run initdb for both nodes (without
    # '-k'); checksums are enabled by default on the current build.

    # allow_in_place_tablespaces is available as far back as v10; the current
    # build always qualifies.
    new.append_conf("allow_in_place_tablespaces = true")
    old.append_conf("allow_in_place_tablespaces = true")

    # We can only test security labels if both the old and new installations
    # have dummy_seclabel.
    test_seclabel = True
    old.start()
    if not check_extension(old, "dummy_seclabel"):
        test_seclabel = False
    old.stop()
    new.start()
    if not check_extension(new, "dummy_seclabel"):
        test_seclabel = False
    new.stop()

    # Create a small variety of simple test objects on the old cluster.  We'll
    # check that these reach the new version after upgrading.
    old.start()
    old.safe_sql("CREATE TABLE test1 AS SELECT generate_series(1, 100)")
    old.safe_sql("CREATE DATABASE testdb1")
    old.safe_sql("CREATE TABLE test2 AS SELECT generate_series(200, 300)", dbname="testdb1")
    old.safe_sql("VACUUM FULL test2", dbname="testdb1")
    old.safe_sql("CREATE SEQUENCE testseq START 5432", dbname="testdb1")

    # Non-in-place tablespaces require an old installation ($oldinstall), which
    # this framework does not support, so those objects are not created here.

    # The old cluster is always >= v10, so we can test in-place tablespaces.
    old.safe_sql("CREATE TABLESPACE inplc_tblspc LOCATION ''")
    old.safe_sql("CREATE DATABASE testdb3 TABLESPACE inplc_tblspc")
    old.safe_sql(
        "CREATE TABLE test5 TABLESPACE inplc_tblspc "
        "AS SELECT generate_series(503, 606)"
    )
    old.safe_sql("CREATE TABLE test6 AS SELECT generate_series(607, 711)", dbname="testdb3")

    # While we are here, test handling of large objects.
    old.safe_sql(
        r"""
        CREATE ROLE regress_lo_1;
        CREATE ROLE regress_lo_2;

        SELECT lo_from_bytea(4532, '\xffffff00');
        COMMENT ON LARGE OBJECT 4532 IS 'test';

        SELECT lo_from_bytea(4533, '\x0f0f0f0f');
        ALTER LARGE OBJECT 4533 OWNER TO regress_lo_1;
        GRANT SELECT ON LARGE OBJECT 4533 TO regress_lo_2;
    """
    )

    if test_seclabel:
        old.safe_sql(
            r"""
            CREATE EXTENSION dummy_seclabel;

            SELECT lo_from_bytea(4534, '\x00ffffff');
            SECURITY LABEL ON LARGE OBJECT 4534 IS 'classified';
        """
        )
    old.stop()

    # Run pg_upgrade in a tmp directory to avoid leaving files like
    # delete_old_cluster.{sh,bat} in the source directory.
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = command_ok_or_fails_like(
            new.pg_bin,
            [
                "pg_upgrade", "--no-sync",
                "--old-datadir", old.data_dir,
                "--new-datadir", new.data_dir,
                "--old-bindir", old.bindir,
                "--new-bindir", new.bindir,
                "--socketdir", new.host,
                "--old-port", str(old.port),
                "--new-port", str(new.port),
                mode,
            ],
            r".* not supported on this platform|"
            r"could not .* between old and new data directories: .*",
            r"^$",
            f"pg_upgrade with transfer mode {mode}",
        )
    finally:
        os.chdir(cwd)

    # If pg_upgrade was successful, check that all of our test objects reached
    # the new version.
    if result:
        new.start()
        assert new.safe_sql("SELECT COUNT(*) FROM test1") == "100", (
            f"test1 data after pg_upgrade {mode}"
        )
        assert new.safe_sql("SELECT COUNT(*) FROM test2", dbname="testdb1") == "101", (
            f"test2 data after pg_upgrade {mode}"
        )
        assert new.safe_sql("SELECT nextval('testseq')", dbname="testdb1") == "5432", (
            f"sequence data after pg_upgrade {mode}"
        )

        # Tests for non-in-place tablespaces require $oldinstall; skipped.

        # Tests for in-place tablespaces (old cluster is always >= v10).
        assert new.safe_sql("SELECT COUNT(*) FROM test5") == "104", (
            f"test5 data after pg_upgrade {mode}"
        )
        assert new.safe_sql("SELECT COUNT(*) FROM test6", dbname="testdb3") == "105", (
            f"test6 data after pg_upgrade {mode}"
        )

        # Tests for large objects
        assert new.safe_sql("SELECT lo_get(4532)") == r"\xffffff00", (
            "LO contents after upgrade"
        )
        assert new.safe_sql(
            "SELECT obj_description(4532, 'pg_largeobject')"
        ) == "test", "comment on LO after pg_upgrade"

        assert new.safe_sql("SELECT lo_get(4533)") == r"\x0f0f0f0f", (
            "LO contents after upgrade"
        )
        assert new.safe_sql(
            "SELECT lomowner::regrole FROM pg_largeobject_metadata WHERE oid = 4533"
        ) == "regress_lo_1", "LO owner after upgrade"
        assert new.safe_sql(
            "SELECT lomacl FROM pg_largeobject_metadata WHERE oid = 4533"
        ) == "{regress_lo_1=rw/regress_lo_1,regress_lo_2=r/regress_lo_1}", (
            "LO ACL after upgrade"
        )

        if test_seclabel:
            assert new.safe_sql("SELECT lo_get(4534)") == r"\x00ffffff", (
                "LO contents after upgrade"
            )
            assert new.safe_sql(
                """
                SELECT label FROM pg_seclabel WHERE objoid = 4534
                AND classoid = 'pg_largeobject'::regclass
            """
            ) == "classified", "seclabel on LO after pg_upgrade"
        new.stop()
