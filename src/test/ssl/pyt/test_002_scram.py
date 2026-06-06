# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test SCRAM authentication and TLS channel binding types over SSL."""

import os

import pytest

# This is the hostname used to connect to the server.
SERVERHOSTADDR = "127.0.0.1"
# This is the pattern to use in pg_hba.conf to match incoming connections.
SERVERHOSTCIDR = "127.0.0.1/32"


def test_002_scram(create_pg, ssl_server):
    # Faithful skip ordering.
    # The ssl_server fixture covers the with_ssl=openssl skip.
    if os.environ.get("with_ssl") != "openssl":
        pytest.skip("OpenSSL not supported by this build")
    if "ssl" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip("Potentially unsafe test SSL not enabled in PG_TEST_EXTRA")

    # Snapshot/restore env vars we mutate (PGPASSWORD) so the rest of the
    # suite is unaffected.
    saved_env = {k: os.environ.get(k) for k in ("PGPASSWORD",)}
    try:
        _run_body(create_pg, ssl_server)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_body(create_pg, ssl_server):
    # Determine whether this build uses OpenSSL or LibreSSL.
    libressl = ssl_server.is_libressl()

    # Determine whether build supports detection of hash algorithms for
    # RSA-PSS certificates.
    supports_rsapss_certs = ssl_server._check_pg_config(
        "#define HAVE_X509_GET_SIGNATURE_INFO 1"
    )
    # As of 5/2025, LibreSSL doesn't actually work for RSA-PSS certificates.
    if libressl:
        supports_rsapss_certs = False

    # Set up the server.
    #
    # Connections go through the in-process libpq layer; the connstr carries
    # host=/hostaddr= explicitly, and later keywords win over the node defaults
    # prepended by _full_connstr.

    # setting up data directory
    node = create_pg("primary")

    # could fail in FIPS mode
    md5_works = node.sql("select md5('')").error_message is None

    # Configure server for SSL connections, with password handling.
    ssl_server.configure_test_server_for_ssl(
        node, SERVERHOSTADDR, SERVERHOSTCIDR,
        "scram-sha-256",
        password="pass",
        password_enc="scram-sha-256",
    )
    ssl_server.switch_server_cert(node, certfile="server-cn-only")
    os.environ["PGPASSWORD"] = "pass"
    common_connstr = (
        "dbname=trustdb sslmode=require sslcert=invalid sslrootcert=invalid "
        f"hostaddr={SERVERHOSTADDR} host=localhost"
    )

    # Default settings
    node.connect_ok(
        f"{common_connstr} user=ssltestuser",
        "Basic SCRAM authentication with SSL",
    )

    # Test channel_binding
    node.connect_fails(
        f"{common_connstr} user=ssltestuser channel_binding=invalid_value",
        "SCRAM with SSL and channel_binding=invalid_value",
        expected_stderr=r'invalid channel_binding value: "invalid_value"',
    )
    node.connect_ok(
        f"{common_connstr} user=ssltestuser channel_binding=disable",
        "SCRAM with SSL and channel_binding=disable",
    )
    node.connect_ok(
        f"{common_connstr} user=ssltestuser channel_binding=require",
        "SCRAM with SSL and channel_binding=require",
    )

    # Now test when the user has an MD5-encrypted password; should fail
    if md5_works:
        node.connect_fails(
            f"{common_connstr} user=md5testuser channel_binding=require",
            "MD5 with SSL and channel_binding=require",
            expected_stderr=(
                r"channel binding required but not supported by server's "
                r"authentication request"
            ),
        )

    # Now test with auth method 'cert' by connecting to 'certdb'. Should fail,
    # because channel binding is not performed.  The ssl_server fixture has
    # already copied ssl/client.key to a private-perms temp copy that libpq
    # will accept.
    ssl_dir = os.path.join(os.path.dirname(__file__), "..", "ssl")
    client_crt = os.path.join(ssl_dir, "client.crt")
    client_tmp_key = ssl_server.key["client.key"]
    node.connect_fails(
        f"sslcert={client_crt} sslkey={client_tmp_key} sslrootcert=invalid "
        f"hostaddr={SERVERHOSTADDR} host=localhost dbname=certdb "
        "user=ssltestuser channel_binding=require",
        "Cert authentication and channel_binding=require",
        expected_stderr=(
            r"channel binding required, but server authenticated client "
            r"without channel binding"
        ),
    )

    # Certificate verification at the connection level should still work fine.
    node.connect_ok(
        f"sslcert={client_crt} sslkey={client_tmp_key} sslrootcert=invalid "
        f"hostaddr={SERVERHOSTADDR} host=localhost dbname=verifydb "
        "user=ssltestuser",
        "SCRAM with clientcert=verify-full",
        log_like=[
            r'connection authenticated: identity="ssltestuser" '
            r"method=scram-sha-256"
        ],
    )

    # channel_binding should continue to work independently of require_auth.
    node.connect_ok(
        f"{common_connstr} user=ssltestuser channel_binding=disable "
        "require_auth=scram-sha-256",
        "SCRAM with SSL, channel_binding=disable, and require_auth=scram-sha-256",
    )
    if md5_works:
        node.connect_fails(
            f"{common_connstr} user=md5testuser require_auth=md5 "
            "channel_binding=require",
            "channel_binding can fail even when require_auth succeeds",
            expected_stderr=(
                r"channel binding required but not supported by server's "
                r"authentication request"
            ),
        )
    node.connect_ok(
        f"{common_connstr} user=ssltestuser channel_binding=require "
        "require_auth=scram-sha-256",
        "SCRAM with SSL, channel_binding=require, and require_auth=scram-sha-256",
    )

    # Now test with a server certificate that uses the RSA-PSS algorithm.
    # This checks that the certificate can be loaded and that channel binding
    # works. (see bug #17760)
    if supports_rsapss_certs:
        ssl_server.switch_server_cert(node, certfile="server-rsapss")
        node.connect_ok(
            f"{common_connstr} user=ssltestuser channel_binding=require",
            "SCRAM with SSL and channel_binding=require, server certificate "
            "uses 'rsassaPss'",
            log_like=[
                r'connection authenticated: identity="ssltestuser" '
                r"method=scram-sha-256"
            ],
        )
