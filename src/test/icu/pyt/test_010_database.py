# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Tests for ICU-based database collations."""

import pytest


def test_010_database(create_pg):
    node1 = create_pg("node1")

    # This test requires a build configured --with-icu.  Ask the server
    # whether any ICU collation provider is available.
    if (
        node1.safe_sql("SELECT count(*) > 0 FROM pg_collation WHERE collprovider = 'i'")
        != "t"
    ):
        pytest.skip("ICU not supported by this build")

    # CREATE DATABASE cannot run inside a transaction block, so it is its own
    # statement.
    node1.safe_sql(
        "CREATE DATABASE dbicu LOCALE_PROVIDER icu LOCALE 'C' "
        "ICU_LOCALE 'en@colCaseFirst=upper' ENCODING 'UTF8' TEMPLATE template0"
    )

    node1.safe_sql(
        """
CREATE COLLATION upperfirst (provider = icu, locale = 'en@colCaseFirst=upper');
CREATE TABLE icu (def text, en text COLLATE "en-x-icu", upfirst text COLLATE upperfirst);
INSERT INTO icu VALUES ('a', 'a', 'a'), ('b', 'b', 'b'), ('A', 'A', 'A'), ('B', 'B', 'B');
""",
        dbname="dbicu",
    )

    assert (
        node1.safe_sql("SELECT icu_unicode_version() IS NOT NULL", dbname="dbicu")
        == "t"
    ), "ICU unicode version defined"

    assert (
        node1.safe_sql("SELECT def FROM icu ORDER BY def", dbname="dbicu")
        == "A\na\nB\nb"
    ), "sort by database default locale"

    assert (
        node1.safe_sql(
            'SELECT def FROM icu ORDER BY def COLLATE "en-x-icu"', dbname="dbicu"
        )
        == "a\nA\nb\nB"
    ), "sort by explicit collation standard"

    assert (
        node1.safe_sql(
            "SELECT def FROM icu ORDER BY en COLLATE upperfirst", dbname="dbicu"
        )
        == "A\na\nB\nb"
    ), "sort by explicit collation upper first"

    # Test that LOCALE='C' works for ICU
    res = node1.sql(
        "CREATE DATABASE dbicu1 LOCALE_PROVIDER icu LOCALE 'C' "
        "TEMPLATE template0 ENCODING UTF8"
    )
    assert res.error_message is None, "C locale works for ICU"

    # Test that LOCALE works for ICU locales if LC_COLLATE and LC_CTYPE
    # are specified
    res = node1.sql(
        "CREATE DATABASE dbicu2 LOCALE_PROVIDER icu LOCALE '@colStrength=primary' "
        "LC_COLLATE='C' LC_CTYPE='C' TEMPLATE template0 ENCODING UTF8"
    )
    assert (
        res.error_message is None
    ), "LOCALE works for ICU locales if LC_COLLATE and LC_CTYPE are specified"

    res = node1.sql(
        "CREATE DATABASE dbicu3 LOCALE_PROVIDER builtin LOCALE 'C' TEMPLATE dbicu"
    )
    assert (
        res.error_message is not None
    ), "locale provider must match template: exit code not 0"
    assert (
        "new locale provider (builtin) does not match locale provider "
        "of the template database (icu)" in res.error_message
    ), "locale provider must match template: error message"
