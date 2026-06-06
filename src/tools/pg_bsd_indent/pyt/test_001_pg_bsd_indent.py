# Copyright (c) 2017-2026, PostgreSQL Global Development Group

"""Run pg_bsd_indent over a set of pre-fab test cases and check its output.

Runs the build-dir program ``pg_bsd_indent`` over a set of pre-fab test cases
(taken from FreeBSD upstream) and compares its output against the expected
``*.0.stdout`` files.
"""

import glob
import os
import subprocess

import pytest

# The input source files (*.0), expected outputs (*.0.stdout), profiles (*.pro)
# and type lists (*.list) all live in the module's tests/ directory, a sibling
# of pyt/.
TESTS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "tests")

# Test basenames: every *.0 input file in tests/.
TEST_CASES = sorted(
    os.path.basename(p)[:-2] for p in glob.glob(os.path.join(TESTS_DIR, "*.0"))
)


def test_version(pg_bin):
    """pg_bsd_indent knows --version but not much else."""
    pg_bin.program_version_ok("pg_bsd_indent")


@pytest.mark.parametrize("test", TEST_CASES)
def test_001_pg_bsd_indent(pg_bin, test, tmp_path):
    test_src = os.path.join(TESTS_DIR, f"{test}.0")
    # Write the indented output under tmp_path, not into the source tree.
    out_file = str(tmp_path / f"{test}.out")
    pro_file = os.path.join(TESTS_DIR, f"{test}.pro")

    # Run pg_bsd_indent on the pre-fab test case.  We run with cwd set to the
    # tests directory so that *.pro files can reference *.list files by their
    # bare name (e.g. types_from_file.pro uses -Utypes_from_file.list).  We
    # always pass -P<test>.pro even when no such file exists; pg_bsd_indent
    # tolerates a missing profile.
    saved_cwd = os.getcwd()
    os.chdir(TESTS_DIR)
    try:
        pg_bin.command_ok(
            ["pg_bsd_indent", test_src, out_file, f"-P{pro_file}"],
            f"pg_bsd_indent succeeds on {test}",
        )
    finally:
        os.chdir(saved_cwd)

    # Check the result matches the expected output.
    with open(f"{test_src}.stdout", encoding="utf-8") as f:
        expected = f.read()
    with open(out_file, encoding="utf-8") as f:
        actual = f.read()

    if expected != actual:
        diff = subprocess.run(
            ["diff", "-U3", f"{test_src}.stdout", out_file],
            stdout=subprocess.PIPE,
            text=True,
            check=False,
        ).stdout
        pytest.fail(f"pg_bsd_indent output does not match for {test}\n{diff}")
