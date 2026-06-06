# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test the JSON parser performance tester.

Here we are just checking that the
performance tester can run, both with the standard parser and the incremental
parser. An actual performance test will run with thousands of iterations
instead of just one.
"""

import os


def test_004_test_parser_perf(pg_bin, tmp_path):
    exe = "test_json_parser_perf"

    # tiny.json lives in the module source dir, one level up from pyt/.
    test_file = os.path.join(os.path.dirname(__file__), "..", "tiny.json")
    with open(test_file, encoding="utf-8") as f:
        contents = f.read()

    # repeat the input json file 50 times in an array
    fname = str(tmp_path / "perf_input.json")
    with open(fname, "w", encoding="utf-8") as fh:
        fh.write("[" + contents + ("," + contents) * 49 + "]")

    # but only do one iteration

    res = pg_bin.result([exe, "1", fname])
    assert res.returncode == 0, "perf test runs with recursive descent parser"

    res = pg_bin.result([exe, "-i", "1", fname])
    assert res.returncode == 0, "perf test runs with table driven parser"
