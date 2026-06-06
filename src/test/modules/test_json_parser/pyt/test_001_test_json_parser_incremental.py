# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test the incremental (table-driven) json parser."""

import os
import re

import pytest

# tiny.json lives in the module source directory, one level up from pyt/.
TEST_FILE = os.path.join(os.path.dirname(__file__), os.pardir, "tiny.json")

EXES = [
    ["test_json_parser_incremental"],
    ["test_json_parser_incremental", "-o"],
    ["test_json_parser_incremental_shlib"],
    ["test_json_parser_incremental_shlib", "-o"],
]


@pytest.mark.parametrize("exe", EXES, ids=" ".join)
def test_001_test_json_parser_incremental(pg_bin, exe):
    print(f"# testing executable {' '.join(exe)}")

    # Test the usage error
    res = pg_bin.result([*exe, "-c", "10"])
    assert re.search(r"Usage:", res.stderr), "error message if not enough arguments"

    # Test that we get success for small chunk sizes from 64 down to 1.
    for size in range(64, 0, -1):
        res = pg_bin.result([*exe, "-c", str(size), TEST_FILE])

        assert re.search(r"SUCCESS", res.stdout), f"chunk size {size}: test succeeds"
        assert res.stderr == "", f"chunk size {size}: no error output"
