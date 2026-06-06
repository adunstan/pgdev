# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test allocating shared memory after startup."""


def _attach_count(node):
    """Read the attach counter in a *fresh* backend.

    safe_sql() would run in its own psql process, so every read would land in
    a new backend and the shmem attach callback would fire anew.  The pytest
    framework's node.safe_sql() reuses one cached connection, so here we open a
    fresh connection per read to preserve those semantics.
    """
    sess = node.connect()
    try:
        return int(sess.query_oneval("SELECT get_test_shmem_attach_count();"))
    finally:
        sess.close()


def test_late_shmem_alloc(create_pg):
    #
    # Test allocating memory after startup, i.e. when the library is not
    # in shared_preload_libraries
    #
    # create_pg runs initdb; start it explicitly.
    node = create_pg("main", start=False)
    node.start()

    node.safe_sql("CREATE EXTENSION test_shmem;")

    # Check that the attach counter is incremented on a new connection
    attach_count1 = _attach_count(node)
    attach_count2 = _attach_count(node)
    assert attach_count2 > attach_count1, (
        "attach callback is called in each backend"
    )
    node.stop()

    #
    # Test that loading via shared_preload_libraries also works
    #
    node.append_conf("shared_preload_libraries = 'test_shmem'")
    node.start()

    # When loaded via shared_preload_libraries, the attach callback is
    # called or not, depending on whether this is an EXEC_BACKEND build.
    exec_backend = node.safe_sql("SHOW debug_exec_backend;") == "on"
    attach_count1 = _attach_count(node)
    attach_count2 = _attach_count(node)

    if exec_backend:
        assert attach_count2 > attach_count1, (
            "attach callback is called in each backend when loaded via "
            "shared_preload_libraries"
        )
    else:
        assert attach_count1 == 0 and attach_count2 == 0, (
            "attach callback is not called when loaded via "
            "shared_preload_libraries"
        )

    node.stop()
