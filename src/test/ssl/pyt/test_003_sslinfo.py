# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for the sslinfo extension.

Exercises the sslinfo extension functions (ssl_is_used, ssl_version,
ssl_cipher, ssl_client_cert_present, ssl_client_serial, ssl_client_dn_field,
ssl_issuer_dn, ssl_issuer_field, ssl_extension_info, ...) over a real SSL
connection that presents a client certificate.

All SSL queries run in-process via the libpq Session (no psql fork); the
``ssl_server`` fixture handles the with_ssl=openssl skip and copies the
client keys with private permissions.
"""

import os

import libpq
import pytest

# This is the hostname used to connect to the server.  This cannot be a
# hostname, because the server certificate is always for the domain
# postgresql-ssl-regression.test.
SERVERHOSTADDR = "127.0.0.1"
# This is the pattern to use in pg_hba.conf to match incoming connections.
SERVERHOSTCIDR = "127.0.0.1/32"

# Path to the directory holding the test certificates/keys.
SSL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ssl"))


def _cert(name):
    """Absolute path to a cert/key file in the ssl directory."""
    # Forward slashes: these paths go into conninfo, where libpq treats a
    # backslash as an escape character.
    return os.path.join(SSL_DIR, name).replace("\\", "/")


# Set of default settings for SSL parameters in connection string.  This
# makes the tests protected against any defaults the environment may have
# in ~/.postgresql/.
DEFAULT_SSL_CONNSTR = (
    "sslkey=invalid sslcert=invalid sslrootcert=invalid "
    "sslcrl=invalid sslcrldir=invalid"
)


def _query(node, connstr, sql):
    """Run *sql* over an SSL connection described by *connstr*, trimmed text."""
    sess = libpq.Session(connstr=connstr, libdir=node.libdir)
    try:
        return sess.query_safe(sql)
    finally:
        sess.close()


def test_sslinfo(ssl_server, create_pg):
    # Faithful skip ordering.
    # The ssl_server fixture covers the with_ssl=openssl skip.
    if "ssl" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip("Potentially unsafe test SSL not enabled in PG_TEST_EXTRA")

    #### Set up the server.
    node = create_pg("primary")

    ssl_server.configure_test_server_for_ssl(
        node, SERVERHOSTADDR, SERVERHOSTCIDR, "trust", extensions=["sslinfo"]
    )

    # We aren't using any CRL's in this suite so we can keep using
    # server-revoked as server certificate for simple client.crt connection
    # much like how the 001 test does.
    ssl_server.switch_server_cert(node, certfile="server-revoked")

    # Determine whether build supports sslcertmode=require.  This corresponds
    # to checking for "#define HAVE_SSL_CTX_SET_CERT_CB 1" in pg_config.
    supports_sslcertmode_require = not ssl_server.is_libressl()

    # Name the port explicitly in the connection string.
    common_connstr = (
        f"{DEFAULT_SSL_CONNSTR} sslrootcert={_cert('root+server_ca.crt')} "
        f"sslmode=require dbname=certdb hostaddr={SERVERHOSTADDR} host=localhost "
        f"port={node.port} "
        f"user=ssltestuser sslcert={_cert('client_ext.crt')}"
        f"{ssl_server.sslkey('client_ext.key')}"
    )

    # Connection string for a connection without a client cert (trustdb).
    nocert_connstr = (
        f"{DEFAULT_SSL_CONNSTR} sslrootcert={_cert('root+server_ca.crt')} "
        f"sslmode=require dbname=trustdb hostaddr={SERVERHOSTADDR} "
        f"port={node.port} "
        "user=ssltestuser host=localhost"
    )

    # Make sure we can connect even though previous test suites have
    # established this.
    node.connect_ok(
        common_connstr,
        "certificate authorization succeeds with correct client cert in PEM format",
    )

    result = _query(node, common_connstr, "SELECT ssl_is_used();")
    assert result == "t", "ssl_is_used() for TLS connection"

    result = _query(
        node,
        common_connstr
        + " ssl_min_protocol_version=TLSv1.2 ssl_max_protocol_version=TLSv1.2",
        "SELECT ssl_version();",
    )
    assert result == "TLSv1.2", "ssl_version() correctly returning TLS protocol"

    result = _query(
        node,
        common_connstr,
        "SELECT ssl_cipher() = cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();",
    )
    assert result == "t", "ssl_cipher() compared with pg_stat_ssl"

    result = _query(node, common_connstr, "SELECT ssl_client_cert_present();")
    assert result == "t", "ssl_client_cert_present() for connection with cert"

    result = _query(node, nocert_connstr, "SELECT ssl_client_cert_present();")
    assert result == "f", "ssl_client_cert_present() for connection without cert"

    result = _query(
        node,
        common_connstr,
        "SELECT ssl_client_serial() = client_serial "
        "FROM pg_stat_ssl WHERE pid = pg_backend_pid();",
    )
    assert result == "t", "ssl_client_serial() compared with pg_stat_ssl"

    # Must not use query_safe since we expect an error here; query() returns
    # the error in the result rather than raising.
    sess = libpq.Session(connstr=common_connstr, libdir=node.libdir)
    try:
        res = sess.query("SELECT ssl_client_dn_field('invalid');")
        assert res.error_message is not None, (
            "ssl_client_dn_field() for an invalid field should error"
        )
    finally:
        sess.close()

    result = _query(node, nocert_connstr, "SELECT ssl_client_dn_field('commonName');")
    assert result == "", "ssl_client_dn_field() for connection without cert"

    result = _query(
        node,
        common_connstr,
        "SELECT '/CN=' || ssl_client_dn_field('commonName') = client_dn "
        "FROM pg_stat_ssl WHERE pid = pg_backend_pid();",
    )
    assert result == "t", "ssl_client_dn_field() for commonName"

    result = _query(
        node,
        common_connstr,
        "SELECT ssl_issuer_dn() = issuer_dn "
        "FROM pg_stat_ssl WHERE pid = pg_backend_pid();",
    )
    assert result == "t", "ssl_issuer_dn() for connection with cert"

    result = _query(
        node,
        common_connstr,
        "SELECT '/CN=' || ssl_issuer_field('commonName') = issuer_dn "
        "FROM pg_stat_ssl WHERE pid = pg_backend_pid();",
    )
    assert result == "t", "ssl_issuer_field() for commonName"

    result = _query(
        node,
        common_connstr,
        "SELECT value, critical FROM ssl_extension_info() "
        "WHERE name = 'basicConstraints';",
    )
    assert result == "CA:FALSE|t", "extract extension from cert"

    # Sanity tests for sslcertmode, using ssl_client_cert_present().
    cases = [
        {"opts": "sslcertmode=allow", "present": "t"},
        {"opts": "sslcertmode=allow sslcert=invalid", "present": "f"},
        {"opts": "sslcertmode=disable", "present": "f"},
    ]
    if supports_sslcertmode_require:
        cases.append({"opts": "sslcertmode=require", "present": "t"})

    for c in cases:
        result = _query(
            node,
            f"{common_connstr} dbname=trustdb {c['opts']}",
            "SELECT ssl_client_cert_present();",
        )
        assert result == c["present"], (
            f"ssl_client_cert_present() for {c['opts']}"
        )
