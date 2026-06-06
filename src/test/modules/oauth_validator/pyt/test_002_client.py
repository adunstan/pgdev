# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Exercises the API for custom OAuth client flows.

The program under test is the ``oauth_hook_client`` test driver (a C binary
built in the module's build dir), which exercises libpq's OAuth client hook
callbacks/flows.  It is never psql: each case runs the binary via the node's
``pg_bin`` helper and asserts on its exit/stdout/stderr plus the postmaster
log.  These tests don't use the builtin flow and there's no authorization
server running, so the issuer is a deliberately invalid IP address -- if some
cascade of errors causes the client to actually attempt a connection to it,
we'll fail noisily.
"""

import os
import re

import pytest

# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

if "oauth" not in os.environ.get("PG_TEST_EXTRA", "").split():
    pytest.skip(
        "Potentially unsafe test oauth not enabled in PG_TEST_EXTRA",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Cluster setup
# ---------------------------------------------------------------------------

ISSUER = "https://256.256.256.256"
SCOPE = "openid postgres"
USER = "test"


@pytest.fixture
def node(create_pg):
    """A started node configured for the OAuth validator.

    Yields a tuple ``(node, common_connstr)`` where *common_connstr* is the
    base connection string used by every case (it is updated per-case in the
    test body, so a fresh copy is rebuilt as needed there).
    """
    n = create_pg("primary", start=False)
    n.append_conf("log_connections = all\n")
    n.append_conf("oauth_validator_libraries = 'validator'\n")
    # Needed to inspect postmaster log after connection failure:
    n.append_conf("log_min_messages = debug2")
    n.start()

    n.safe_sql("CREATE USER test;")

    path = os.path.join(n.data_dir, "pg_hba.conf")
    if os.path.exists(path):
        os.unlink(path)
    n.append_conf(
        f'\nlocal all test oauth issuer="{ISSUER}" scope="{SCOPE}"\n',
        filename="pg_hba.conf",
    )
    n.reload()
    n.wait_for_log(r"reloading configuration files")

    yield n


# ---------------------------------------------------------------------------
# Test driver
# ---------------------------------------------------------------------------


def _run_case(
    n,
    common_connstr,
    test_name,
    *,
    flags=None,
    expect_success=False,
    expected_stderr=None,
    log_like=None,
):
    """Run oauth_hook_client for one case and assert on its output.

    *common_connstr* is the connection string appended after the flags, so the
    binary is invoked as ``oauth_hook_client <flags...> <common_connstr>``.
    """
    flags = flags or []
    cmd = ["oauth_hook_client", *flags, common_connstr]

    log_start = n.log_position()
    res = n.pg_bin.result(cmd)

    if expect_success:
        assert re.search(r"connection succeeded", res.stdout), (
            f"{test_name}: stdout matches, got {res.stdout!r}"
        )

    if expected_stderr is not None:
        assert re.search(expected_stderr, res.stderr), (
            f"{test_name}: stderr matches {expected_stderr!r}, got {res.stderr!r}"
        )
    else:
        assert res.stderr == "", (
            f"{test_name}: no stderr, got {res.stderr!r}"
        )

    if log_like is not None:
        # See connect_fails(): to avoid races, wait for the postmaster to flush
        # the log for the finished connection.
        # Use (?s) so ".*" spans the newline between the two DEBUG lines, since
        # wait_for_log compiles the regex without the DOTALL flag.
        n.wait_for_log(
            r"(?s)DEBUG:  (?:00000: )?forked new client backend, pid=(\d+) "
            r"socket.*DEBUG:  (?:00000: )?client backend \(PID \1\) exited "
            r"with exit code \d",
            log_start,
        )
        n.log_check(f"{test_name}: log matches", log_start, log_like=log_like)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_oauth_client(node):
    n = node

    os.environ["PGOAUTHDEBUG"] = "UNSAFE"
    try:
        base_connstr = f"{n.connstr()} user={USER}"
        common_connstr = f"{base_connstr} oauth_issuer={ISSUER} oauth_client_id=myID"

        _run_case(
            n,
            common_connstr,
            "basic synchronous hook can provide a token",
            flags=[
                "--token", "my-token",
                "--expected-uri", f"{ISSUER}/.well-known/openid-configuration",
                "--expected-issuer", ISSUER,
                "--expected-scope", SCOPE,
            ],
            expect_success=True,
            log_like=[rf'oauth_validator: token="my-token", role="{USER}"'],
        )

        # The issuer ID provided to the hook is based on, but not equal to,
        # oauth_issuer.  Make sure the correct string is passed.
        common_connstr = (
            f"{base_connstr} "
            f"oauth_issuer={ISSUER}/.well-known/openid-configuration "
            f"oauth_client_id=myID oauth_scope='{SCOPE}'"
        )
        _run_case(
            n,
            common_connstr,
            "derived issuer ID is correctly provided",
            flags=[
                "--token", "my-token",
                "--expected-uri", f"{ISSUER}/.well-known/openid-configuration",
                "--expected-issuer", ISSUER,
                "--expected-scope", SCOPE,
            ],
            expect_success=True,
            log_like=[rf'oauth_validator: token="my-token", role="{USER}"'],
        )

        common_connstr = f"{base_connstr} oauth_issuer={ISSUER} oauth_client_id=myID"

        # Make sure the v1 hook continues to work.
        _run_case(
            n,
            common_connstr,
            "v1 synchronous hook can provide a token",
            flags=[
                "-v1",
                "--token", "my-token-v1",
                "--expected-uri", f"{ISSUER}/.well-known/openid-configuration",
                "--expected-scope", SCOPE,
            ],
            expect_success=True,
            log_like=[rf'oauth_validator: token="my-token-v1", role="{USER}"'],
        )

        if os.environ.get("with_libcurl") != "yes":
            # libpq should help users out if no OAuth support is built in.
            _run_case(
                n,
                common_connstr,
                "fails without custom hook installed",
                flags=["--no-hook"],
                expected_stderr=(
                    r"no OAuth flows are available "
                    r"\(try installing the libpq-oauth package\)"
                ),
            )

        # v2 synchronous flows should be able to set custom error messages.
        _run_case(
            n,
            common_connstr,
            "basic synchronous hook can set error messages",
            flags=["--error", "a custom error message"],
            expected_stderr=r"user-defined OAuth flow failed: a custom error message",
        )

        # connect_timeout should work if the flow doesn't respond.
        common_connstr_timeout = f"{common_connstr} connect_timeout=1"
        _run_case(
            n,
            common_connstr_timeout,
            "connect_timeout interrupts hung client flow",
            flags=["--hang-forever"],
            expected_stderr=r"failed: timeout expired",
        )

        # Remove the timeout for later tests.
        common_connstr = f"{base_connstr} oauth_issuer={ISSUER} oauth_client_id=myID"

        # Test various misbehaviors of the client hook.
        cases = [
            {
                "flag": "--misbehave=no-hook",
                "expected_error": (
                    r"user-defined OAuth flow provided neither a token "
                    r"nor an async callback"
                ),
            },
            {
                "flag": "--misbehave=fail-async",
                "expected_error": r"user-defined OAuth flow failed",
            },
            {
                "flag": "--misbehave=no-token",
                "expected_error": r"user-defined OAuth flow did not provide a token",
            },
            {
                "flag": "--misbehave=no-socket",
                "expected_error": (
                    r"user-defined OAuth flow did not provide a socket for polling"
                ),
            },
        ]

        for c in cases:
            _run_case(
                n,
                common_connstr,
                f"hook misbehavior: {c['flag']}",
                flags=[c["flag"]],
                expected_stderr=c["expected_error"],
            )
            _run_case(
                n,
                common_connstr,
                f"hook misbehavior: {c['flag']} (v1)",
                flags=["-v1", c["flag"]],
                expected_stderr=c["expected_error"],
            )

        # v2 async flows should be able to set error messages, too.
        _run_case(
            n,
            common_connstr,
            "asynchronous hook can set error messages",
            flags=["--misbehave", "fail-async", "--error", "async error message"],
            expected_stderr=(
                r"user-defined OAuth flow failed: async error message"
            ),
        )
    finally:
        os.environ.pop("PGOAUTHDEBUG", None)
