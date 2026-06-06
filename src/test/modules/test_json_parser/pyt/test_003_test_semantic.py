# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test the incremental JSON parser with semantic routines.

Run the incremental JSON parser with semantic routines, and compare the
output with the expected output.
"""

import os

import pytest

# The module source directory is the parent of this pyt/ directory, where the
# test data files (tiny.json, tiny.out) live.
_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEST_FILE = os.path.join(_MODULE_DIR, "tiny.json")
_TEST_OUT = os.path.join(_MODULE_DIR, "tiny.out")

_EXES = (
    ("test_json_parser_incremental",),
    ("test_json_parser_incremental", "-o"),
    ("test_json_parser_incremental_shlib",),
    ("test_json_parser_incremental_shlib", "-o"),
)


@pytest.mark.parametrize("exe", _EXES, ids=[" ".join(e) for e in _EXES])
def test_003_test_semantic(pg_bin, exe):
    print(f"# testing executable {' '.join(exe)}")

    res = pg_bin.result([*exe, "-s", _TEST_FILE])

    # Chomp a single trailing newline off both streams.
    stdout = res.stdout[:-1] if res.stdout.endswith("\n") else res.stdout
    stderr = res.stderr[:-1] if res.stderr.endswith("\n") else res.stderr

    assert stderr == "", "no error output"

    with open(_TEST_OUT, encoding="utf-8") as f:
        expected = f.read()

    # Append a newline to the chomped stdout before diffing.
    assert stdout + "\n" == expected, "no output diff"
