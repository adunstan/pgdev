# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test the libpq pipeline mode via the libpq_pipeline client program.

Runs the build-dir ``libpq_pipeline`` client program against a running server
for each of its self-described subcommands, and for a subset compares the libpq
trace it emits against the expected ``traces/*.trace`` file shipped with the
module.
"""

import os

import pytest

NUMROWS = 700

# Tests for which libpq_pipeline can emit a trace file we compare against the
# expected output in the module source traces/ directory.
CMPTRACE = {
    "simple_pipeline",
    "nosync",
    "multi_pipelines",
    "prepared",
    "singlerow",
    "pipeline_abort",
    "pipeline_idle",
    "transaction",
    "disallowed_in_pipeline",
}

# traces/ lives in the module source directory, one level up from pyt/.
TRACES_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "traces")


def _list_tests(pg_bin):
    """Return the list of subcommands the libpq_pipeline binary reports."""
    res = pg_bin.result(["libpq_pipeline", "tests"])
    assert res.stderr == "", f"oops: {res.stderr}"
    return res.stdout.split()


def test_001_libpq_pipeline(pg_bin, pg, tmp_path):
    tests = _list_tests(pg_bin)

    tracedir = tmp_path / "traces"
    tracedir.mkdir()

    for testname in tests:
        extraargs = ["-r", str(NUMROWS)]
        cmptrace = testname in CMPTRACE

        traceout = str(tracedir / f"{testname}.trace")
        if cmptrace:
            extraargs += ["-t", traceout]

        # Execute the test using the latest protocol version.
        pg_bin.command_ok(
            [
                "libpq_pipeline",
                *extraargs,
                testname,
                pg.connstr("postgres") + " max_protocol_version=latest",
            ],
            f"libpq_pipeline {testname}",
        )

        # Compare the trace, if requested.
        if cmptrace:
            expected_path = os.path.join(TRACES_DIR, f"{testname}.trace")
            with open(expected_path, encoding="utf-8") as fh:
                expected = fh.read()
            with open(traceout, encoding="utf-8") as fh:
                result = fh.read()
            assert result == expected, f"{testname} trace match"

    # There were changes to query cancellation in protocol version 3.2, so
    # test separately that it still works the old protocol version too.
    pg_bin.command_ok(
        [
            "libpq_pipeline",
            "cancel",
            pg.connstr("postgres") + " max_protocol_version=3.0",
        ],
        "libpq_pipeline cancel with protocol 3.0",
    )

    pg.stop("fast")
