# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_ctl start/stop/restart and resulting data-directory permissions."""

import os
import re
import stat
import time

from pypg.util import WINDOWS_OS, short_tempdir


def _chmod_recursive(path, dir_mode, file_mode):
    """Recursively chmod *path*, applying *dir_mode* to directories and
    *file_mode* to regular files (symlinks are left untouched)."""
    for root, dirs, files in os.walk(path):
        os.chmod(root, dir_mode)
        for d in dirs:
            os.chmod(os.path.join(root, d), dir_mode)
        for f in files:
            full = os.path.join(root, f)
            if not os.path.islink(full):
                os.chmod(full, file_mode)


def _check_mode_recursive(path, dir_mode, file_mode):
    """Check that every entry under *path* has the expected permissions.

    Returns True if all directories match *dir_mode* and all files match
    *file_mode*.
    """
    ok = True
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, d) for d in dirs]:
            actual = stat.S_IMODE(os.lstat(name).st_mode)
            if actual != dir_mode:
                print(
                    f"# Directory permissions check failed for {name}: "
                    f"expected {dir_mode:#o}, got {actual:#o}"
                )
                ok = False
        for f in files:
            full = os.path.join(root, f)
            if os.path.islink(full):
                continue
            actual = stat.S_IMODE(os.lstat(full).st_mode)
            if actual != file_mode:
                print(
                    f"# File permissions check failed for {full}: "
                    f"expected {file_mode:#o}, got {actual:#o}"
                )
                ok = False
    return ok


def test_start_stop(pg_bin, tmp_path):
    """Exercise pg_ctl start/stop/restart and the resulting file permissions."""
    pg_bin.program_help_ok("pg_ctl")
    pg_bin.program_version_ok("pg_ctl")
    pg_bin.program_options_handling_ok("pg_ctl")

    pg_bin.command_exit_is(
        ["pg_ctl", "start", "--pgdata", str(tmp_path / "nonexistent")],
        1,
        "pg_ctl start with nonexistent directory",
    )

    data_dir = str(tmp_path / "data")
    # Set up trust authentication by passing "-A trust" to initdb, which grants
    # trust for the local unix socket connections this test uses.
    pg_bin.command_ok(
        [
            "pg_ctl",
            "initdb",
            "--pgdata",
            data_dir,
            "--options",
            "--no-sync -A trust",
        ],
        "pg_ctl initdb",
    )

    # Use a short socket directory under /tmp to stay within the socket path
    # length limit.
    sockdir = short_tempdir()
    try:
        with open(
            os.path.join(data_dir, "postgresql.conf"), "a", encoding="utf-8"
        ) as conf:
            conf.write("fsync = off\n")
            conf.write("listen_addresses = ''\n")
            # Forward slashes: backslashes in the postgresql.conf string value
            # are mangled, so the socket directory would be wrong on Windows.
            conf.write(f"unix_socket_directories = '{sockdir.replace(chr(92), '/')}'\n")

        log_file = str(tmp_path / "001_start_stop_server.log")
        ctlcmd = ["pg_ctl", "start", "--pgdata", data_dir, "--log", log_file]
        pg_bin.command_like(
            ctlcmd, re.compile(r"done.*server started", re.S), "pg_ctl start"
        )

        # On Windows pg_ctl needs more than its ~2 second slop time to notice
        # the already-running postmaster; without the wait the second start
        # spuriously succeeds instead of failing.
        if WINDOWS_OS:
            time.sleep(3)
        pg_bin.command_fails(
            ["pg_ctl", "start", "--pgdata", data_dir],
            "second pg_ctl start fails",
        )
        pg_bin.command_ok(
            ["pg_ctl", "stop", "--pgdata", data_dir],
            "pg_ctl stop",
        )
        pg_bin.command_fails(
            ["pg_ctl", "stop", "--pgdata", data_dir],
            "second pg_ctl stop fails",
        )

        # Log file for default permission test.
        log_file = os.path.join(data_dir, "perm-test-600.log")

        pg_bin.command_ok(
            ["pg_ctl", "restart", "--pgdata", data_dir, "--log", log_file],
            "pg_ctl restart with server not running",
        )

        # Permissions on the log file should be default.  Unix-style
        # permissions are not supported on Windows, so skip the check there.
        if not WINDOWS_OS:
            assert os.path.isfile(log_file)
            assert _check_mode_recursive(data_dir, 0o700, 0o600)

        # Log file for group access test.
        log_file = os.path.join(data_dir, "perm-test-640.log")

        # Group access is not supported on Windows; skip that part there.
        if not WINDOWS_OS:
            pg_bin.command_ok(
                ["pg_ctl", "stop", "--pgdata", data_dir],
                "stop server before group permission test",
            )

            # Change the data dir mode so the log file will be created with
            # group read privileges on the next start.
            _chmod_recursive(data_dir, 0o750, 0o640)

            pg_bin.command_ok(
                ["pg_ctl", "start", "--pgdata", data_dir, "--log", log_file],
                "start server to check group permissions",
            )

            assert os.path.isfile(log_file)
            assert _check_mode_recursive(data_dir, 0o750, 0o640)

        pg_bin.command_ok(
            ["pg_ctl", "restart", "--pgdata", data_dir, "--log", log_file],
            "pg_ctl restart with server running",
        )

        pg_bin.command_ok(
            ["pg_ctl", "stop", "--pgdata", data_dir],
            "stop server at end of test",
        )
    finally:
        # Make sure the server is down even if an assertion failed.
        pg_bin.result(["pg_ctl", "stop", "--pgdata", data_dir, "-m", "immediate"])
