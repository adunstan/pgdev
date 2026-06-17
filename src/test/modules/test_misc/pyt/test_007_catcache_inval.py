# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Test recursive catalog cache invalidation.

That is, invalidation while a catalog cache entry is being built.
"""

import os
import random
import string

import pytest

from libpq.constants import PGRES_TUPLES_OK


def rand_str(length):
    """Return a random alphanumeric string of the given length."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def test_007_catcache_inval(create_pg):
    # Skip unless the build has injection points enabled.
    if os.environ.get("enable_injection_points", "no") != "yes":
        pytest.skip("Injection points not supported by this build")

    node = create_pg("node", start=False)
    node.start()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    if (
        node.safe_sql(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name = 'injection_points'"
        )
        == "0"
    ):
        pytest.skip("Extension injection_points not installed")

    node.safe_sql("CREATE EXTENSION injection_points;")

    # Create a function with a large body, so that it is toasted.
    longtext = rand_str(10000)
    node.safe_sql(
        "CREATE FUNCTION foofunc(dummy integer) RETURNS integer AS $$ "
        f"SELECT 1; /* {longtext} */ $$ LANGUAGE SQL"
    )

    psql_session = node.connect()
    psql_session2 = node.connect()
    try:
        # Set injection point in the session, to pause while populating the
        # catcache list.
        psql_session.query_safe("SELECT injection_points_set_local();")
        psql_session.query_safe(
            "SELECT injection_points_attach("
            "'catcache-list-miss-systable-scan-started', 'wait');"
        )

        # This pauses on the injection point while populating catcache list
        # for functions with name "foofunc".
        psql_session.do_async("SELECT foofunc(1);")

        # Wait until that backend is actually paused at the injection point
        # before invalidating and waking it.  do_async returns as soon as the
        # query is sent, so without this explicit wait the wakeup below can run
        # before the point is reached ("could not find injection point ... to
        # wake up").  The TAP test instead relies on the latency of spawning a
        # psql for the CREATE FUNCTION, which the in-process layer does not have.
        node.wait_for_event(
            "client backend", "catcache-list-miss-systable-scan-started"
        )

        # While the first session is building the catcache list, create a new
        # function that overloads the same name.  This sends a catcache
        # invalidation.
        node.safe_sql(
            "CREATE FUNCTION foofunc() RETURNS integer AS $$ "
            "SELECT 123 $$ LANGUAGE SQL"
        )

        # Continue the paused session.  It will continue to construct the
        # catcache list, and will accept invalidations while doing that.
        #
        # (The fact that the first function has a large body is crucial,
        # because the cache invalidation is accepted during detoasting.  If
        # the body is not toasted, the invalidation is processed after
        # building the catcache list, which avoids the recursion that we are
        # trying to exercise here.)
        #
        # The "SELECT foofunc(1)" query will now finish.
        psql_session2.query_safe(
            "SELECT injection_points_wakeup("
            "'catcache-list-miss-systable-scan-started');"
        )
        psql_session2.query_safe(
            "SELECT injection_points_detach("
            "'catcache-list-miss-systable-scan-started');"
        )

        # Test that the new function is visible to the session.
        psql_session.wait_for_completion()
        res = psql_session.query("SELECT foofunc();")

        assert res.status == PGRES_TUPLES_OK, "got TUPLES_OK"
    finally:
        psql_session.close()
        psql_session2.close()
