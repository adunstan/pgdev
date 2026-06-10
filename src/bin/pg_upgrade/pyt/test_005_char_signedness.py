# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Tests for handling the default char signedness during upgrade."""

import os

from pypg.util import enable_localhost_tcp


def test_005_char_signedness(pg_bin, create_pg, bindir, tmp_path):
    # Can be changed to test the other modes
    mode = os.environ.get("PG_TEST_PG_UPGRADE_MODE") or "--copy"

    # Initialize old and new clusters.  pg_upgrade needs the old cluster
    # stopped and the new cluster freshly initdb'd, so neither is started.
    old = create_pg("old", start=False)
    new = create_pg("new", start=False)
    # pg_upgrade connects to the clusters over localhost TCP on Windows.
    enable_localhost_tcp(old)
    enable_localhost_tcp(new)

    # Check the default char signedness of both the old and the new clusters.
    # Newly created clusters unconditionally use 'signed'.
    pg_bin.command_like(
        ["pg_controldata", old.data_dir],
        r"Default char data signedness:\s+signed",
        "default char signedness of old cluster is signed in control file",
    )
    pg_bin.command_like(
        ["pg_controldata", new.data_dir],
        r"Default char data signedness:\s+signed",
        "default char signedness of new cluster is signed in control file",
    )

    # Set the old cluster's default char signedness to unsigned for test.
    pg_bin.command_ok(
        [
            "pg_resetwal",
            "--char-signedness", "unsigned",
            "--force",
            old.data_dir,
        ],
        "set old cluster's default char signedness to unsigned",
    )

    # Check if the value is successfully updated.
    pg_bin.command_like(
        ["pg_controldata", old.data_dir],
        r"Default char data signedness:\s+unsigned",
        "updated default char signedness is unsigned in control file",
    )

    # In a VPATH build, we'll be started in the source directory, but we want
    # to run pg_upgrade in the build directory so that any files generated
    # finish in it, like delete_old_cluster.{sh,bat}.  Use the test's tmp_path
    # as a clean cwd since pg_upgrade writes logs there.
    os.chdir(tmp_path)

    # Cannot use --set-char-signedness option for upgrading from v18+
    pg_bin.command_checks_all(
        [
            "pg_upgrade", "--no-sync",
            "--old-datadir", old.data_dir,
            "--new-datadir", new.data_dir,
            "--old-bindir", bindir,
            "--new-bindir", bindir,
            "--socketdir", new.host,
            "--old-port", str(old.port),
            "--new-port", str(new.port),
            "--set-char-signedness", "signed",
            mode,
        ],
        1,
        [r"option --set-char-signedness cannot be used"],
        [],
        "--set-char-signedness option cannot be used for upgrading from v18 or later",
    )

    # pg_upgrade should be successful.
    pg_bin.command_ok(
        [
            "pg_upgrade", "--no-sync",
            "--old-datadir", old.data_dir,
            "--new-datadir", new.data_dir,
            "--old-bindir", bindir,
            "--new-bindir", bindir,
            "--socketdir", new.host,
            "--old-port", str(old.port),
            "--new-port", str(new.port),
            mode,
        ],
        "run of pg_upgrade",
    )

    # Check if the default char signedness of the new cluster inherited
    # the old cluster's value.
    pg_bin.command_like(
        ["pg_controldata", new.data_dir],
        r"Default char data signedness:\s+unsigned",
        "the default char signedness is updated during pg_upgrade",
    )
