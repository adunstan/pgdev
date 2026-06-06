# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_dump / pg_restore / pg_dumpall command-line handling.

Tests pg_dump / pg_restore / pg_dumpall command-line option handling and
error messages.  None of these cases require a running server (they all
exercise invalid options and disallowed option combinations), so the
programs under test are run as subprocesses through pg_bin.
"""

import os
import re
import subprocess

import pytest


def _have_pg_config_define(define):
    """Return True if the installed pg_config.h contains the given #define."""
    try:
        out = subprocess.run(
            ["pg_config", "--includedir"],
            stdout=subprocess.PIPE, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return False
    header = os.path.join(out, "pg_config.h")
    try:
        with open(header, encoding="utf-8", errors="replace") as fh:
            return define in fh.read()
    except OSError:
        return False


#########################################
# Basic checks

def test_program_help_version_options(pg_bin):
    for prog in ("pg_dump", "pg_restore", "pg_dumpall"):
        pg_bin.program_help_ok(prog)
        pg_bin.program_version_ok(prog)
        pg_bin.program_options_handling_ok(prog)


#########################################
# Test various invalid options and disallowed combinations.
# Doesn't require a PG instance to be set up.
#
# Each entry: (name, argv, expected stderr regexp).  Most cases match a
# literal string, so we use re.escape() on the literal text.
# A few cases use real regexp patterns, kept verbatim (marked raw=True).

# (name, argv, pattern, raw)
_CASES = [
    ("pg_dump: too many command-line arguments",
     ["pg_dump", "qqq", "abc"],
     'pg_dump: error: too many command-line arguments (first is "abc")'),

    ("pg_restore: too many command-line arguments",
     ["pg_restore", "qqq", "abc"],
     'pg_restore: error: too many command-line arguments (first is "abc")'),

    ("pg_dumpall: too many command-line arguments",
     ["pg_dumpall", "qqq", "abc"],
     'pg_dumpall: error: too many command-line arguments (first is "qqq")'),

    ("pg_dump: options -a/--data-only and -s/--schema-only cannot be used together",
     ["pg_dump", "-s", "-a"],
     "pg_dump: error: options -a/--data-only and -s/--schema-only cannot be used together"),

    ("pg_dump: error: options -s/--schema-only and --statistics-only cannot be used together",
     ["pg_dump", "-s", "--statistics-only"],
     "pg_dump: error: options -s/--schema-only and --statistics-only cannot be used together"),

    ("pg_dump: error: options -a/--data-only and --statistics-only cannot be used together",
     ["pg_dump", "-a", "--statistics-only"],
     "pg_dump: error: options -a/--data-only and --statistics-only cannot be used together"),

    ("pg_dump: options --include-foreign-data and -s/--schema-only cannot be used together",
     ["pg_dump", "-s", "--include-foreign-data=xxx"],
     "pg_dump: error: options --include-foreign-data and -s/--schema-only cannot be used together"),

    ("pg_dump: options --statistics-only and --no-statistics cannot be used together",
     ["pg_dump", "--statistics-only", "--no-statistics"],
     "pg_dump: error: options --statistics-only and --no-statistics cannot be used together"),

    ("pg_dump: option --include-foreign-data is not supported with parallel backup",
     ["pg_dump", "-j2", "--include-foreign-data=xxx"],
     "pg_dump: error: option --include-foreign-data is not supported with parallel backup"),

    ("pg_restore: error: one of -d/--dbname and -f/--file must be specified",
     ["pg_restore"],
     "pg_restore: error: one of -d/--dbname and -f/--file must be specified"),

    ("pg_restore: options -a/--data-only and -s/--schema-only cannot be used together",
     ["pg_restore", "-s", "-a", "-f -"],
     "pg_restore: error: options -a/--data-only and -s/--schema-only cannot be used together"),

    ("pg_restore: options -d/--dbname and -f/--file cannot be used together",
     ["pg_restore", "-d", "xxx", "-f", "xxx"],
     "pg_restore: error: options -d/--dbname and -f/--file cannot be used together"),

    ("pg_dump: options -c/--clean and -a/--data-only cannot be used together",
     ["pg_dump", "-c", "-a"],
     "pg_dump: error: options -c/--clean and -a/--data-only cannot be used together"),

    ("pg_dumpall: options -c/--clean and -a/--data-only cannot be used together",
     ["pg_dumpall", "-c", "-a"],
     "pg_dumpall: error: options -c/--clean and -a/--data-only cannot be used together"),

    ("pg_restore: options -c/--clean and -a/--data-only cannot be used together",
     ["pg_restore", "-c", "-a", "-f -"],
     "pg_restore: error: options -c/--clean and -a/--data-only cannot be used together"),

    ("pg_dump: option --if-exists requires option -c/--clean",
     ["pg_dump", "--if-exists"],
     "pg_dump: error: option --if-exists requires option -c/--clean"),

    ("pg_dump: parallel backup only supported by the directory format",
     ["pg_dump", "-j3"],
     "pg_dump: error: parallel backup only supported by the directory format"),

    # Note the trailing whitespace for the value of --jobs, that is valid.
    ("pg_dump: -j/--jobs must be in range",
     ["pg_dump", "-j", "-1 "],
     "pg_dump: error: -j/--jobs must be in range"),

    ("pg_dump: invalid output format",
     ["pg_dump", "-F", "garbage"],
     "pg_dump: error: invalid output format"),

    ("pg_restore: -j/--jobs must be in range",
     ["pg_restore", "-j", "-1", "-f -"],
     "pg_restore: error: -j/--jobs must be in range"),

    ("pg_restore: cannot specify both --single-transaction and multiple jobs",
     ["pg_restore", "--single-transaction", "-j3", "-f -"],
     "pg_restore: error: cannot specify both --single-transaction and multiple jobs"),

    ("pg_dump: invalid --compress",
     ["pg_dump", "--compress", "garbage"],
     "pg_dump: error: unrecognized compression algorithm"),

    ("pg_dump: invalid compression specification: compression algorithm "
     '"none" does not accept a compression level',
     ["pg_dump", "--compress", "none:1"],
     'pg_dump: error: invalid compression specification: compression algorithm '
     '"none" does not accept a compression level'),
]


@pytest.mark.parametrize(
    "name,argv,pattern", _CASES,
    ids=[c[0] for c in _CASES],
)
def test_option_errors(pg_bin, name, argv, pattern):
    pg_bin.command_fails_like(argv, re.escape(pattern), name)


# Cases gated on libz support.
_LIBZ_CASES = [
    ("pg_dump: invalid compression specification: must be in range",
     ["pg_dump", "-Z", "15"],
     'pg_dump: error: invalid compression specification: compression algorithm '
     '"gzip" expects a compression level between 1 and 9 (default at -1)'),

    ("pg_dump: compression is not supported by tar archive format",
     ["pg_dump", "--compress", "1", "--format", "tar"],
     "pg_dump: error: compression is not supported by tar archive format"),

    ("pg_dump: invalid compression specification: must be an integer",
     ["pg_dump", "-Z", "gzip:nonInt"],
     'pg_dump: error: invalid compression specification: unrecognized '
     'compression option: "nonInt"'),
]

# Cases used when libz is NOT available.
_NO_LIBZ_CASES = [
    # --jobs > 1 forces an error with tar format.
    ("pg_dump: warning: parallel backup not supported by tar format",
     ["pg_dump", "--format", "tar", "-j3"],
     "pg_dump: error: parallel backup only supported by the directory format"),

    ("pg_dump: invalid compression specification: must be an integer",
     ["pg_dump", "-Z", "gzip:nonInt", "--format", "tar", "-j2"],
     "pg_dump: error: invalid compression specification: unrecognized compression option"),
]


@pytest.mark.parametrize(
    "name,argv,pattern", _LIBZ_CASES,
    ids=[c[0] for c in _LIBZ_CASES],
)
def test_libz_option_errors(pg_bin, name, argv, pattern):
    if not _have_pg_config_define("#define HAVE_LIBZ 1"):
        pytest.skip("build does not have libz support")
    pg_bin.command_fails_like(argv, re.escape(pattern), name)


@pytest.mark.parametrize(
    "name,argv,pattern", _NO_LIBZ_CASES,
    ids=[c[0] for c in _NO_LIBZ_CASES],
)
def test_no_libz_option_errors(pg_bin, name, argv, pattern):
    if _have_pg_config_define("#define HAVE_LIBZ 1"):
        pytest.skip("build has libz support")
    pg_bin.command_fails_like(argv, re.escape(pattern), name)


# Remaining option-error cases (those after the libz-conditional block).
_MORE_CASES = [
    ("pg_dump: --extra-float-digits must be in range",
     ["pg_dump", "--extra-float-digits", "-16"],
     "pg_dump: error: --extra-float-digits must be in range"),

    ("pg_dump: --rows-per-insert must be in range",
     ["pg_dump", "--rows-per-insert", "0"],
     "pg_dump: error: --rows-per-insert must be in range"),

    ("pg_restore: option --if-exists requires option -c/--clean",
     ["pg_restore", "--if-exists", "-f -"],
     "pg_restore: error: option --if-exists requires option -c/--clean"),

    ("pg_restore: unrecognized archive format",
     ["pg_restore", "-f -", "-F", "garbage"],
     'pg_restore: error: unrecognized archive format "garbage";'),

    ("pg_restore: empty archive format",
     ["pg_restore", "-f -", "-F", ""],
     'pg_restore: error: unrecognized archive format "";'),

    ("pg_dump: --on-conflict-do-nothing requires --inserts, --rows-per-insert, --column-inserts",
     ["pg_dump", "--on-conflict-do-nothing"],
     "pg_dump: error: option --on-conflict-do-nothing requires option "
     "--inserts, --rows-per-insert, or --column-inserts"),

    # pg_dumpall command-line argument checks
    ("pg_dumpall: options -g/--globals-only and -r/--roles-only cannot be used together",
     ["pg_dumpall", "-g", "-r"],
     "pg_dumpall: error: options -g/--globals-only and -r/--roles-only cannot be used together"),

    ("pg_dumpall: options -g/--globals-only and -t/--tablespaces-only cannot be used together",
     ["pg_dumpall", "-g", "-t"],
     "pg_dumpall: error: options -g/--globals-only and -t/--tablespaces-only cannot be used together"),

    ("pg_dumpall: options -r/--roles-only and -t/--tablespaces-only cannot be used together",
     ["pg_dumpall", "-r", "-t"],
     "pg_dumpall: error: options -r/--roles-only and -t/--tablespaces-only cannot be used together"),

    ("pg_dumpall: option --if-exists requires option -c/--clean",
     ["pg_dumpall", "--if-exists"],
     "pg_dumpall: error: option --if-exists requires option -c/--clean"),

    ("pg_restore: options -C/--create and -1/--single-transaction cannot be used together",
     ["pg_restore", "-C", "-1", "-f -"],
     "pg_restore: error: options -C/--create and -1/--single-transaction cannot be used together"),

    # also fails for -r and -t, but it seems pointless to add more tests for those.
    ("pg_dumpall: options --exclude-database and -g/--globals-only cannot be used together",
     ["pg_dumpall", "--exclude-database=foo", "--globals-only"],
     "pg_dumpall: error: options --exclude-database and -g/--globals-only cannot be used together"),

    ("pg_dumpall: options -a/--data-only and --no-data cannot be used together",
     ["pg_dumpall", "-a", "--no-data"],
     "pg_dumpall: error: options -a/--data-only and --no-data cannot be used together"),

    ("pg_dumpall: options -s/--schema-only and --no-schema cannot be used together",
     ["pg_dumpall", "-s", "--no-schema"],
     "pg_dumpall: error: options -s/--schema-only and --no-schema cannot be used together"),

    ("pg_dumpall: options --statistics-only and --no-statistics cannot be used together",
     ["pg_dumpall", "--statistics-only", "--no-statistics"],
     "pg_dumpall: error: options --statistics-only and --no-statistics cannot be used together"),

    ("pg_dumpall: options --statistics and --no-statistics cannot be used together",
     ["pg_dumpall", "--statistics", "--no-statistics"],
     "pg_dumpall: error: options --statistics and --no-statistics cannot be used together"),

    ("pg_dumpall: options --statistics and -t/--tablespaces-only cannot be used together",
     ["pg_dumpall", "--statistics", "--tablespaces-only"],
     "pg_dumpall: error: options --statistics and -t/--tablespaces-only cannot be used together"),

    ("pg_dumpall: unrecognized output format",
     ["pg_dumpall", "--format", "x"],
     'pg_dumpall: error: unrecognized output format "x";'),

    ("pg_dumpall: --restrict-key can only be used with plain dump format",
     ["pg_dumpall", "--format", "d", "--restrict-key=uu", "-f dumpfile"],
     "pg_dumpall: error: option --restrict-key can only be used with --format=plain"),

    ("pg_dumpall: --clean and -g/--globals-only cannot be used together in non-text dump",
     ["pg_dumpall", "--format", "d", "--globals-only", "--clean", "-f", "dumpfile"],
     "pg_dumpall: error: options --clean and -g/--globals-only cannot be used together "
     "in non-text dump"),

    ("pg_dumpall: non-plain format requires --file option",
     ["pg_dumpall", "--format", "d"],
     "pg_dumpall: error: option -F/--format=d|c|t requires option -f/--file"),

    ("pg_restore: options --exclude-database and -g/--globals-only cannot be used together",
     ["pg_restore", "--exclude-database=foo", "--globals-only", "-d", "xxx"],
     "pg_restore: error: options --exclude-database and -g/--globals-only cannot be used together"),

    ("pg_restore: error: options -a/--data-only and -g/--globals-only cannot be used together",
     ["pg_restore", "--data-only", "--globals-only", "-d", "xxx"],
     "pg_restore: error: options -a/--data-only and -g/--globals-only cannot be used together"),

    ("pg_restore: error: options -g/--globals-only and -s/--schema-only cannot be used together",
     ["pg_restore", "--schema-only", "--globals-only", "-d", "xxx"],
     "pg_restore: error: options -g/--globals-only and -s/--schema-only cannot be used together"),

    ("pg_restore: error: options -g/--globals-only and --statistics-only cannot be used together",
     ["pg_restore", "--statistics-only", "--globals-only", "-d", "xxx"],
     "pg_restore: error: options -g/--globals-only and --statistics-only cannot be used together"),

    ("When option --exclude-database is used in pg_restore with dump of pg_dump",
     ["pg_restore", "--exclude-database=foo", "-d", "xxx", "dumpdir"],
     "pg_restore: error: option --exclude-database can be used only when restoring "
     "an archive created by pg_dumpall"),

    ("When option --globals-only is used in pg_restore with the dump of pg_dump",
     ["pg_restore", "--globals-only", "-d", "xxx", "dumpdir"],
     "pg_restore: error: option -g/--globals-only can be used only when restoring "
     "an archive created by pg_dumpall"),

    ("options --no-globals and --globals-only cannot be used together",
     ["pg_restore", "--globals-only", "--no-globals", "-d", "xxx", "dumpdir"],
     "pg_restore: error: options -g/--globals-only and --no-globals cannot be used together"),
]


@pytest.mark.parametrize(
    "name,argv,pattern", _MORE_CASES,
    ids=[c[0] for c in _MORE_CASES],
)
def test_more_option_errors(pg_bin, name, argv, pattern):
    pg_bin.command_fails_like(argv, re.escape(pattern), name)
