# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests pg_dump / pg_restore compression handling.

This test uses essentially the same matrix structure as test_002_pg_dump, but
is specialized to compression concerns: it dumps/restores across the gzip,
lz4 and zstd methods in the plain, custom and directory formats (with various
compression levels and the compress-spec syntax), and checks that the dump
output round-trips correctly.

pg_dump / pg_restore are the binaries under test and are run as subprocesses
through the node's command_ok / command_like helpers.  The seed SQL the test
itself runs is executed in-process via safe_sql.

Each method's cases are gated on whether the corresponding compression
library was built in (HAVE_LIBZ / USE_LZ4 / USE_ZSTD in pg_config.h).  Where a
"run" needs an external (de)compression program (gzip/lz4/zstd) for
manually-compressed TOC files or to decompress a plain dump, the program is
located via the GZIP_PROGRAM / LZ4 / ZSTD environment variables (set by the
build system) falling back to PATH; if it cannot be found, the rest of that
run is skipped.
"""

import glob as _glob
import os
import re
import shutil
import subprocess

from pypg.util import slurp_file


def _have_pg_config_define(define):
    """Return True if the installed pg_config.h contains the given #define."""
    try:
        out = subprocess.run(
            ["pg_config", "--includedir"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    header = os.path.join(out, "pg_config.h")
    try:
        with open(header, encoding="utf-8", errors="replace") as fh:
            return define in fh.read()
    except OSError:
        return False


def _find_program(env_var, default_name):
    """Locate an external (de)compression program.

    The build system sets GZIP_PROGRAM / LZ4 / ZSTD to the program's full path
    (or an empty string when not found); honor that first.
    When the variable is unset (e.g. running pytest standalone), fall back to
    searching PATH so the test stays useful.  Returns None when unavailable.
    """
    val = os.environ.get(env_var)
    if val is not None:
        return val or None
    return shutil.which(default_name)


# The regexps below run in verbose mode.  re.VERBOSE (X) lets us keep the
# whitespace layout; re.MULTILINE (M) anchors ^/$ per line; re.DOTALL (S) is
# added where a pattern needs '.' to span newlines.
_XM = re.VERBOSE | re.MULTILINE
_XMS = re.VERBOSE | re.MULTILINE | re.DOTALL


def _pgdump_runs(tempdir):
    """Definition of the pg_dump runs to make.

    Each run has a dump_cmd and a test_key (reuse another run's like/unlike
    sets), and may have: a compile_option gating it on a compression library;
    a restore_cmd; a compress_cmd (an external program + args used either to
    decompress a plain dump into the .sql the matrix checks, or to manually
    compress directory-format TOC files for restore coverage); glob_patterns
    that must exist after dumping; and a command_like (run a command and check
    its stdout).  Commands are argv lists; cmd[0] is resolved in the node's
    bindir.
    """
    return {
        "compression_none_custom": {
            "test_key": "compression",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "custom",
                "--compress",
                "none",
                "--file",
                f"{tempdir}/compression_none_custom.dump",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file",
                f"{tempdir}/compression_none_custom.sql",
                "--statistics",
                f"{tempdir}/compression_none_custom.dump",
            ],
        },
        "compression_gzip_custom": {
            "test_key": "compression",
            "compile_option": "gzip",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "custom",
                "--compress",
                "1",
                "--file",
                f"{tempdir}/compression_gzip_custom.dump",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file",
                f"{tempdir}/compression_gzip_custom.sql",
                "--statistics",
                f"{tempdir}/compression_gzip_custom.dump",
            ],
            "command_like": {
                "command": [
                    "pg_restore",
                    "--list",
                    f"{tempdir}/compression_gzip_custom.dump",
                ],
                "expected": re.compile(r"Compression: gzip"),
                "name": "data content is gzip-compressed",
            },
        },
        "compression_gzip_dir": {
            "test_key": "compression",
            "compile_option": "gzip",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--jobs",
                "2",
                "--format",
                "directory",
                "--compress",
                "gzip:1",
                "--file",
                f"{tempdir}/compression_gzip_dir",
                "--statistics",
                "postgres",
            ],
            # Give coverage for manually-compressed TOC files during restore.
            "compress_cmd": {
                "program": _find_program("GZIP_PROGRAM", "gzip"),
                "args": [
                    "-f",
                    f"{tempdir}/compression_gzip_dir/toc.dat",
                    f"{tempdir}/compression_gzip_dir/blobs_*.toc",
                ],
            },
            # Verify that TOC and data files were compressed.
            "glob_patterns": [
                f"{tempdir}/compression_gzip_dir/toc.dat.gz",
                f"{tempdir}/compression_gzip_dir/*.dat.gz",
            ],
            "restore_cmd": [
                "pg_restore",
                "--jobs",
                "2",
                "--file",
                f"{tempdir}/compression_gzip_dir.sql",
                "--statistics",
                f"{tempdir}/compression_gzip_dir",
            ],
        },
        "compression_gzip_plain": {
            "test_key": "compression",
            "compile_option": "gzip",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "plain",
                "--compress",
                "1",
                "--file",
                f"{tempdir}/compression_gzip_plain.sql.gz",
                "--statistics",
                "postgres",
            ],
            # Decompress the generated file to run through the tests.
            "compress_cmd": {
                "program": _find_program("GZIP_PROGRAM", "gzip"),
                "args": ["-d", f"{tempdir}/compression_gzip_plain.sql.gz"],
            },
        },
        "compression_lz4_custom": {
            "test_key": "compression",
            "compile_option": "lz4",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "custom",
                "--compress",
                "lz4",
                "--file",
                f"{tempdir}/compression_lz4_custom.dump",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file",
                f"{tempdir}/compression_lz4_custom.sql",
                "--statistics",
                f"{tempdir}/compression_lz4_custom.dump",
            ],
            "command_like": {
                "command": [
                    "pg_restore",
                    "--list",
                    f"{tempdir}/compression_lz4_custom.dump",
                ],
                "expected": re.compile(r"Compression: lz4"),
                "name": "data content is lz4 compressed",
            },
        },
        "compression_lz4_dir": {
            "test_key": "compression",
            "compile_option": "lz4",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--jobs",
                "2",
                "--format",
                "directory",
                "--compress",
                "lz4:1",
                "--file",
                f"{tempdir}/compression_lz4_dir",
                "--statistics",
                "postgres",
            ],
            # Give coverage for manually-compressed TOC files during restore.
            "compress_cmd": {
                "program": _find_program("LZ4", "lz4"),
                "args": [
                    "-z",
                    "-f",
                    "-m",
                    "--rm",
                    f"{tempdir}/compression_lz4_dir/toc.dat",
                    f"{tempdir}/compression_lz4_dir/blobs_*.toc",
                ],
            },
            # Verify that TOC and data files were compressed.
            "glob_patterns": [
                f"{tempdir}/compression_lz4_dir/toc.dat.lz4",
                f"{tempdir}/compression_lz4_dir/*.dat.lz4",
            ],
            "restore_cmd": [
                "pg_restore",
                "--jobs",
                "2",
                "--file",
                f"{tempdir}/compression_lz4_dir.sql",
                "--statistics",
                f"{tempdir}/compression_lz4_dir",
            ],
        },
        "compression_lz4_plain": {
            "test_key": "compression",
            "compile_option": "lz4",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "plain",
                "--compress",
                "lz4",
                "--file",
                f"{tempdir}/compression_lz4_plain.sql.lz4",
                "--statistics",
                "postgres",
            ],
            # Decompress the generated file to run through the tests.
            "compress_cmd": {
                "program": _find_program("LZ4", "lz4"),
                "args": [
                    "-d",
                    "-f",
                    f"{tempdir}/compression_lz4_plain.sql.lz4",
                    f"{tempdir}/compression_lz4_plain.sql",
                ],
            },
        },
        "compression_zstd_custom": {
            "test_key": "compression",
            "compile_option": "zstd",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "custom",
                "--compress",
                "zstd",
                "--file",
                f"{tempdir}/compression_zstd_custom.dump",
                "--statistics",
                "postgres",
            ],
            "restore_cmd": [
                "pg_restore",
                "--file",
                f"{tempdir}/compression_zstd_custom.sql",
                "--statistics",
                f"{tempdir}/compression_zstd_custom.dump",
            ],
            "command_like": {
                "command": [
                    "pg_restore",
                    "--list",
                    f"{tempdir}/compression_zstd_custom.dump",
                ],
                "expected": re.compile(r"Compression: zstd"),
                "name": "data content is zstd compressed",
            },
        },
        "compression_zstd_dir": {
            "test_key": "compression",
            "compile_option": "zstd",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--jobs",
                "2",
                "--format",
                "directory",
                "--compress",
                "zstd:1",
                "--file",
                f"{tempdir}/compression_zstd_dir",
                "--statistics",
                "postgres",
            ],
            # Give coverage for manually-compressed TOC files during restore.
            "compress_cmd": {
                "program": _find_program("ZSTD", "zstd"),
                "args": [
                    "-z",
                    "-f",
                    "--rm",
                    f"{tempdir}/compression_zstd_dir/toc.dat",
                    f"{tempdir}/compression_zstd_dir/blobs_*.toc",
                ],
            },
            # Verify that TOC and data files were compressed.
            "glob_patterns": [
                f"{tempdir}/compression_zstd_dir/toc.dat.zst",
                f"{tempdir}/compression_zstd_dir/*.dat.zst",
            ],
            "restore_cmd": [
                "pg_restore",
                "--jobs",
                "2",
                "--file",
                f"{tempdir}/compression_zstd_dir.sql",
                "--statistics",
                f"{tempdir}/compression_zstd_dir",
            ],
        },
        # Exercise long mode for test coverage.
        "compression_zstd_plain": {
            "test_key": "compression",
            "compile_option": "zstd",
            "dump_cmd": [
                "pg_dump",
                "--no-sync",
                "--format",
                "plain",
                "--compress",
                "zstd:long",
                "--file",
                f"{tempdir}/compression_zstd_plain.sql.zst",
                "--statistics",
                "postgres",
            ],
            # Decompress the generated file to run through the tests.
            "compress_cmd": {
                "program": _find_program("ZSTD", "zstd"),
                "args": [
                    "-d",
                    "-f",
                    f"{tempdir}/compression_zstd_plain.sql.zst",
                    "-o",
                    f"{tempdir}/compression_zstd_plain.sql",
                ],
            },
        },
    }


# Tests which are considered 'full' dumps by pg_dump (mirrors %full_runs).
FULL_RUNS = {"compression"}


def _tests():
    """Definition of the tests to run.

    Each entry: create_order/create_sql (seed SQL, run before any dump),
    regexp (compiled), like/unlike sets of test keys, and an optional
    compile_option gating the test on a compression library.  A run whose
    test_key is in 'like' (and not 'unlike') must match the regexp.
    """
    full = set(FULL_RUNS)
    return {
        "CREATE MATERIALIZED VIEW matview_compression_lz4": {
            "create_order": 20,
            "create_sql": "CREATE MATERIALIZED VIEW\n"
            "    matview_compression_lz4 (col2) AS\n"
            "    SELECT repeat('xyzzy', 10000);\n"
            "    ALTER MATERIALIZED VIEW matview_compression_lz4\n"
            "    ALTER COLUMN col2 SET COMPRESSION lz4;",
            "regexp": re.compile(
                r"^"
                r"CREATE\ MATERIALIZED\ VIEW\ public\.matview_compression_lz4\ AS"
                r"\n\s+SELECT\ repeat\('xyzzy'::text,\ 10000\)\ AS\ col2"
                r"\n\s+WITH\ NO\ DATA;"
                r".*"
                r"ALTER\ TABLE\ ONLY\ public\.matview_compression_lz4\ "
                r"ALTER\ COLUMN\ col2\ SET\ COMPRESSION\ lz4;\n",
                _XMS,
            ),
            "compile_option": "lz4",
            "like": set(full),
        },
        "CREATE TABLE test_compression_method": {
            "create_order": 110,
            "create_sql": "CREATE TABLE test_compression_method (\n"
            "    col1 text\n"
            ");",
            "regexp": re.compile(
                r"^"
                r"CREATE\ TABLE\ public\.test_compression_method\ \(\n"
                r"\s+col1\ text\n"
                r"\);",
                _XM,
            ),
            "like": set(full),
        },
        # Insert enough data to surpass DEFAULT_IO_BUFFER_SIZE during
        # (de)compression operations.  The data is the concatenation of the
        # decimal representations of 1..65536, which is exactly 316574 digits,
        # i.e. 31657 groups of ten digits followed by four more.
        "COPY test_compression_method": {
            "create_order": 111,
            "create_sql": "INSERT INTO test_compression_method (col1) "
            "SELECT string_agg(a::text, '') FROM generate_series(1,65536) a;",
            "regexp": re.compile(
                r"^"
                r"COPY\ public\.test_compression_method\ \(col1\)\ FROM\ stdin;"
                r"\n(?:(?:\d\d\d\d\d\d\d\d\d\d){31657}\d\d\d\d\n){1}\\\.\n",
                _XM,
            ),
            "like": set(full),
        },
        "CREATE TABLE test_compression": {
            "create_order": 3,
            "create_sql": "CREATE TABLE test_compression (\n"
            "    col1 int,\n"
            "    col2 text COMPRESSION lz4\n"
            ");",
            "regexp": re.compile(
                r"^"
                r"CREATE\ TABLE\ public\.test_compression\ \(\n"
                r"\s+col1\ integer,\n"
                r"\s+col2\ text\n"
                r"\);\n"
                r".*"
                r"ALTER\ TABLE\ ONLY\ public\.test_compression\ "
                r"ALTER\ COLUMN\ col2\ SET\ COMPRESSION\ lz4;\n",
                _XMS,
            ),
            "compile_option": "lz4",
            "like": set(full),
        },
        # Create a large object so we can test compression of blobs.toc.
        "LO create (using lo_from_bytea)": {
            "create_order": 50,
            "create_sql": "SELECT pg_catalog.lo_from_bytea(0, "
            "'\\x310a320a330a340a350a360a370a380a390a');",
            "regexp": re.compile(
                r"^SELECT pg_catalog\.lo_create\('\d+'\);", re.MULTILINE
            ),
            "like": set(full),
        },
        "LO load (using lo_from_bytea)": {
            "regexp": re.compile(
                r"^"
                r"SELECT\ pg_catalog\.lo_open\('\d+',\ \d+\);\n"
                r"SELECT\ pg_catalog\.lowrite\(0,\ "
                r"'\\x310a320a330a340a350a360a370a380a390a'\);\n"
                r"SELECT\ pg_catalog\.lo_close\(0\);",
                _XM,
            ),
            "like": set(full),
        },
    }


def _compile_option_supported(option, supports):
    """Return True if *option* (gzip/lz4/zstd) is built in, per *supports*."""
    if not option:
        return True
    return supports.get(option, False)


def _create_order_key(item):
    """Sort key implementing the create_order comparator.

    Tests with a create_order sort by it (ascending) and before tests without
    one; among tests without a create_order, order is irrelevant for the
    concatenated seed SQL but we keep it stable by name.
    """
    name, spec = item
    order = spec.get("create_order")
    return (0, order, name) if order is not None else (1, 0, name)


def test_pg_dump_compress(pg, tmp_path):
    node = pg
    tempdir = str(tmp_path)

    supports = {
        "gzip": _have_pg_config_define("#define HAVE_LIBZ 1"),
        "lz4": _have_pg_config_define("#define USE_LZ4 1"),
        "zstd": _have_pg_config_define("#define USE_ZSTD 1"),
    }

    pgdump_runs = _pgdump_runs(tempdir)
    tests = _tests()

    #########################################
    # Set up schemas, tables, etc, to be dumped.  Build up the combined create
    # statements in create_order (skipping ones needing an unsupported compile
    # option), then send them.
    create_sql = ""
    for _name, spec in sorted(tests.items(), key=_create_order_key):
        if not _compile_option_supported(spec.get("compile_option"), supports):
            continue
        if not spec.get("create_sql"):
            continue
        # Normalize command ending: strip trailing whitespace/newlines, add a
        # semicolon if missing, then two newlines.
        sql = spec["create_sql"].rstrip("\r\n")
        if not sql.endswith(";"):
            sql += ";"
        create_sql += sql + "\n\n"

    node.safe_sql(create_sql)

    #########################################
    # Run all runs (sorted by name).
    for run in sorted(pgdump_runs):
        spec = pgdump_runs[run]
        test_key = run

        # Skip runs that require an unsupported compile option.
        opt = spec.get("compile_option")
        if not _compile_option_supported(opt, supports):
            print(f"# {run}: skipped due to no {opt} support")
            continue

        node.command_ok(spec["dump_cmd"], f"{run}: pg_dump runs")

        if "compress_cmd" in spec:
            compress_cmd = spec["compress_cmd"]
            program = compress_cmd["program"]

            # Skip the rest of the test if the compression program is not
            # available (the build env var was empty / program not on PATH).
            if not program:
                print(f"# {run}: skipped, no compression program available")
                continue

            # Arguments may require globbing (e.g. blobs_*.toc).
            full_compress_cmd = [program]
            for arg in compress_cmd["args"]:
                matches = sorted(_glob.glob(arg))
                full_compress_cmd += matches if matches else [arg]

            res = subprocess.run(
                full_compress_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            assert res.returncode == 0, (
                f"{run}: compression commands\n"
                f"{' '.join(full_compress_cmd)}\n{res.stdout}"
            )

        for glob_pattern in spec.get("glob_patterns", []):
            matches = _glob.glob(glob_pattern)
            ok = len(matches) > 1 or (len(matches) == 1 and os.path.isfile(matches[0]))
            assert ok, f"{run}: glob check for {glob_pattern}"

        if "command_like" in spec:
            cl = spec["command_like"]
            node.command_like(cl["command"], cl["expected"], f"{run}: {cl['name']}")

        if "restore_cmd" in spec:
            node.command_ok(spec["restore_cmd"], f"{run}: pg_restore runs")

        if "test_key" in spec:
            test_key = spec["test_key"]

        output_file = slurp_file(os.path.join(tempdir, f"{run}.sql"))

        #########################################
        # Run all tests where this run is included as a 'like' or 'unlike'.
        for test_name in sorted(tests):
            tspec = tests[test_name]

            like = tspec.get("like")
            unlike = tspec.get("unlike", set())
            all_runs_flag = tspec.get("all_runs", False)

            # Either all_runs should be set or there must be a "like" list, to
            # keep the test self-documenting.
            assert (
                all_runs_flag or like is not None
            ), f'missing "like" in test "{test_name}"'
            like_set = like if like is not None else set()

            # Check for useless entries in "unlike": a run not listed in "like"
            # doesn't need excluding.
            assert not (
                test_key in unlike and test_key not in like_set
            ), f'useless "unlike" entry "{test_key}" in test "{test_name}"'

            # Skip tests that require an unsupported compile option.
            if not _compile_option_supported(tspec.get("compile_option"), supports):
                continue

            if (test_key in like_set or all_runs_flag) and test_key not in unlike:
                assert tspec["regexp"].search(output_file), (
                    f"{run}: should dump {test_name}\n"
                    f"Review {run} results in {tempdir}"
                )
            else:
                assert not tspec["regexp"].search(output_file), (
                    f"{run}: should not dump {test_name}\n"
                    f"Review {run} results in {tempdir}"
                )

    node.stop("fast")
