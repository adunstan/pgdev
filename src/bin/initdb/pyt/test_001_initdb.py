# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for initdb: option handling, error paths, and successful cluster creation."""

# To test successful data directory creation with an additional feature, first
# try to elaborate the "successful creation" test instead of adding a test.
# Successful initdb consumes much time and I/O.

import os
import re
import stat

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pg_config_value(pg_config, option):
    import subprocess

    return subprocess.run(
        [pg_config, option], stdout=subprocess.PIPE, text=True, check=True
    ).stdout.strip()


def _check_pg_config(pg_config, define):
    """Return True if *define* appears in the installed pg_config.h."""
    include_server = _pg_config_value(pg_config, "--includedir-server")
    header = os.path.join(include_server, "pg_config.h")
    with open(header, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if define in line:
                return True
    return False


def _check_mode_recursive(directory, expected_dir_mode, expected_file_mode,
                          ignore_list=None):
    """Recursively verify file/dir permission bits under *directory*.

    Pure filesystem check using os.walk + os.lstat; symlinks are followed.
    Returns True/False.
    """
    ignore = set()
    for item in ignore_list or []:
        ignore.add(os.path.join(directory, item))

    result = True

    def check_one(path):
        nonlocal result
        if path in ignore:
            return
        try:
            st = os.stat(path)
        except FileNotFoundError:
            # Allow ENOENT.  A running server can delete files.
            return
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISREG(st.st_mode):
            if mode != expected_file_mode:
                print("%s mode must be %04o" % (path, expected_file_mode))
                result = False
        elif stat.S_ISDIR(st.st_mode):
            if mode != expected_dir_mode:
                print("%s mode must be %04o" % (path, expected_dir_mode))
                result = False

    # Check the top directory itself, then everything underneath, following
    # symlinks.
    check_one(directory)
    for root, dirs, files in os.walk(directory, followlinks=True):
        for name in dirs:
            check_one(os.path.join(root, name))
        for name in files:
            check_one(os.path.join(root, name))

    return result


@pytest.fixture(scope="module")
def with_icu(pg_config):
    return _check_pg_config(pg_config, "#define USE_ICU 1")


@pytest.fixture(scope="module")
def supports_syncfs(pg_config):
    return _check_pg_config(pg_config, "#define HAVE_SYNCFS 1")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_program_help_version_options(pg_bin):
    pg_bin.program_help_ok("initdb")
    pg_bin.program_version_ok("initdb")
    pg_bin.program_options_handling_ok("initdb")


def test_initial_failures(pg_bin, tmp_path):
    """Various invalid invocations that should fail before any creation."""
    xlogdir = str(tmp_path / "pgxlog")
    datadir = str(tmp_path / "data")

    pg_bin.command_fails(
        ["initdb", "--sync-only", str(tmp_path / "nonexistent")],
        "sync missing data directory",
    )

    os.mkdir(xlogdir)
    os.mkdir(os.path.join(xlogdir, "lost+found"))
    pg_bin.command_fails(
        ["initdb", "--waldir", xlogdir, datadir],
        "existing nonempty xlog directory",
    )
    os.rmdir(os.path.join(xlogdir, "lost+found"))
    pg_bin.command_fails(
        ["initdb", "--waldir", "pgxlog", datadir],
        "relative xlog directory not allowed",
    )

    pg_bin.command_fails(
        ["initdb", "--username", "pg_test", datadir],
        'role names cannot begin with "pg_"',
    )


def test_successful_creation_and_permissions(pg_bin, tmp_path):
    """Successful creation, default permissions, control file and sync."""
    xlogdir = str(tmp_path / "pgxlog")
    datadir = str(tmp_path / "data")
    os.mkdir(xlogdir)
    os.mkdir(datadir)

    # make sure we run one successful test without a TZ setting so we test
    # initdb's time zone setting code.  Also exercise --text-search-config and
    # --set options.  PgBin builds the child environment from os.environ, so
    # temporarily drop TZ for the duration of this call.
    saved_tz = os.environ.pop("TZ", None)
    try:
        pg_bin.command_ok(
            [
                "initdb", "--no-sync",
                "--text-search-config", "german",
                "--set", "default_text_search_config=german",
                "--waldir", xlogdir,
                datadir,
            ],
            "successful creation",
        )
    finally:
        if saved_tz is not None:
            os.environ["TZ"] = saved_tz

    # Permissions on PGDATA should be default (Windows skipped: Linux only).
    assert _check_mode_recursive(datadir, 0o700, 0o600), "check PGDATA permissions"

    # Control file should tell that data checksums are enabled by default.
    pg_bin.command_like(
        ["pg_controldata", datadir],
        re.compile(r"Data page checksum version:.*1"),
        "checksums are enabled in control file",
    )

    pg_bin.command_ok(["initdb", "--sync-only", datadir], "sync only")
    pg_bin.command_ok(
        ["initdb", "--sync-only", "--no-sync-data-files", datadir],
        "--no-sync-data-files",
    )
    pg_bin.command_fails(["initdb", datadir], "existing data directory")


def test_sync_method_syncfs(pg_bin, tmp_path, supports_syncfs):
    datadir = str(tmp_path / "data")
    os.mkdir(datadir)
    pg_bin.command_ok(
        ["initdb", "--no-sync", datadir],
        "create cluster for syncfs test",
    )

    cmd = ["initdb", "--sync-only", datadir, "--sync-method", "syncfs"]
    if supports_syncfs:
        pg_bin.command_ok(cmd, "sync method syncfs")
    else:
        pg_bin.command_fails(cmd, "sync method syncfs")


def test_group_access(pg_bin, tmp_path):
    """Check group access on PGDATA (Windows/cygwin skipped: Linux only)."""
    datadir_group = str(tmp_path / "data_group")
    pg_bin.command_ok(
        ["initdb", "--allow-group-access", datadir_group],
        "successful creation with group access",
    )
    assert _check_mode_recursive(datadir_group, 0o750, 0o640), \
        "check PGDATA permissions"


def test_locale_provider_icu(pg_bin, tmp_path, with_icu):
    """ICU locale provider tests, or the no-ICU fallback check."""
    if with_icu:
        pg_bin.command_fails_like(
            ["initdb", "--no-sync", "--locale-provider", "icu",
             str(tmp_path / "data2")],
            re.compile(r"initdb: error: locale must be specified if provider is icu"),
            "locale provider ICU requires --icu-locale",
        )

        pg_bin.command_ok(
            ["initdb", "--no-sync", "--locale-provider", "icu",
             "--icu-locale", "en", str(tmp_path / "data3")],
            "option --icu-locale",
        )

        pg_bin.command_like(
            [
                "initdb", "--no-sync",
                "--auth", "trust",
                "--locale-provider", "icu",
                "--locale", "und",
                "--lc-collate", "C",
                "--lc-ctype", "C",
                "--lc-messages", "C",
                "--lc-numeric", "C",
                "--lc-monetary", "C",
                "--lc-time", "C",
                str(tmp_path / "data4"),
            ],
            re.compile(r"^\s+default collation:\s+und\n", re.MULTILINE),
            "options --locale-provider=icu --locale=und --lc-*=C",
        )

        pg_bin.command_fails_like(
            ["initdb", "--no-sync", "--locale-provider", "icu",
             "--icu-locale", "@colNumeric=lower", str(tmp_path / "dataX")],
            re.compile(r"could not open collator for locale"),
            "fails for invalid ICU locale",
        )

        pg_bin.command_fails_like(
            ["initdb", "--no-sync", "--locale-provider", "icu",
             "--encoding", "SQL_ASCII", "--icu-locale", "en",
             str(tmp_path / "dataX")],
            re.compile(r"error: encoding mismatch"),
            "fails for encoding not supported by ICU",
        )

        pg_bin.command_fails_like(
            ["initdb", "--no-sync", "--locale-provider", "icu",
             "--icu-locale", "nonsense-nowhere", str(tmp_path / "dataX")],
            re.compile(
                r'error: locale "nonsense-nowhere" has unknown language "nonsense"'),
            "fails for nonsense language",
        )

        pg_bin.command_fails_like(
            ["initdb", "--no-sync", "--locale-provider", "icu",
             "--icu-locale", "@colNumeric=lower", str(tmp_path / "dataX")],
            re.compile(
                r'could not open collator for locale "und-u-kn-lower": '
                r"U_ILLEGAL_ARGUMENT_ERROR"),
            "fails for invalid collation argument",
        )
    else:
        pg_bin.command_fails(
            ["initdb", "--no-sync", "--locale-provider", "icu",
             str(tmp_path / "data2")],
            "locale provider ICU fails since no ICU support",
        )


def test_locale_provider_builtin(pg_bin, tmp_path):
    pg_bin.command_fails(
        ["initdb", "--no-sync", "--locale-provider", "builtin",
         str(tmp_path / "data6")],
        "locale provider builtin fails without --locale",
    )

    pg_bin.command_ok(
        ["initdb", "--no-sync", "--locale-provider", "builtin",
         "--locale", "C", str(tmp_path / "data7")],
        "locale provider builtin with --locale",
    )

    pg_bin.command_ok(
        ["initdb", "--no-sync", "--locale-provider", "builtin",
         "--encoding", "UTF-8", "--lc-collate", "C", "--lc-ctype", "C",
         "--builtin-locale", "C.UTF-8", str(tmp_path / "data8")],
        "locale provider builtin with --encoding=UTF-8 --builtin-locale=C.UTF-8",
    )

    pg_bin.command_fails(
        ["initdb", "--no-sync", "--locale-provider", "builtin",
         "--encoding", "SQL_ASCII", "--lc-collate", "C", "--lc-ctype", "C",
         "--builtin-locale", "C.UTF-8", str(tmp_path / "data9")],
        "locale provider builtin with --builtin-locale=C.UTF-8 fails for SQL_ASCII",
    )

    pg_bin.command_ok(
        ["initdb", "--no-sync", "--locale-provider", "builtin",
         "--lc-ctype", "C", "--locale", "C", str(tmp_path / "data10")],
        "locale provider builtin with --lc-ctype",
    )

    pg_bin.command_fails(
        ["initdb", "--no-sync", "--locale-provider", "builtin",
         "--icu-locale", "en", str(tmp_path / "dataX")],
        "fails for locale provider builtin with ICU locale",
    )

    pg_bin.command_fails(
        ["initdb", "--no-sync", "--locale-provider", "builtin",
         "--icu-rules", '""', str(tmp_path / "dataX")],
        "fails for locale provider builtin with ICU rules",
    )


def test_invalid_provider_and_options(pg_bin, tmp_path):
    pg_bin.command_fails(
        ["initdb", "--no-sync", "--locale-provider", "xyz",
         str(tmp_path / "dataX")],
        "fails for invalid locale provider",
    )

    pg_bin.command_fails(
        ["initdb", "--no-sync", "--locale-provider", "libc",
         "--icu-locale", "en", str(tmp_path / "dataX")],
        "fails for invalid option combination",
    )

    pg_bin.command_fails(
        ["initdb", "--no-sync", "--set", "foo=bar", str(tmp_path / "dataX")],
        "fails for invalid --set option",
    )


def test_set_case_insensitive(pg_bin, tmp_path):
    """Multiple --set parameters are added case insensitively."""
    from pypg import util

    datay = str(tmp_path / "dataY")
    pg_bin.command_ok(
        ["initdb", "--no-sync",
         "--set", "work_mem=128",
         "--set", "Work_Mem=256",
         "--set", "WORK_MEM=512",
         datay],
        "multiple --set options with different case",
    )

    conf = util.slurp_file(os.path.join(datay, "postgresql.conf"))
    assert not re.search(r"^WORK_MEM = ", conf, re.MULTILINE), \
        "WORK_MEM should not be configured"
    assert not re.search(r"^Work_Mem = ", conf, re.MULTILINE), \
        "Work_Mem should not be configured"
    assert re.search(r"^work_mem = 512", conf, re.MULTILINE), \
        "work_mem should be in config"


def test_no_data_checksums(pg_bin, tmp_path):
    """Test the --no-data-checksums flag and that pg_checksums then fails."""
    datadir_nochecksums = str(tmp_path / "data_no_checksums")

    pg_bin.command_ok(
        ["initdb", "--no-data-checksums", datadir_nochecksums],
        "successful creation without data checksums",
    )

    # Control file should tell that data checksums are disabled.
    pg_bin.command_like(
        ["pg_controldata", datadir_nochecksums],
        re.compile(r"Data page checksum version:.*0"),
        "checksums are disabled in control file",
    )

    # pg_checksums fails with checksums disabled.
    pg_bin.command_fails(
        ["pg_checksums", "--pgdata", datadir_nochecksums],
        "pg_checksums fails with data checksum disabled",
    )
