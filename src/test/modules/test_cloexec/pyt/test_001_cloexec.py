# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test O_CLOEXEC flag handling on Windows.

This test verifies that file handles opened with O_CLOEXEC are not
inherited by child processes, while handles opened without O_CLOEXEC
are inherited.
"""

import os
import re
import sys

import pytest


def test_cloexec(pg_bin):
    if sys.platform != "win32":
        pytest.skip("test is Windows-specific")

    # Locate the test program on PATH, falling back to the cwd.
    test_prog = None
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "test_cloexec.exe")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            test_prog = candidate
            break

    if not test_prog:
        test_prog = os.path.join(".", "test_cloexec.exe")

    if not os.path.isfile(test_prog):
        pytest.fail(f"test program not found: {test_prog}")

    print(f"# Using test program: {test_prog}")

    res = pg_bin.result([test_prog])

    print("# Test program output:")
    if res.stdout:
        print(res.stdout)
    if res.stderr:
        print("# Test program stderr:")
        print(res.stderr)

    assert res.returncode == 0 and re.search(
        r"SUCCESS.*O_CLOEXEC behavior verified", res.stdout, re.S
    ), "O_CLOEXEC prevents handle inheritance"
