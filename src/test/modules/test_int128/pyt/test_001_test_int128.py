# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test the 128-bit integer arithmetic code in int128.h."""

import pytest


def test_001_test_int128(pg_bin):
    # Test 128-bit integer arithmetic code in int128.h

    # Run the test program with 1M iterations
    exe = "test_int128"
    size = 1_000_000

    print(f"# testing executable {exe}")

    res = pg_bin.result([exe, str(size)])

    if "skipping tests" in res.stdout:
        pytest.skip("no native int128 type")

    assert res.stdout == "", "test_int128: no stdout"
    assert res.stderr == "", "test_int128: no stderr"
