# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test multixid corner cases."""

import pytest


def test_001_multixact(create_pg):
    node = create_pg("main", start=False)
    node.append_conf(
        "shared_preload_libraries = 'test_slru,injection_points'")
    node.start()

    # Skip if injection points are not supported by this build.
    if node.safe_sql(
        "SELECT count(*) FROM pg_available_extensions "
        "WHERE name = 'injection_points'"
    ) == "0":
        pytest.skip("Injection points not supported by this build")

    node.safe_sql("CREATE EXTENSION injection_points")
    node.safe_sql("CREATE EXTENSION test_slru")

    # This test creates three multixacts. The middle one is never
    # WAL-logged or recorded on the offsets page, because we pause the
    # backend and crash the server before that. After restart, verify that
    # the other multixacts are readable, despite the middle one being
    # lost.

    # Create the first multixact
    bg_session = node.connect()
    multi1 = bg_session.query_oneval("SELECT test_create_multixact();")

    # Assign the middle multixact. Use an injection point to prevent it
    # from being fully recorded.
    node.safe_sql(
        "SELECT injection_points_attach("
        "'multixact-create-from-members','wait');")

    # Start the second multixact creation asynchronously - it will block at
    # the injection point
    bg_session.do_async("SELECT test_create_multixact();")

    node.wait_for_event("client backend", "multixact-create-from-members")
    node.safe_sql(
        "SELECT injection_points_detach('multixact-create-from-members')")

    # Create the third multixid
    multi2 = node.safe_sql("SELECT test_create_multixact();")

    # All set and done, it's time for hard restart. The background session
    # will be terminated by the crash.
    node.stop("immediate")
    node.start()

    # Verify that the recorded multixids are readable
    assert node.safe_sql(f"SELECT test_read_multixact('{multi1}');") == "", \
        "first recorded multi is readable"

    assert node.safe_sql(f"SELECT test_read_multixact('{multi2}');") == "", \
        "second recorded multi is readable"
