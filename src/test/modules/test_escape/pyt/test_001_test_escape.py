# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test the string escaping functions.

Runs the test_escape C program against a running node and maps its
TAP-style stdout into pytest assertions.
"""

import re


def test_001_test_escape(create_pg, pg_bin):
    node = create_pg("node")

    node.safe_sql(
        'CREATE DATABASE db_sql_ascii ENCODING "sql_ascii" TEMPLATE template0;'
    )

    conninfo = node.connstr("db_sql_ascii")
    cmd = ["test_escape", "--conninfo", conninfo]

    # There currently is no good other way to transport test results from a C
    # program that requires just the node being set-up...
    res = pg_bin.result(cmd)

    assert res.returncode == 0, "test_escape returns 0"
    assert res.stderr == "", "test_escape stderr is empty"

    failures = []
    for line in res.stdout.split("\n"):
        if not line:
            continue
        m = re.match(r"^ok \d+ ?(.*)", line)
        if m:
            print(f"# ok {m.group(1)}")
            continue
        m = re.match(r"^not ok \d+ ?(.*)", line)
        if m:
            failures.append(m.group(1))
            continue
        m = re.match(r"^# ?(.*)", line)
        if m:
            print(f"# {m.group(1)}")
            continue
        if re.match(r"^\d+\.\.\d+$", line):
            continue
        raise AssertionError(f"no unmapped lines, got {line}")

    assert not failures, "test_escape subtests failed:\n" + "\n".join(failures)
