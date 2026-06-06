# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Server Name Indication (SNI) tests.  The server is taught to select a
certificate/key/CA per requested SNI hostname via pg_hosts.conf, and the
client sends the connection's ``host`` value as the SNI hostname.  The tests
exercise:
 - falling back to the postgresql.conf cert/key when ssl_sni is off or
   pg_hosts.conf is missing/empty,
 - per-host certificate/CA selection (including hostname lists and
   @-file inclusion, case-insensitive matching),
 - rejection of malformed/duplicate pg_hosts.conf entries (server fails to
   start),
 - password-protected per-host keys and passphrase-command reload behaviour,
 - non-SNI-only (/no_sni/) host entries, and
 - per-host client-CA selection.

Connections are made over TCP to 127.0.0.1 (via hostaddr) while the libpq
``host`` keyword carries the SNI hostname, so SNI is sent to the server while
the actual connection always lands on 127.0.0.1.
"""

import os

import pytest


# This is the hostaddr used to connect to the server.  This cannot be a
# hostname, because the server certificate is always for the domain
# postgresql-ssl-regression.test.
SERVERHOSTADDR = "127.0.0.1"
# This is the pattern to use in pg_hba.conf to match incoming connections.
SERVERHOSTCIDR = "127.0.0.1/32"

# The directory holding the test certificates and keys.
SSL_DIR = os.path.join(os.path.dirname(__file__), "..", "ssl").replace("\\", "/")


def _restart_fails(node):
    """Restart the node, expecting failure; return True if it came back up.

    The framework's restart() raises on failure, so do a stop followed by a
    fail-tolerant start and report whether it started.
    """
    node.stop(mode="fast", fail_ok=True)
    return node.start(fail_ok=True)


def test_004_sni(create_pg, ssl_server):
    # ssl_server fixture skips unless this build uses OpenSSL.
    if "ssl" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip("Potentially unsafe test SSL not enabled in PG_TEST_EXTRA")

    if ssl_server.is_libressl():
        pytest.skip("SNI not supported when building with LibreSSL")

    node = create_pg("primary")

    exec_backend = node.safe_sql("SHOW debug_exec_backend")

    ssl_server.configure_test_server_for_ssl(
        node, SERVERHOSTADDR, SERVERHOSTCIDR, "trust"
    )

    ssl_server.switch_server_cert(node, certfile="server-cn-only")

    connstr = f"user=ssltestuser dbname=trustdb hostaddr={SERVERHOSTADDR} sslsni=1"

    ##########################################################################
    # postgresql.conf
    ##########################################################################

    # Connect without any hosts configured in pg_hosts.conf, thus using the
    # cert and key in postgresql.conf.  pg_hosts.conf exists at this point but
    # is empty apart from the comments stemming from the sample.
    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require",
        "pg.conf: connect with correct server CA cert file sslmode=require",
    )

    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root_ca.crt sslmode=verify-ca",
        "pg.conf: connect fails without intermediate for sslmode=verify-ca",
        expected_stderr=r"certificate verify failed",
    )

    # Add an entry in pg_hosts.conf with no default, and reload.  Since
    # ssl_sni is still 'off' we should still be able to connect using the
    # certificates in postgresql.conf
    node.append_conf(
        "example.org server-cn-only.crt server-cn-only.key",
        filename="pg_hosts.conf",
    )
    node.reload()
    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require",
        "pg.conf: connect with correct server CA cert file sslmode=require",
    )

    # Turn on SNI support and remove pg_hosts.conf and reload to make sure a
    # missing file is treated like an empty file.
    node.append_conf("ssl_sni = on")
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.reload()

    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require",
        "pg.conf: connect after deleting pg_hosts.conf",
    )

    ##########################################################################
    # pg_hosts.conf
    ##########################################################################

    # Replicate the postgresql.conf configuration into pg_hosts.conf and retry
    # the same tests as above.
    node.append_conf(
        "* server-cn-only.crt server-cn-only.key",
        filename="pg_hosts.conf",
    )
    node.reload()

    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require",
        "pg_hosts.conf: connect to default, with correct server CA cert file "
        "sslmode=require",
    )

    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root_ca.crt sslmode=verify-ca",
        "pg_hosts.conf: connect to default, fail without intermediate for "
        "sslmode=verify-ca",
        expected_stderr=r"certificate verify failed",
    )

    # Add host entry for example.org which serves the server cert and its
    # intermediate CA.  The previously existing default host still exists
    # without a CA.
    node.append_conf(
        "example.org server-cn-only+server_ca.crt server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    node.reload()

    node.connect_ok(
        f"{connstr} host=example.org sslrootcert={SSL_DIR}/root_ca.crt "
        "sslmode=verify-ca",
        "pg_hosts.conf: connect to example.org and verify server CA",
    )

    node.connect_ok(
        f"{connstr} host=Example.ORG sslrootcert={SSL_DIR}/root_ca.crt "
        "sslmode=verify-ca",
        "pg_hosts.conf: connect to Example.ORG and verify server CA",
    )

    node.connect_fails(
        f"{connstr} host=example.org sslrootcert=invalid sslmode=verify-ca",
        "pg_hosts.conf: connect to example.org but without server root cert, "
        "sslmode=verify-ca",
        expected_stderr=r'root certificate file "invalid" does not exist',
    )

    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root_ca.crt sslmode=verify-ca",
        "pg_hosts.conf: connect to default and fail to verify CA",
        expected_stderr=r"certificate verify failed",
    )

    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require",
        "pg_hosts.conf: connect to default with sslmode=require",
    )

    # Use multiple hostnames for a single configuration
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "example.org,example.com,example.net server-cn-only+server_ca.crt "
        "server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    node.reload()

    node.connect_ok(
        f"{connstr} host=example.org sslrootcert={SSL_DIR}/root_ca.crt "
        "sslmode=verify-ca",
        "pg_hosts.conf: connect to example.org and verify server CA",
    )
    node.connect_ok(
        f"{connstr} host=example.com sslrootcert={SSL_DIR}/root_ca.crt "
        "sslmode=verify-ca",
        "pg_hosts.conf: connect to example.com and verify server CA",
    )
    node.connect_ok(
        f"{connstr} host=example.net sslrootcert={SSL_DIR}/root_ca.crt "
        "sslmode=verify-ca",
        "pg_hosts.conf: connect to example.net and verify server CA",
    )
    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "host=example.se",
        "pg_hosts.conf: connect to default with sslmode=require",
        expected_stderr=r"unrecognized name",
    )

    # Test @-inclusion of hostnames.
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "example.org,@hostnames.txt server-cn-only+server_ca.crt "
        "server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    node.append_conf(
        "\nexample.com\nexample.net\n",
        filename="hostnames.txt",
    )
    node.reload()

    node.connect_ok(
        f"{connstr} host=example.org sslrootcert={SSL_DIR}/root_ca.crt "
        "sslmode=verify-ca",
        "@hostnames.txt: connect to example.org and verify server CA",
    )
    node.connect_ok(
        f"{connstr} host=example.com sslrootcert={SSL_DIR}/root_ca.crt "
        "sslmode=verify-ca",
        "@hostnames.txt: connect to example.com and verify server CA",
    )
    node.connect_ok(
        f"{connstr} host=example.net sslrootcert={SSL_DIR}/root_ca.crt "
        "sslmode=verify-ca",
        "@hostnames.txt: connect to example.net and verify server CA",
    )
    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "host=example.se",
        "@hostnames.txt: connect to default with sslmode=require",
        expected_stderr=r"unrecognized name",
    )

    # Add an incorrect entry specifying a default entry combined with hostnames
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "example.org,*,example.net server-cn-only+server_ca.crt "
        "server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    assert not _restart_fails(
        node
    ), "pg_hosts.conf: restart fails with default entry combined with hostnames"

    # Add incorrect duplicate entries.
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "\n* server-cn-only.crt server-cn-only.key\n"
        "* server-cn-only.crt server-cn-only.key\n",
        filename="pg_hosts.conf",
    )
    assert not _restart_fails(
        node
    ), "pg_hosts.conf: restart fails with two default entries"

    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "\n/no_sni/ server-cn-only.crt server-cn-only.key\n"
        "/no_sni/ server-cn-only.crt server-cn-only.key\n",
        filename="pg_hosts.conf",
    )
    assert not _restart_fails(
        node
    ), "pg_hosts.conf: restart fails with two no_sni entries"

    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "\nexample.org server-cn-only.crt server-cn-only.key\n"
        "example.net server-cn-only.crt server-cn-only.key\n"
        "example.org server-cn-only.crt server-cn-only.key\n",
        filename="pg_hosts.conf",
    )
    assert not _restart_fails(
        node
    ), "pg_hosts.conf: restart fails with two identical hostname entries"

    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "\nexample.org server-cn-only.crt server-cn-only.key\n"
        "example.net,example.com,Example.org server-cn-only.crt "
        "server-cn-only.key\n",
        filename="pg_hosts.conf",
    )
    assert not _restart_fails(
        node
    ), "pg_hosts.conf: restart fails with two identical hostname entries in lists"

    # Modify pg_hosts.conf to no longer have the default host entry.
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "example.org server-cn-only+server_ca.crt server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    node.restart()

    # Connecting without a hostname as well as with a hostname which isn't in
    # the pg_hosts configuration should fail.
    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "sslsni=0",
        "pg_hosts.conf: connect to default with sslmode=require",
        expected_stderr=r"handshake failure",
    )
    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "host=example.com",
        "pg_hosts.conf: connect to default with sslmode=require",
        expected_stderr=r"unrecognized name",
    )
    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "host=example",
        "pg_hosts.conf: connect to 'example' with sslmode=require",
        expected_stderr=r"unrecognized name",
    )

    # Reconfigure with broken configuration for the key passphrase, the server
    # should not start up
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "localhost server-cn-only.crt server-password.key "
        'root+client_ca.crt "echo wrongpassword" on',
        filename="pg_hosts.conf",
    )
    assert not _restart_fails(node), (
        "pg_hosts.conf: restart fails with password-protected key when using "
        "the wrong passphrase command"
    )

    # Reconfigure again but with the correct passphrase set
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "localhost server-cn-only.crt server-password.key "
        'root+client_ca.crt "echo secret1" on',
        filename="pg_hosts.conf",
    )
    assert _restart_fails(node), (
        "pg_hosts.conf: restart succeeds with password-protected key when "
        "using the correct passphrase command"
    )

    # Make sure connecting works, and try to stress the reload logic by issuing
    # subsequent reloads
    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "host=localhost",
        "pg_hosts.conf: connect with correct server CA cert file sslmode=require",
    )
    node.reload()
    node.reload()
    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "host=localhost",
        "pg_hosts.conf: connect with correct server CA cert file after reloads",
    )
    node.reload()
    node.reload()
    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "host=localhost",
        "pg_hosts.conf: connect with correct server CA cert file after more reloads",
    )

    # Test reloading a passphrase protected key without reloading support in
    # the passphrase hook.  Restarting should not give any errors in the log,
    # but the subsequent reload should fail with an error regarding reloading.
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "localhost server-cn-only.crt server-password.key "
        'root+client_ca.crt "echo secret1" off',
        filename="pg_hosts.conf",
    )
    node_loglocation = node.log_position()
    assert _restart_fails(node), (
        "pg_hosts.conf: restart succeeds with password-protected key when "
        "using the correct passphrase command"
    )
    log = node.log_content()[node_loglocation:]
    assert (
        "cannot be reloaded because it requires a passphrase" not in log
    ), "log reload failure due to passphrase command reloading"

    # Passphrase reloads must be enabled on Windows (and EXEC_BACKEND) to
    # succeed even without a restart
    if exec_backend != "on":
        node.connect_ok(
            f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt "
            "sslmode=require host=localhost",
            "pg_hosts.conf: connect with correct server CA cert file "
            "sslmode=require",
        )
        # Reloading should fail since the passphrase cannot be reloaded, with
        # an error recorded in the log.  Since we keep existing contexts around
        # it should still work.
        node_loglocation = node.log_position()
        node.reload()
        node.connect_ok(
            f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt "
            "sslmode=require host=localhost",
            "pg_hosts.conf: connect with correct server CA cert file "
            "sslmode=require",
        )
        node.wait_for_log(
            r"cannot be reloaded because it requires a passphrase",
            node_loglocation,
        )

    # Configure with only non-SNI connections allowed
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    node.append_conf(
        "/no_sni/ server-cn-only.crt server-cn-only.key",
        filename="pg_hosts.conf",
    )
    node.restart()

    node.connect_ok(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "sslsni=0",
        "pg_hosts.conf: only non-SNI connections allowed",
    )

    node.connect_fails(
        f"{connstr} sslrootcert={SSL_DIR}/root+server_ca.crt sslmode=require "
        "host=example.org",
        "pg_hosts.conf: only non-SNI connections allowed, connecting with SNI",
        expected_stderr=r"unrecognized name",
    )

    # Test client CAs

    # pg_hosts configuration
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))

    # Neither ssl_ca_file nor the default host should have any effect
    # whatsoever on the following tests.
    node.append_conf("ssl_ca_file = 'root+client_ca.crt'")
    node.append_conf(
        "* server-cn-only.crt server-cn-only.key root+client_ca.crt",
        filename="pg_hosts.conf",
    )

    # example.org has an unconfigured CA.
    node.append_conf(
        "example.org server-cn-only.crt server-cn-only.key",
        filename="pg_hosts.conf",
    )
    # example.com uses the client CA.
    node.append_conf(
        "example.com server-cn-only.crt server-cn-only.key root+client_ca.crt",
        filename="pg_hosts.conf",
    )
    # example.net uses the server CA (which is wrong).
    node.append_conf(
        "example.net server-cn-only.crt server-cn-only.key root+server_ca.crt",
        filename="pg_hosts.conf",
    )

    node.restart()

    connstr = (
        f"user=ssltestuser dbname=certdb hostaddr={SERVERHOSTADDR} "
        "sslmode=require sslsni=1"
    )

    # example.org is unconfigured and should fail.
    node.connect_fails(
        f"{connstr} host=example.org sslcertmode=require "
        f"sslcert={SSL_DIR}/client.crt" + ssl_server.sslkey("client.key"),
        "host: 'example.org', ca: '': connect with sslcert, no client CA configured",
        expected_stderr=r"client certificates can only be checked if a root "
        r"certificate store is available",
    )

    # example.com is configured and should require a valid client cert.
    node.connect_fails(
        f"{connstr} host=example.com sslcertmode=disable",
        "host: 'example.com', ca: 'root+client_ca.crt': connect fails if no "
        "client certificate sent",
        expected_stderr=r"connection requires a valid client certificate",
    )

    node.connect_ok(
        f"{connstr} host=example.com sslcertmode=require "
        f"sslcert={SSL_DIR}/client.crt" + ssl_server.sslkey("client.key"),
        "host: 'example.com', ca: 'root+client_ca.crt': connect with sslcert, "
        "client certificate sent",
    )

    # example.net is configured and should require a client cert, but will
    # always fail verification.
    node.connect_fails(
        f"{connstr} host=example.net sslcertmode=disable",
        "host: 'example.net', ca: 'root+server_ca.crt': connect fails if no "
        "client certificate sent",
        expected_stderr=r"connection requires a valid client certificate",
    )

    node.connect_fails(
        f"{connstr} host=example.net sslcertmode=require "
        f"sslcert={SSL_DIR}/client.crt" + ssl_server.sslkey("client.key"),
        "host: 'example.net', ca: 'root+server_ca.crt': connect with sslcert, "
        "client certificate sent",
        expected_stderr=r"unknown ca",
    )

    # Make sure the global CRL dir interacts properly with per-host trust.
    ssl_server.switch_server_cert(
        node,
        certfile="server-cn-only",
        crldir="client-crldir",
    )

    node.connect_fails(
        f"{connstr} host=example.com sslcertmode=require "
        f"sslcert={SSL_DIR}/client-revoked.crt"
        + ssl_server.sslkey("client-revoked.key"),
        "host: 'example.com', ca: 'root+client_ca.crt': connect fails with "
        "revoked client cert",
        expected_stderr=r"certificate revoked",
    )

    # pg_hosts configuration with useless data at EOL
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    # example.org has an unconfigured CA.
    node.append_conf(
        "example.org server-cn-only.crt server-cn-only.key "
        'root+client_ca.crt "cmd" on TRAILING_TEXT MORE_TEXT',
        filename="pg_hosts.conf",
    )
    assert not _restart_fails(
        node
    ), "pg_hosts.conf: restart fails with extra data at EOL"
    # pg_hosts configuration with useless data at EOL
    os.unlink(os.path.join(node.data_dir, "pg_hosts.conf"))
    # example.org has an unconfigured CA.
    node.append_conf(
        "example.org server-cn-only.crt server-cn-only.key "
        'root+client_ca.crt "cmd" notabooleanvalue',
        filename="pg_hosts.conf",
    )
    assert not _restart_fails(
        node
    ), "pg_hosts.conf: restart fails with non-boolean value in boolean field"
