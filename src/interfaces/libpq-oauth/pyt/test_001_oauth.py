# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Tests for libpq OAuth support.

Defer entirely to the ``oauth_tests`` executable, which lives in the build
directory (resolved via PATH) and emits its own TAP on stdout.  We do not parse
that TAP; we just run the program, echo its stdout/stderr, and require a zero
exit status.

``oauth_tests`` only exists when libpq is built with libcurl/OAuth support, so
if the program cannot be found we skip rather than fail.
"""

import pytest


def test_001_oauth(pg_bin):
    try:
        res = pg_bin.result(["oauth_tests"])
    except FileNotFoundError:
        pytest.skip("oauth_tests not built (no libcurl/OAuth support)")

    # Route the executable's output through pytest's capture so our logging
    # infrastructure can handle it.
    if res.stdout:
        print(res.stdout)
    if res.stderr:
        print(res.stderr)

    assert res.returncode == 0, (
        f"oauth_tests returned {res.returncode}\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
