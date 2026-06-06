# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test all ranges of valid UTF-8 codepoints under SASLprep."""

import os
import re

import pytest


def test_saslprep_ranges(create_pg):
    # Test all ranges of valid UTF-8 codepoints under SASLprep.
    #
    # This test is expensive and so is only enabled when PG_TEST_EXTRA
    # requests it.
    extra = os.environ.get("PG_TEST_EXTRA", "")
    if not re.search(r"\bsaslprep\b", extra):
        pytest.skip("test saslprep not enabled in PG_TEST_EXTRA")

    # Initialize node
    node = create_pg("main")
    node.safe_sql("CREATE EXTENSION test_saslprep;")

    # Among all the valid UTF-8 codepoint ranges, our implementation of
    # SASLprep should never return an empty password if the operation is
    # considered a success.
    result = node.safe_sql(
        "SELECT * FROM test_saslprep_ranges()\n"
        "  WHERE status = 'SUCCESS' AND res IN (NULL, '')\n"
    )

    assert result == "", "valid codepoints returning an empty password"
