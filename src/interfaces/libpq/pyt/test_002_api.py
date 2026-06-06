# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Test PQsslAttribute(NULL, "library") via the libpq_testclient helper."""

import os


def test_002_api(pg_bin):
    # Test PQsslAttribute(NULL, "library")
    res = pg_bin.result(["libpq_testclient", "--ssl"])

    if os.environ.get("with_ssl") == "openssl":
        assert res.stdout.strip() == "OpenSSL", (
            'PQsslAttribute(NULL, "library") returns "OpenSSL"'
        )
    else:
        assert res.stderr.strip() == "SSL is not enabled", (
            'PQsslAttribute(NULL, "library") returns NULL'
        )
