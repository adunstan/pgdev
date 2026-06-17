# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests the libpq builtin OAuth flow, plus server-side HBA and validator setup.

Connections run in-process through the libpq Session helpers
(node.connect_ok / node.connect_fails).  The builtin OAuth device flow writes
its "Visit ... and enter the code" prompt and its "[libpq] total number of
polls" line directly to the process's real stderr (fd 2) rather than through a
libpq notice.  connect_ok/connect_fails capture fd 2 during the connection
attempt and fold it into the stderr they match against expected_stderr, so
those device prompts / WARNINGs are passed as expected_stderr regexes.
"""

import base64
import contextlib
import json
import os
import re
import shutil
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

if "oauth" not in os.environ.get("PG_TEST_EXTRA", "").split():
    pytest.skip(
        "Potentially unsafe test oauth not enabled in PG_TEST_EXTRA",
        allow_module_level=True,
    )

if os.environ.get("with_libcurl") != "yes":
    pytest.skip(
        "client-side OAuth not supported by this build",
        allow_module_level=True,
    )

if os.environ.get("with_python") != "yes":
    pytest.skip(
        "OAuth tests require --with-python to run",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The device-flow prompt printed by the builtin libpq flow.  connect_ok
# captures fd 2, so these are passed as expected_stderr.
PROMPT_EXAMPLE_COM = r"Visit https://example\.com/ and enter the code: postgresuser"
PROMPT_EXAMPLE_ORG = r"Visit https://example\.org/ and enter the code: postgresuser"


def _set_hba(node, contents):
    """Replace pg_hba.conf with *contents* (unlink then append)."""
    path = os.path.join(node.data_dir, "pg_hba.conf")
    if os.path.exists(path):
        os.unlink(path)
    node.append_conf(contents, filename="pg_hba.conf")


def _set_ident(node, contents):
    path = os.path.join(node.data_dir, "pg_ident.conf")
    if os.path.exists(path):
        os.unlink(path)
    node.append_conf(contents, filename="pg_ident.conf")


@contextlib.contextmanager
def _env(**kwargs):
    """Temporarily set environment variables (None deletes), restoring after."""
    saved = {k: os.environ.get(k) for k in kwargs}
    try:
        for k, v in kwargs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _restart_failok(node):
    """Restart allowing the start to fail; return True on success, else False.

    The framework's restart() raises on a failed start, so we stop then
    start(fail_ok=True) to observe failure.
    """
    node.stop(fail_ok=True)
    return node.start(fail_ok=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def node(create_pg, oauth_server):
    """A started node configured for the OAuth validator, plus the mock server.

    Yields a tuple ``(node, issuer)`` where *issuer* is the HTTPS issuer URL of
    the running mock OAuth provider.
    """
    n = create_pg("primary", start=False)
    n.append_conf("log_connections = all\n")
    n.append_conf("oauth_validator_libraries = 'validator'\n")
    # Needed to allow connect_fails to inspect postmaster log:
    n.append_conf("log_min_messages = debug2")
    n.start()

    n.safe_sql("CREATE USER test;")
    n.safe_sql("CREATE USER testalt;")
    n.safe_sql("CREATE USER testparam;")

    script = os.path.join(os.path.dirname(__file__), "..", "t", "oauth_server.py")
    srv = oauth_server(script)
    issuer = f"https://127.0.0.1:{srv.port}"

    yield n, issuer, srv


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_oauth_server(node):
    n, issuer, srv = node
    cert_dir = os.environ.get("cert_dir")
    alternative_ca = f"{cert_dir}/root+server_ca.crt"

    # A background session for later configuration changes (ALTER SYSTEM, etc.).
    bgconn = n.session()

    # ----------------------------------------------------------------------
    # Check the client refuses HTTP and untrusted HTTPS by default.
    # ----------------------------------------------------------------------
    http_issuer = f"http://127.0.0.1:{srv.port}"

    _set_hba(
        n,
        f'\nlocal all test oauth issuer="{http_issuer}" scope="openid postgres"\n',
    )
    n.reload()
    log_start = n.wait_for_log(r"reloading configuration files")

    n.connect_fails(
        f"user=test dbname=postgres oauth_issuer={http_issuer} oauth_client_id=f02c6361-0635",
        "HTTPS is required without debug mode",
        expected_stderr=(
            r'OAuth discovery URI "'
            + re.escape(http_issuer)
            + r'/.well-known/openid-configuration" must use HTTPS'
        ),
    )

    # PGOAUTHDEBUG=http should have no effect (it needs an UNSAFE: marker).
    # The "option ... is unsafe" WARNING is printed by the builtin flow to the
    # real stderr (fd 2); connect_fails captures it together with the libpq
    # "must use HTTPS" error.  Match both with a single multiline/dotall regex.
    with _env(PGOAUTHDEBUG="http"):
        n.connect_fails(
            f"user=test dbname=postgres oauth_issuer={http_issuer} oauth_client_id=f02c6361-0635",
            "HTTPS is required without debug mode (bad PGOAUTHDEBUG value)",
            expected_stderr=(
                r'(?ms)^WARNING: .* option "http" is unsafe'
                r".*"
                r'OAuth discovery URI "'
                + re.escape(http_issuer)
                + r'/.well-known/openid-configuration" must use HTTPS'
            ),
        )

    # ----------------------------------------------------------------------
    # Switch to HTTPS.
    # ----------------------------------------------------------------------
    _set_hba(
        n,
        f"""
local all test      oauth issuer="{issuer}"       scope="openid postgres"
local all testalt   oauth issuer="{issuer}/.well-known/oauth-authorization-server/alternate" scope="openid postgres alt"
local all testparam oauth issuer="{issuer}/param" scope="openid postgres"
""",
    )
    n.reload()
    log_start = n.wait_for_log(r"reloading configuration files", log_start)

    # Check pg_hba_file_rules() support.
    contents = bgconn.query_safe(
        "SELECT rule_number, auth_method, options "
        "FROM pg_hba_file_rules ORDER BY rule_number;"
    )
    expected = (
        f'1|oauth|{{issuer={issuer},"scope=openid postgres",validator=validator}}\n'
        f'2|oauth|{{issuer={issuer}/.well-known/oauth-authorization-server/alternate,"scope=openid postgres alt",validator=validator}}\n'
        f'3|oauth|{{issuer={issuer}/param,"scope=openid postgres",validator=validator}}'
    )
    assert contents == expected, (
        "pg_hba_file_rules recreates OAuth HBA settings\n"
        f"got: {contents!r}\nexpected: {expected!r}"
    )

    # Make sure PGOAUTHDEBUG=UNSAFE doesn't disable certificate verification.
    with _env(PGOAUTHDEBUG="UNSAFE"):
        n.connect_fails(
            f"user=test dbname=postgres oauth_issuer={issuer} oauth_client_id=f02c6361-0635",
            "HTTPS trusts only system CA roots by default",
            expected_stderr=(
                r"(?i)could not fetch OpenID discovery document:.*peer certificate"
            ),
        )

    user = "test"

    # Use the oauth_ca_file option to specify the alternative CA path.
    n.connect_ok(
        f"user={user} dbname=postgres oauth_issuer={issuer} oauth_client_id=f02c6361-0635 oauth_ca_file={alternative_ca}",
        "connect as test (oauth_ca_file)",
        expected_stderr=PROMPT_EXAMPLE_COM,
        log_like=[
            rf'oauth_validator: token="9243959234", role="{user}"',
            r'oauth_validator: issuer="'
            + re.escape(issuer)
            + r'", scope="openid postgres"',
            r'connection authenticated: identity="test" method=oauth',
            r"connection authorized",
        ],
    )

    # Make sure we can use the environment variable without PGOAUTHDEBUG, and
    # then use it for the rest of the tests.
    with _env(PGOAUTHCAFILE=alternative_ca):
        n.connect_ok(
            f"user={user} dbname=postgres oauth_issuer={issuer} oauth_client_id=f02c6361-0635",
            "connect as test",
            expected_stderr=PROMPT_EXAMPLE_COM,
            log_like=[
                rf'oauth_validator: token="9243959234", role="{user}"',
                r'oauth_validator: issuer="'
                + re.escape(issuer)
                + r'", scope="openid postgres"',
                r'connection authenticated: identity="test" method=oauth',
                r"connection authorized",
            ],
            log_unlike=[r"FATAL.*OAuth bearer authentication failed"],
        )

        # Enable some debugging features for all remaining tests:
        # - trace, for detailed Curl logs on failure
        # - dos-endpoint, to speed up the three-way handshake
        # - call-count, for our later sanity check
        with _env(PGOAUTHDEBUG="UNSAFE:trace,dos-endpoint,call-count"):

            # The /alternate issuer uses slightly different parameters, along
            # with an OAuth-style discovery document.
            user = "testalt"
            n.connect_ok(
                f"user={user} dbname=postgres oauth_issuer={issuer}/alternate oauth_client_id=f02c6361-0636",
                "connect as testalt",
                expected_stderr=PROMPT_EXAMPLE_ORG,
                log_like=[
                    rf'oauth_validator: token="9243959234-alt", role="{user}"',
                    r'oauth_validator: issuer="'
                    + re.escape(
                        f"{issuer}/.well-known/oauth-authorization-server/alternate"
                    )
                    + r'", scope="openid postgres alt"',
                    r'connection authenticated: identity="testalt" method=oauth',
                    r"connection authorized",
                ],
                log_unlike=[r"FATAL.*OAuth bearer authentication failed"],
            )

            # The issuer linked by the server must match the client's
            # oauth_issuer setting.
            n.connect_fails(
                f"user={user} dbname=postgres oauth_issuer={issuer} oauth_client_id=f02c6361-0636",
                "oauth_issuer must match discovery",
                expected_stderr=(
                    r"server's discovery document at "
                    + re.escape(
                        f"{issuer}/.well-known/oauth-authorization-server/alternate"
                    )
                    + r' \(issuer "'
                    + re.escape(f"{issuer}/alternate")
                    + r'"\) is incompatible with oauth_issuer \('
                    + re.escape(issuer)
                    + r"\)"
                ),
            )

            # Test require_auth settings against OAUTHBEARER.
            cases = [
                {"require_auth": "oauth"},
                {"require_auth": "oauth,scram-sha-256"},
                {"require_auth": "password,oauth"},
                {"require_auth": "none,oauth"},
                {"require_auth": "!scram-sha-256"},
                {"require_auth": "!none"},
                {
                    "require_auth": "!oauth",
                    "failure": r"server requested OAUTHBEARER authentication",
                },
                {
                    "require_auth": "scram-sha-256",
                    "failure": r"server requested OAUTHBEARER authentication",
                },
                {
                    "require_auth": "!password,!oauth",
                    "failure": r"server requested OAUTHBEARER authentication",
                },
                {
                    "require_auth": "none",
                    "failure": r"server requested SASL authentication",
                },
                {
                    "require_auth": "!oauth,!scram-sha-256",
                    "failure": r"server requested SASL authentication",
                },
            ]

            user = "test"
            for c in cases:
                case_connstr = (
                    f"user={user} dbname=postgres oauth_issuer={issuer} "
                    f"oauth_client_id=f02c6361-0635 require_auth={c['require_auth']}"
                )
                if "failure" in c:
                    n.connect_fails(
                        case_connstr,
                        f"require_auth={c['require_auth']} fails",
                        expected_stderr=c["failure"],
                    )
                else:
                    n.connect_ok(
                        case_connstr,
                        f"require_auth={c['require_auth']} succeeds",
                        expected_stderr=PROMPT_EXAMPLE_COM,
                    )

            # Make sure the client_id and secret are correctly encoded.
            # $vschars contains every allowed character for a client_id/_secret
            # (the "VSCHAR" class).  In a connection string a single quote and
            # backslash must be backslash-escaped.
            vschars = (
                " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
                "abcdefghijklmnopqrstuvwxyz{|}~"
            )
            vschars_esc = vschars.replace("\\", "\\\\").replace("'", "\\'")

            n.connect_ok(
                f"user={user} dbname=postgres oauth_issuer={issuer} oauth_client_id='{vschars_esc}'",
                "escapable characters: client_id",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )
            n.connect_ok(
                f"user={user} dbname=postgres oauth_issuer={issuer} oauth_client_id='{vschars_esc}' oauth_client_secret='{vschars_esc}'",
                "escapable characters: client_id and secret",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )

            # ------------------------------------------------------------------
            # Tests relying on oauth_server.py behaviors triggered via the
            # special .../param issuer (set up in HBA for testparam) by encoding
            # magic instructions into the oauth_client_id.
            # ------------------------------------------------------------------
            common_connstr = (
                f"user=testparam dbname=postgres oauth_issuer={issuer}/param "
            )
            base = {"value": common_connstr}

            def connstr(**params):
                js = json.dumps(params, separators=(",", ":"))
                # encode_base64($json, "") -> no line breaks
                encoded = base64.b64encode(js.encode("utf-8")).decode("ascii")
                return f"{base['value']} oauth_client_id={encoded}"

            # Make sure the param system works end-to-end first.
            n.connect_ok(
                connstr(), "connect to /param", expected_stderr=PROMPT_EXAMPLE_COM
            )

            n.connect_ok(
                connstr(stage="token", retries=1),
                "token retry",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )
            n.connect_ok(
                connstr(stage="token", retries=2),
                "token retry (twice)",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )
            n.connect_ok(
                connstr(stage="all", retries=1, interval=2),
                "token retry (two second interval)",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )
            n.connect_ok(
                connstr(stage="all", retries=1, interval=None),
                "token retry (default interval)",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )

            n.connect_ok(
                connstr(stage="all", content_type="application/json;charset=utf-8"),
                "content type with charset",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )
            n.connect_ok(
                connstr(
                    stage="all", content_type="application/json \t;\t charset=utf-8"
                ),
                "content type with charset (whitespace)",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )
            n.connect_ok(
                connstr(stage="device", uri_spelling="verification_url"),
                "alternative spelling of verification_uri",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )

            n.connect_fails(
                connstr(stage="device", huge_response=True),
                "bad device authz response: overlarge JSON",
                expected_stderr=r"could not obtain device authorization: response is too large",
            )
            n.connect_fails(
                connstr(stage="token", huge_response=True),
                "bad token response: overlarge JSON",
                expected_stderr=r"could not obtain access token: response is too large",
            )

            nesting_limit = 16
            n.connect_ok(
                connstr(
                    stage="device",
                    nested_array=nesting_limit,
                    nested_object=nesting_limit,
                ),
                "nested arrays and objects, up to parse limit",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )
            n.connect_fails(
                connstr(stage="device", nested_array=nesting_limit + 1),
                "bad discovery response: overly nested JSON array",
                expected_stderr=r"could not parse device authorization: JSON is too deeply nested",
            )
            n.connect_fails(
                connstr(stage="device", nested_object=nesting_limit + 1),
                "bad discovery response: overly nested JSON object",
                expected_stderr=r"could not parse device authorization: JSON is too deeply nested",
            )

            n.connect_fails(
                connstr(stage="device", content_type="text/plain"),
                "bad device authz response: wrong content type",
                expected_stderr=r"could not parse device authorization: unexpected content type",
            )
            n.connect_fails(
                connstr(stage="token", content_type="text/plain"),
                "bad token response: wrong content type",
                expected_stderr=r"could not parse access token response: unexpected content type",
            )
            n.connect_fails(
                connstr(stage="token", content_type="application/jsonx"),
                "bad token response: wrong content type (correct prefix)",
                expected_stderr=r"could not parse access token response: unexpected content type",
            )

            n.connect_fails(
                # 2**64-1 is the all-ones 64-bit unsigned integer.  This huge
                # interval makes the client overflow when it adds the
                # device-authz interval.
                connstr(
                    stage="all",
                    interval=2**64 - 1,
                    retries=1,
                    retry_code="slow_down",
                ),
                "bad token response: server overflows the device authz interval",
                expected_stderr=r"could not obtain access token: slow_down interval overflow",
            )

            n.connect_fails(
                connstr(stage="token", error_code="invalid_grant"),
                "bad token response: invalid_grant, no description",
                expected_stderr=r"could not obtain access token: \(invalid_grant\)",
            )
            n.connect_fails(
                connstr(
                    stage="token",
                    error_code="invalid_grant",
                    error_desc="grant expired",
                ),
                "bad token response: expired grant",
                expected_stderr=r"could not obtain access token: grant expired \(invalid_grant\)",
            )
            n.connect_fails(
                connstr(
                    stage="token",
                    error_code="invalid_client",
                    error_status=401,
                ),
                "bad token response: client authentication failure, default description",
                expected_stderr=(
                    r"could not obtain access token: provider requires client authentication, "
                    r"and no oauth_client_secret is set \(invalid_client\)"
                ),
            )
            n.connect_fails(
                connstr(
                    stage="token",
                    error_code="invalid_client",
                    error_status=401,
                    error_desc="authn failure",
                ),
                "bad token response: client authentication failure, provided description",
                expected_stderr=r"could not obtain access token: authn failure \(invalid_client\)",
            )

            n.connect_fails(
                connstr(stage="token", token=""),
                "server rejects access: empty token",
                expected_stderr=r"bearer authentication failed",
            )
            n.connect_fails(
                connstr(stage="token", token="****"),
                "server rejects access: invalid token contents",
                expected_stderr=r"bearer authentication failed",
            )

            # Test behavior of the oauth_client_secret.
            base["value"] = f"{common_connstr} oauth_client_secret=''"

            n.connect_ok(
                connstr(stage="all", expected_secret=""),
                "empty oauth_client_secret",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )

            base["value"] = f"{common_connstr} oauth_client_secret='{vschars_esc}'"

            n.connect_ok(
                connstr(stage="all", expected_secret=vschars),
                "nonempty oauth_client_secret",
                expected_stderr=PROMPT_EXAMPLE_COM,
            )

            n.connect_fails(
                connstr(
                    stage="token",
                    error_code="invalid_client",
                    error_status=401,
                ),
                "bad token response: client authentication failure, default description with oauth_client_secret",
                expected_stderr=(
                    r"could not obtain access token: provider rejected the oauth_client_secret "
                    r"\(invalid_client\)"
                ),
            )
            n.connect_fails(
                connstr(
                    stage="token",
                    error_code="invalid_client",
                    error_status=401,
                    error_desc="mutual TLS required for client",
                ),
                "bad token response: client authentication failure, provided description with oauth_client_secret",
                expected_stderr=(
                    r"could not obtain access token: mutual TLS required for client \(invalid_client\)"
                ),
            )

            # ------------------------------------------------------------------
            # Count the number of calls to the internal flow when multiple
            # retries are triggered.  The poll count is printed to fd 2 by the
            # builtin flow (call-count debug feature).  We grab stderr and run
            # several checks against it via attempt_connection, which folds
            # fd 2 into the stderr it returns.
            # ------------------------------------------------------------------
            base["value"] = common_connstr
            ok, _stdout, captured = n.attempt_connection(
                connstr(stage="token", retries=2),
                "SELECT 'connected for call count'",
            )
            assert ok, f"call count connection succeeds, got {captured!r}"
            assert re.search(
                PROMPT_EXAMPLE_COM, captured
            ), f"call count: stderr matches, got {captured!r}"
            m = re.search(r"\[libpq\] total number of polls: (\d+)", captured)
            assert m is not None, f"call count: count is printed, got {captured!r}"
            # A typical two-retry flow takes 5-15 calls; hundreds/thousands would
            # indicate the multiplexer isn't clearing stale events.
            assert (
                int(m.group(1)) < 100
            ), f"call count is reasonably small: {m.group(1)}"

            # ------------------------------------------------------------------
            # Stress test: make sure the builtin flow operates correctly even if
            # the client application isn't respecting PGRES_POLLING_*.  This uses
            # the oauth_hook_client test program (a separate C binary).
            # ------------------------------------------------------------------
            base["value"] = f"{common_connstr} port={n.port} host={n.host}"
            cmd = [
                "oauth_hook_client",
                "--no-hook",
                "--stress-async",
                connstr(stage="all", retries=1, interval=1),
            ]
            print("# running '" + "' '".join(cmd) + "'")
            exe = shutil.which("oauth_hook_client") or "oauth_hook_client"
            cmd[0] = exe
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            assert re.search(
                r"connection succeeded", proc.stdout
            ), f"stress-async: stdout matches, got {proc.stdout!r}"
            assert not re.search(
                r"connection to database failed", proc.stderr
            ), f"stress-async: stderr matches, got {proc.stderr!r}"

        # End of PGOAUTHDEBUG=UNSAFE:trace,... block.

        # ------------------------------------------------------------------
        # This section reconfigures the validator module itself, rather than
        # the OAuth server.  Hardcode the discovery URI (and empty scope) so
        # OAuth parameter discovery doesn't clutter the logs.
        # ------------------------------------------------------------------
        common_connstr = (
            f"dbname=postgres oauth_issuer={issuer}/.well-known/openid-configuration "
            f"oauth_scope='' oauth_client_id=f02c6361-0635"
        )

        # Misbehaving validators must fail shut.
        bgconn.do("ALTER SYSTEM SET oauth_validator.authn_id TO ''")
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        n.connect_fails(
            f"{common_connstr} user=test",
            "validator must set authn_id",
            expected_stderr=r"OAuth bearer authentication failed",
            log_like=[
                r'connection authenticated: identity=""',
                r"FATAL: ( [A-Z0-9]+:)? OAuth bearer authentication failed",
                r"DETAIL:\s+Validator provided no identity",
            ],
        )

        # Even if a validator authenticates the user, if the token isn't valid
        # the connection fails.
        bgconn.do("ALTER SYSTEM SET oauth_validator.authn_id TO 'test@example.org'")
        bgconn.do("ALTER SYSTEM SET oauth_validator.authorize_tokens TO false")
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        n.connect_fails(
            f"{common_connstr} user=test",
            "validator must authorize token explicitly",
            expected_stderr=r"OAuth bearer authentication failed",
            log_like=[
                r'connection authenticated: identity="test@example\.org"',
                r"FATAL: ( [A-Z0-9]+:)? OAuth bearer authentication failed",
                r"DETAIL:\s+Validator failed to authorize the provided token",
            ],
        )

        # Validators can provide their own explanations.
        bgconn.query_safe(
            "ALTER SYSTEM SET oauth_validator.error_detail TO 'something failed'"
        )
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        n.connect_fails(
            f"{common_connstr} user=test",
            "validator must authorize token explicitly (custom logdetail)",
            expected_stderr=r"OAuth bearer authentication failed",
            log_like=[
                r'connection authenticated: identity="test@example\.org"',
                r"FATAL:\s+OAuth bearer authentication failed",
                r"DETAIL:\s+something failed",
            ],
        )

        bgconn.query_safe("ALTER SYSTEM SET oauth_validator.internal_error TO true")
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        n.connect_fails(
            f"{common_connstr} user=test",
            "validator internal error (custom logdetail)",
            expected_stderr=r"OAuth bearer authentication failed",
            log_like=[
                r"WARNING:\s+internal error in OAuth validator module",
                r"DETAIL:\s+something failed",
            ],
        )

        bgconn.query_safe("ALTER SYSTEM RESET oauth_validator.error_detail")
        bgconn.query_safe("ALTER SYSTEM RESET oauth_validator.internal_error")

        # We complain when bad option names are registered, but connections may
        # proceed (users can't set those options in the HBA anyway).
        bgconn.query_safe("ALTER SYSTEM RESET oauth_validator.authn_id")
        bgconn.query_safe("ALTER SYSTEM RESET oauth_validator.authorize_tokens")
        bgconn.query_safe("ALTER SYSTEM SET oauth_validator.invalid_hba TO true")
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        n.connect_ok(
            f"{common_connstr} user=test",
            "bad registered HBA option",
            expected_stderr=PROMPT_EXAMPLE_COM,
            log_like=[
                r'WARNING:\s+HBA option name "bad option name" is invalid and will be ignored',
                r'CONTEXT:\s+validator module "validator", in call to RegisterOAuthHBAOptions',
            ],
        )

        bgconn.query_safe("ALTER SYSTEM RESET oauth_validator.invalid_hba")

        # ------------------------------------------------------------------
        # Test user mapping.
        # ------------------------------------------------------------------
        # Allow "user@example.com" to log in under the test role.
        _set_ident(n, "\noauthmap\tuser@example.com\ttest\n")

        # test and testalt use the map; testparam uses ident delegation.
        _set_hba(
            n,
            f"""
local all test      oauth issuer="{issuer}" scope="" map=oauthmap
local all testalt   oauth issuer="{issuer}" scope="" map=oauthmap
local all testparam oauth issuer="{issuer}" scope="" delegate_ident_mapping=1
""",
        )

        # To start, have the validator use the role names as authn IDs.
        bgconn.do("ALTER SYSTEM RESET oauth_validator.authn_id")
        bgconn.do("ALTER SYSTEM RESET oauth_validator.authorize_tokens")
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        # The test and testalt roles should no longer map correctly.
        n.connect_fails(
            f"{common_connstr} user=test",
            "mismatched username map (test)",
            expected_stderr=r"OAuth bearer authentication failed",
        )
        n.connect_fails(
            f"{common_connstr} user=testalt",
            "mismatched username map (testalt)",
            expected_stderr=r"OAuth bearer authentication failed",
        )

        # Have the validator identify the end user as user@example.com.
        bgconn.do("ALTER SYSTEM SET oauth_validator.authn_id TO 'user@example.com'")
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        # Now the test role can be logged into. (testalt still can't be mapped.)
        n.connect_ok(
            f"{common_connstr} user=test",
            "matched username map (test)",
            expected_stderr=PROMPT_EXAMPLE_COM,
        )
        n.connect_fails(
            f"{common_connstr} user=testalt",
            "mismatched username map (testalt)",
            expected_stderr=r"OAuth bearer authentication failed",
        )

        # testparam ignores the map entirely.
        n.connect_ok(
            f"{common_connstr} user=testparam",
            "delegated ident (testparam)",
            expected_stderr=PROMPT_EXAMPLE_COM,
        )

        bgconn.do("ALTER SYSTEM RESET oauth_validator.authn_id")
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        bgconn.quit()  # the tests below restart the server

        # ------------------------------------------------------------------
        # Test validator-specific HBA options.
        # ------------------------------------------------------------------
        _set_hba(
            n,
            f"""
local all test    oauth issuer="{issuer}" scope="openid postgres" delegate_ident_mapping=1 \\
                        validator.authn_id="ignored" validator.authn_id="other-identity"
local all testalt oauth issuer="{issuer}" scope="openid postgres" validator.log="testalt message"
""",
        )
        n.reload()
        log_start = n.wait_for_log(r"reloading configuration files", log_start)

        n.connect_ok(
            f"{common_connstr} user=test",
            "custom HBA setting (test)",
            expected_stderr=PROMPT_EXAMPLE_COM,
            log_like=[r'connection authenticated: identity="other-identity"'],
        )
        n.connect_ok(
            f"{common_connstr} user=testalt",
            "custom HBA setting (testalt)",
            expected_stderr=PROMPT_EXAMPLE_COM,
            log_like=[
                r"LOG:\s+testalt message",
                r'connection authenticated: identity="testalt"',
            ],
        )

        # bad syntax
        _set_hba(
            n,
            f'\nlocal all testalt oauth issuer="{issuer}" scope="openid postgres" validator.=1\n',
        )
        log_start = n.log_position()
        _restart_failok(n)
        n.log_check(
            "empty HBA option name",
            log_start,
            log_like=[r'invalid OAuth validator option name: "validator\."'],
        )

        _set_hba(
            n,
            f'\nlocal all testalt oauth issuer="{issuer}" scope="openid postgres" validator.@@=1\n',
        )
        log_start = n.log_position()
        _restart_failok(n)
        n.log_check(
            "invalid HBA option name",
            log_start,
            log_like=[r'invalid OAuth validator option name: "validator\.@@"'],
        )

        # unknown settings (validation is deferred to connect time)
        _set_hba(
            n,
            f"""
local all testalt oauth issuer="{issuer}" scope="openid postgres" \\
                        validator.log=ignored validator.bad=1
""",
        )
        n.restart()

        n.connect_fails(
            f"{common_connstr} user=testalt",
            "bad HBA setting",
            expected_stderr=r"OAuth bearer authentication failed",
            log_like=[
                r'WARNING:\s+unrecognized authentication option name: "validator\.bad"',
                r"FATAL:\s+OAuth bearer authentication failed",
                r'DETAIL:\s+unrecognized authentication option name: "validator\.bad"',
            ],
        )

        # ------------------------------------------------------------------
        # Test multiple validators.
        # ------------------------------------------------------------------
        n.append_conf("oauth_validator_libraries = 'validator, fail_validator'\n")

        # With multiple validators, every HBA line must explicitly declare one.
        result = _restart_failok(n)
        assert (
            result is False
        ), "restart fails without explicit validators in oauth HBA entries"
        log_start = n.wait_for_log(
            r'authentication method "oauth" requires option "validator" to be set',
            log_start,
        )

        _set_hba(
            n,
            f"""
local all test    oauth validator=validator      issuer="{issuer}"           scope="openid postgres"
local all testalt oauth validator=fail_validator issuer="{issuer}/.well-known/oauth-authorization-server/alternate" scope="openid postgres alt"
""",
        )
        n.restart()
        log_start = n.wait_for_log(r"ready to accept connections", log_start)

        # The test user should work as before.
        user = "test"
        n.connect_ok(
            f"user={user} dbname=postgres oauth_issuer={issuer} oauth_client_id=f02c6361-0635",
            f"validator is used for {user}",
            expected_stderr=PROMPT_EXAMPLE_COM,
            log_like=[r"connection authorized"],
        )

        # testalt should be routed through the fail_validator.
        user = "testalt"
        n.connect_fails(
            f"user={user} dbname=postgres oauth_issuer={issuer}/.well-known/oauth-authorization-server/alternate oauth_client_id=f02c6361-0636",
            f"fail_validator is used for {user}",
            expected_stderr=r"FATAL: ( [A-Z0-9]+:)? fail_validator: sentinel error",
        )

        # ------------------------------------------------------------------
        # Test ABI compatibility magic marker.
        # ------------------------------------------------------------------
        n.append_conf("oauth_validator_libraries = 'magic_validator'\n")
        _set_hba(
            n,
            f'\nlocal all test    oauth validator=magic_validator      issuer="{issuer}"           scope="openid postgres"\n',
        )
        n.restart()
        log_start = n.wait_for_log(r"ready to accept connections", log_start)

        n.connect_fails(
            f"user=test dbname=postgres oauth_issuer={issuer}/.well-known/oauth-authorization-server/alternate oauth_client_id=f02c6361-0636",
            f"magic_validator is used for {user}",
            expected_stderr=(
                r'FATAL: ( [A-Z0-9]+:)? OAuth validator module "magic_validator": magic number mismatch'
            ),
        )
        n.stop()
