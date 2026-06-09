# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""The main SSL test.

Exercises server cert/key/CRL variations (via switch_server_cert), client
certificate authentication, and the libpq
sslmode/sslnegotiation/sslrootcert/sslcert/sslkey options, CRL directory vs
file, verify-ca/verify-full host name matching, and channel binding.

Connections are made in-process through libpq (node.connect_ok /
connect_fails); psql is forked only for the two pg_stat_ssl command_like
checks.

The SSL helper (pypg.ssl_server.SSLServer, exposed by the ``ssl_server``
fixture) installs the server certs into the data directory and copies the
client keys to a private-perms temp dir.  Client cert files referenced in
connection strings are given as absolute paths under src/test/ssl/ssl; the key
fragment comes from ssl_server.sslkey() so libpq sees the right permissions.
"""

import os
import subprocess
import sys

import pytest

# Path to src/test/ssl/ssl, where the cert/key/CRL files live.
_SSL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ssl"))


def _ssl(relpath):
    """Absolute path to a file under src/test/ssl/ssl.

    We expand to an absolute path so the working directory does not matter.
    """
    # Forward slashes: these paths go into conninfo, where libpq treats a
    # backslash as an escape character.
    return os.path.join(_SSL_DIR, relpath).replace("\\", "/")


def test_001_ssltests(create_pg, ssl_server):
    # The ssl_server fixture already skips unless with_ssl=openssl.
    if "ssl" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip("Potentially unsafe test SSL not enabled in PG_TEST_EXTRA")

    windows_os = sys.platform == "win32"

    # Determine whether this build uses OpenSSL or LibreSSL.
    libressl = ssl_server.is_libressl()

    # This is the hostname used to connect to the server.  This cannot be a
    # hostname, because the server certificate is always for the domain
    # postgresql-ssl-regression.test.
    SERVERHOSTADDR = "127.0.0.1"
    # This is the pattern to use in pg_hba.conf to match incoming connections.
    SERVERHOSTCIDR = "127.0.0.1/32"

    # Determine whether build supports sslcertmode=require.
    supports_sslcertmode_require = ssl_server._check_pg_config(
        "#define HAVE_SSL_CTX_SET_CERT_CB 1")
    # Determine whether build supports IPv6 in certificates.
    have_inet_pton = ssl_server._check_pg_config("#define HAVE_INET_PTON 1")

    # Set of default settings for SSL parameters in connection string.  This
    # makes the tests protected against any defaults the environment may have
    # in ~/.postgresql/.
    default_ssl_connstr = (
        "sslkey=invalid sslcert=invalid sslrootcert=invalid "
        "sslcrl=invalid sslcrldir=invalid")

    # Allocation of base connection string shared among multiple tests.
    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"hostaddr={SERVERHOSTADDR} host=common-name.pg-ssltest.test")

    #### Set up the server.

    node = create_pg("primary", start=False)
    # Needed to allow connect_fails to inspect postmaster log:
    node.append_conf("log_min_messages = debug2")
    node.start()

    # Run this before we lock down access below.
    result = node.safe_sql("SHOW ssl_library")
    assert result == ssl_server.ssl_library(), "ssl_library parameter"

    exec_backend = node.safe_sql("SHOW debug_exec_backend").strip()

    ssl_server.configure_test_server_for_ssl(
        node, SERVERHOSTADDR, SERVERHOSTCIDR, "trust")

    def switch_server_cert(*args, **kwargs):
        ssl_server.switch_server_cert(node, *args, **kwargs)

    def sslkey(keyfile):
        return ssl_server.sslkey(keyfile)

    def restart_check():
        """Emulate $node->restart(fail_ok => 1): stop then start.

        Returns True if the server came back up, False otherwise.  pg_ctl
        restart cannot tolerate a server that fails to start, so this performs
        a fail-tolerant stop followed by a fail-tolerant start.
        """
        node.stop("fast", fail_ok=True)
        return node.start(fail_ok=True)

    # ---- testing password-protected keys ----------------------------------

    # Test a passphrase command which fails to unlock the private key, the
    # server should not start at all.
    switch_server_cert(
        certfile="server-cn-only",
        cafile="root+client_ca",
        keyfile="server-password",
        passphrase_cmd="echo wrongpassword",
        restart=False)

    # restart should fail (wrong password).
    node.stop("fast", fail_ok=True)
    log_pos = node.log_position()
    started = node.start(fail_ok=True)
    assert not started, (
        "restart fails with password-protected key file with wrong password")
    assert node.log_contains(r"could not load private key file", log_pos)
    # The failed start above leaves the server down; the next switch+restart
    # picks it up from there.

    # Test a passphrase command which successfully unlocks the private key but
    # which doesn't support reloading.  Unlocking the private key will fail
    # when reloading and the already existing SSL context will remain in place,
    # with connections still accepted.  EXEC_BACKEND builds will reload the SSL
    # context on each backend startup, so command reloading must be enabled or
    # else connections will fail.
    switch_server_cert(
        certfile="server-cn-only",
        cafile="root+client_ca",
        keyfile="server-password",
        passphrase_cmd="echo secret1",
        passphrase_cmd_reload="off",
        restart=False)

    log_pos = node.log_position()
    started = restart_check()
    assert started, "restart succeeds with password-protected key file"
    assert not node.log_contains(r"could not load private key file", log_pos)

    if "on" in exec_backend:
        node.connect_fails(
            f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require",
            "connect with correct server CA cert file sslmode=require",
            expected_stderr=r"server does not support SSL")
    else:
        node.connect_ok(
            f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require",
            "connect with correct server CA cert file sslmode=require")

    # Reloading should fail since we cannot execute the passphrase command
    node.reload()
    log_start = node.wait_for_log(
        r"cannot be reloaded because it requires a passphrase")

    # Test a passphrase command which successfully unlocks the private key, and
    # which can be reloaded.  The server should start and connections be
    # accepted.
    switch_server_cert(
        certfile="server-cn-only",
        cafile="root+client_ca",
        keyfile="server-password",
        passphrase_cmd="echo secret1",
        passphrase_cmd_reload="on",
        restart=False)

    log_pos = node.log_position()
    started = restart_check()
    assert started, "restart succeeds with password-protected key file"
    assert not node.log_contains(r"could not load private key file", log_pos)
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require",
        "connect with correct server CA cert file sslmode=require")

    # Reloading the config should execute the passphrase reload command and
    # successfully reload the private key.
    node.reload()
    log_start = node.wait_for_log(r"reloading configuration files", log_start)
    node.log_check(
        "passphrase could reload private key",
        log_start,
        log_unlike=[r"cannot be reloaded because it requires a passphrase"])
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require",
        "connect with correct server CA cert file sslmode=require")

    # Test compatibility of SSL protocols.
    # TLSv1.1 is lower than TLSv1.2, so it won't work.
    node.append_conf(
        "ssl_min_protocol_version='TLSv1.2'\n"
        "ssl_max_protocol_version='TLSv1.1'\n")
    started = restart_check()
    assert not started, "restart fails with incorrect SSL protocol bounds"

    # Go back to the defaults, this works.
    node.append_conf(
        "ssl_min_protocol_version='TLSv1.2'\n"
        "ssl_max_protocol_version=''\n")
    started = restart_check()
    assert started, "restart succeeds with correct SSL protocol bounds"

    # Test parsing colon-separated groups.  Resetting to a default value to
    # clear the error is fine since the call to switch_server_cert in the
    # client side tests will overwrite ssl_groups with a known set of groups.
    node.append_conf("ssl_groups='bad:value'", filename="sslconfig.conf")
    log_pos = node.log_position()
    started = restart_check()
    assert not started, "restart fails with incorrect groups"
    assert not node.log_contains(r"no SSL error reported", log_pos), \
        "error message translated"
    node.append_conf("ssl_groups='prime256v1'", filename="ssl_config.conf")
    restart_check()

    # ---- Run client-side tests --------------------------------------------
    #
    # Test that libpq accepts/rejects the connection correctly, depending on
    # sslmode and whether the server's certificate looks correct.  No client
    # certificate is used in these tests.

    switch_server_cert(certfile="server-cn-only")

    if not libressl:
        # Keylogging is not supported with LibreSSL.
        tempdir = str(node.basedir)

        # Connect should work with a given sslkeylogfile
        keytxt = os.path.join(tempdir, "key.txt")
        node.connect_ok(
            f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} "
            f"sslkeylogfile={keytxt} sslmode=require",
            f"connect with server root cert and sslkeylogfile={keytxt}")

        # Verify the key file exists
        assert os.path.isfile(keytxt), f"keylog file exists at: {keytxt}"

        # Skip permission checks on Windows/Cygwin
        if not windows_os:
            status = os.stat(keytxt)
            assert not (status.st_mode & 0o006), \
                "keylog file is not world readable"

        # Connect should work with an incorrect sslkeylogfile, with the error
        # to open the logfile printed to stderr
        node.connect_ok(
            f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} "
            f"sslkeylogfile={tempdir}/invalid/key.txt sslmode=require",
            "connect with server root cert and incorrect sslkeylogfile path",
            expected_stderr=r"could not open")

    # The server should not accept non-SSL connections.
    node.connect_fails(
        f"{common_connstr} sslmode=disable",
        "server doesn't accept non-SSL connections",
        expected_stderr=r"no pg_hba\.conf entry")

    # Try without a root cert.  In sslmode=require, this should work.  In
    # verify-ca or verify-full mode it should fail.
    node.connect_ok(
        f"{common_connstr} sslrootcert=invalid sslmode=require",
        "connect without server root cert sslmode=require")
    node.connect_fails(
        f"{common_connstr} sslrootcert=invalid sslmode=verify-ca",
        "connect without server root cert sslmode=verify-ca",
        expected_stderr=r'root certificate file "invalid" does not exist')
    node.connect_fails(
        f"{common_connstr} sslrootcert=invalid sslmode=verify-full",
        "connect without server root cert sslmode=verify-full",
        expected_stderr=r'root certificate file "invalid" does not exist')

    # Try with wrong root cert, should fail.  (We're using the client CA as the
    # root, but the server's key is signed by the server CA.)
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('client_ca.crt')} sslmode=require",
        "connect with wrong server root cert sslmode=require",
        expected_stderr=r"SSL error: certificate verify failed")
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('client_ca.crt')} sslmode=verify-ca",
        "connect with wrong server root cert sslmode=verify-ca",
        expected_stderr=r"SSL error: certificate verify failed")
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('client_ca.crt')} sslmode=verify-full",
        "connect with wrong server root cert sslmode=verify-full",
        expected_stderr=r"SSL error: certificate verify failed")

    # Try with just the server CA's cert.  This fails because the root file
    # must contain the whole chain up to the root CA.
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('server_ca.crt')} sslmode=verify-ca",
        "connect with server CA cert, without root CA",
        expected_stderr=r"SSL error: certificate verify failed")

    # And finally, with the correct root cert.
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require",
        "connect with correct server CA cert file sslmode=require")
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca",
        "connect with correct server CA cert file sslmode=verify-ca")
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-full",
        "connect with correct server CA cert file sslmode=verify-full")

    # Test with cert root file that contains two certificates.  The client
    # should be able to pick the right one, regardless of the order in the file.
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('both-cas-1.crt')} sslmode=verify-ca",
        "cert root file that contains two certificates, order 1")
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('both-cas-2.crt')} sslmode=verify-ca",
        "cert root file that contains two certificates, order 2")

    # sslcertmode=allow and disable should both work without a client cert.
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require sslcertmode=disable",
        "connect with sslcertmode=disable")
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require sslcertmode=allow",
        "connect with sslcertmode=allow")

    # sslcertmode=require, however, should fail.
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require sslcertmode=require",
        "connect with sslcertmode=require fails without a client certificate",
        expected_stderr=(
            r"server accepted connection without a valid SSL certificate"
            if supports_sslcertmode_require
            else r'sslcertmode value "require" is not supported'))

    # CRL tests

    # Invalid CRL filename is the same as no CRL, succeeds
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca sslcrl=invalid",
        "sslcrl option with invalid file name")

    # A CRL belonging to a different CA is not accepted, fails
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca sslcrl={_ssl('client.crl')}",
        "CRL belonging to a different CA",
        expected_stderr=r"SSL error: certificate verify failed")

    # The same for CRL directory.  sslcrl='' is added here to override the
    # invalid default, so as this does not interfere with this case.
    node.connect_fails(
        f"{common_connstr} sslcrl='' sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca sslcrldir={_ssl('client-crldir')}",
        "directory CRL belonging to a different CA",
        expected_stderr=r"SSL error: certificate verify failed")

    # With the correct CRL, succeeds (this cert is not revoked)
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca sslcrl={_ssl('root+server.crl')}",
        "CRL with a non-revoked cert")

    # The same for CRL directory
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca sslcrldir={_ssl('root+server-crldir')}",
        "directory CRL with a non-revoked cert")

    # Check that connecting with verify-full fails, when the hostname doesn't
    # match the hostname in the server's certificate.
    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"sslrootcert={_ssl('root+server_ca.crt')} hostaddr={SERVERHOSTADDR}")

    node.connect_ok(
        f"{common_connstr} sslmode=require host=wronghost.test",
        "mismatch between host name and server certificate sslmode=require")
    node.connect_ok(
        f"{common_connstr} sslmode=verify-ca host=wronghost.test",
        "mismatch between host name and server certificate sslmode=verify-ca")
    node.connect_fails(
        f"{common_connstr} sslmode=verify-full host=wronghost.test",
        "mismatch between host name and server certificate sslmode=verify-full",
        expected_stderr=(
            r'server certificate for "common-name\.pg-ssltest\.test" does '
            r'not match host name "wronghost\.test"'))

    # Test with an IP address in the Common Name.  This is a strange corner
    # case that nevertheless is supported, as long as the address string
    # matches exactly.
    switch_server_cert(certfile="server-ip-cn-only")

    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"sslrootcert={_ssl('root+server_ca.crt')} hostaddr={SERVERHOSTADDR} "
        "sslmode=verify-full")

    node.connect_ok(
        f"{common_connstr} host=192.0.2.1 sslsni=0",
        "IP address in the Common Name")

    node.connect_fails(
        f"{common_connstr} host=192.000.002.001 sslsni=0",
        "mismatch between host name and server certificate IP address",
        expected_stderr=(
            r'server certificate for "192\.0\.2\.1" does not match host name '
            r'"192\.000\.002\.001"'))

    # Similarly, we'll also match an IP address in a dNSName SAN.  (This is
    # long-standing behavior.)
    switch_server_cert(certfile="server-ip-in-dnsname")

    node.connect_ok(
        f"{common_connstr} host=192.0.2.1 sslsni=0",
        "IP address in a dNSName")

    # Test Subject Alternative Names.
    switch_server_cert(certfile="server-multiple-alt-names")

    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"sslrootcert={_ssl('root+server_ca.crt')} hostaddr={SERVERHOSTADDR} "
        "sslmode=verify-full")

    node.connect_ok(
        f"{common_connstr} host=dns1.alt-name.pg-ssltest.test",
        "host name matching with X.509 Subject Alternative Names 1")
    node.connect_ok(
        f"{common_connstr} host=dns2.alt-name.pg-ssltest.test",
        "host name matching with X.509 Subject Alternative Names 2")
    node.connect_ok(
        f"{common_connstr} host=foo.wildcard.pg-ssltest.test",
        "host name matching with X.509 Subject Alternative Names wildcard")

    node.connect_fails(
        f"{common_connstr} host=wronghost.alt-name.pg-ssltest.test",
        "host name not matching with X.509 Subject Alternative Names",
        expected_stderr=(
            r'server certificate for "dns1\.alt-name\.pg-ssltest\.test" '
            r'\(and 2 other names\) does not match host name '
            r'"wronghost\.alt-name\.pg-ssltest\.test"'))
    node.connect_fails(
        f"{common_connstr} host=deep.subdomain.wildcard.pg-ssltest.test",
        "host name not matching with X.509 Subject Alternative Names wildcard",
        expected_stderr=(
            r'server certificate for "dns1\.alt-name\.pg-ssltest\.test" '
            r'\(and 2 other names\) does not match host name '
            r'"deep\.subdomain\.wildcard\.pg-ssltest\.test"'))

    # Test certificate with a single Subject Alternative Name.  (this gives a
    # slightly different error message, that's all)
    switch_server_cert(certfile="server-single-alt-name")

    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"sslrootcert={_ssl('root+server_ca.crt')} hostaddr={SERVERHOSTADDR} "
        "sslmode=verify-full")

    node.connect_ok(
        f"{common_connstr} host=single.alt-name.pg-ssltest.test",
        "host name matching with a single X.509 Subject Alternative Name")

    node.connect_fails(
        f"{common_connstr} host=wronghost.alt-name.pg-ssltest.test",
        "host name not matching with a single X.509 Subject Alternative Name",
        expected_stderr=(
            r'server certificate for "single\.alt-name\.pg-ssltest\.test" '
            r'does not match host name "wronghost\.alt-name\.pg-ssltest\.test"'))
    node.connect_fails(
        f"{common_connstr} host=deep.subdomain.wildcard.pg-ssltest.test",
        "host name not matching with a single X.509 Subject Alternative Name wildcard",
        expected_stderr=(
            r'server certificate for "single\.alt-name\.pg-ssltest\.test" '
            r'does not match host name '
            r'"deep\.subdomain\.wildcard\.pg-ssltest\.test"'))

    if have_inet_pton:
        # Test certificate with IP addresses in the SANs.
        switch_server_cert(certfile="server-ip-alt-names")

        node.connect_ok(
            f"{common_connstr} host=192.0.2.1",
            "host matching an IPv4 address (Subject Alternative Name 1)")

        node.connect_ok(
            f"{common_connstr} host=192.000.002.001",
            "host matching an IPv4 address in alternate form (Subject Alternative Name 1)")

        node.connect_fails(
            f"{common_connstr} host=192.0.2.2",
            "host not matching an IPv4 address (Subject Alternative Name 1)",
            expected_stderr=(
                r'server certificate for "192\.0\.2\.1" \(and 1 other name\) '
                r'does not match host name "192\.0\.2\.2"'))

        node.connect_ok(
            f"{common_connstr} host=2001:DB8::1",
            "host matching an IPv6 address (Subject Alternative Name 2)")

        node.connect_ok(
            f"{common_connstr} host=2001:db8:0:0:0:0:0:1",
            "host matching an IPv6 address in alternate form (Subject Alternative Name 2)")

        node.connect_ok(
            f"{common_connstr} host=2001:db8::0.0.0.1",
            "host matching an IPv6 address in mixed form (Subject Alternative Name 2)")

        node.connect_fails(
            f"{common_connstr} host=::1",
            "host not matching an IPv6 address (Subject Alternative Name 2)",
            expected_stderr=(
                r'server certificate for "192\.0\.2\.1" \(and 1 other name\) '
                r'does not match host name "::1"'))

        node.connect_fails(
            f"{common_connstr} host=2001:DB8::1/128",
            "IPv6 host with CIDR mask does not match",
            expected_stderr=(
                r'server certificate for "192\.0\.2\.1" \(and 1 other name\) '
                r'does not match host name "2001:DB8::1/128"'))

    # Test server certificate with a CN and DNS SANs.  Per RFCs 2818 and 6125,
    # the CN should be ignored when the certificate has both.
    switch_server_cert(certfile="server-cn-and-alt-names")

    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"sslrootcert={_ssl('root+server_ca.crt')} hostaddr={SERVERHOSTADDR} "
        "sslmode=verify-full")

    node.connect_ok(
        f"{common_connstr} host=dns1.alt-name.pg-ssltest.test",
        "certificate with both a CN and SANs 1")
    node.connect_ok(
        f"{common_connstr} host=dns2.alt-name.pg-ssltest.test",
        "certificate with both a CN and SANs 2")
    node.connect_fails(
        f"{common_connstr} host=common-name.pg-ssltest.test",
        "certificate with both a CN and SANs ignores CN",
        expected_stderr=(
            r'server certificate for "dns1\.alt-name\.pg-ssltest\.test" '
            r'\(and 1 other name\) does not match host name '
            r'"common-name\.pg-ssltest\.test"'))

    if have_inet_pton:
        # But we will fall back to check the CN if the SANs contain only IP
        # addresses.
        switch_server_cert(certfile="server-cn-and-ip-alt-names")

        node.connect_ok(
            f"{common_connstr} host=common-name.pg-ssltest.test",
            "certificate with both a CN and IP SANs matches CN")
        node.connect_ok(
            f"{common_connstr} host=192.0.2.1",
            "certificate with both a CN and IP SANs matches SAN 1")
        node.connect_ok(
            f"{common_connstr} host=2001:db8::1",
            "certificate with both a CN and IP SANs matches SAN 2")

        # And now the same tests, but with IP addresses and DNS names swapped.
        switch_server_cert(certfile="server-ip-cn-and-alt-names")

        node.connect_ok(
            f"{common_connstr} host=192.0.2.2",
            "certificate with both an IP CN and IP SANs 1")
        node.connect_ok(
            f"{common_connstr} host=2001:db8::1",
            "certificate with both an IP CN and IP SANs 2")
        node.connect_fails(
            f"{common_connstr} host=192.0.2.1",
            "certificate with both an IP CN and IP SANs ignores CN",
            expected_stderr=(
                r'server certificate for "192\.0\.2\.2" \(and 1 other name\) '
                r'does not match host name "192\.0\.2\.1"'))

    switch_server_cert(certfile="server-ip-cn-and-dns-alt-names")

    node.connect_ok(
        f"{common_connstr} host=192.0.2.1",
        "certificate with both an IP CN and DNS SANs matches CN")
    node.connect_ok(
        f"{common_connstr} host=dns1.alt-name.pg-ssltest.test",
        "certificate with both an IP CN and DNS SANs matches SAN 1")
    node.connect_ok(
        f"{common_connstr} host=dns2.alt-name.pg-ssltest.test",
        "certificate with both an IP CN and DNS SANs matches SAN 2")

    # Finally, test a server certificate that has no CN or SANs.  Of course,
    # that's not a very sensible certificate, but libpq should handle it
    # gracefully.
    switch_server_cert(certfile="server-no-names")
    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"sslrootcert={_ssl('root+server_ca.crt')} hostaddr={SERVERHOSTADDR}")

    node.connect_ok(
        f"{common_connstr} sslmode=verify-ca host=common-name.pg-ssltest.test",
        "server certificate without CN or SANs sslmode=verify-ca")
    node.connect_fails(
        f"{common_connstr} sslmode=verify-full host=common-name.pg-ssltest.test",
        "server certificate without CN or SANs sslmode=verify-full",
        expected_stderr=r"could not get server's host name from server certificate")

    # Test system trusted roots.
    switch_server_cert(
        certfile="server-cn-only+server_ca",
        keyfile="server-cn-only",
        cafile="root_ca")
    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"sslrootcert=system hostaddr={SERVERHOSTADDR}")

    # By default our custom-CA-signed certificate should not be trusted.
    # OpenSSL 3.0 reports a missing/invalid system CA as "unregistered schema"
    # instead of a failed certificate verification.
    node.connect_fails(
        f"{common_connstr} sslmode=verify-full host=common-name.pg-ssltest.test",
        "sslrootcert=system does not connect with private CA",
        expected_stderr=r"SSL error: (certificate verify failed|unregistered scheme)")

    # Modes other than verify-full cannot be mixed with sslrootcert=system.
    node.connect_fails(
        f"{common_connstr} sslmode=verify-ca host=common-name.pg-ssltest.test",
        "sslrootcert=system only accepts sslmode=verify-full",
        expected_stderr=r'weak sslmode "verify-ca" may not be used with sslrootcert=system')

    if not libressl:
        # SSL_CERT_FILE is not supported with LibreSSL.
        # We can modify the definition of "system" to get it trusted again.
        saved_cert_file = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = os.path.join(node.data_dir, "root_ca.crt")
        try:
            node.connect_ok(
                f"{common_connstr} sslmode=verify-full host=common-name.pg-ssltest.test",
                "sslrootcert=system connects with overridden SSL_CERT_FILE")

            # verify-full mode should be the default for system CAs.
            node.connect_fails(
                f"{common_connstr} host=common-name.pg-ssltest.test.bad",
                "sslrootcert=system defaults to sslmode=verify-full",
                expected_stderr=(
                    r'server certificate for "common-name\.pg-ssltest\.test" '
                    r'does not match host name '
                    r'"common-name\.pg-ssltest\.test\.bad"'))
        finally:
            if saved_cert_file is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = saved_cert_file

    # Test that the CRL works
    switch_server_cert(certfile="server-revoked")

    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=trustdb "
        f"hostaddr={SERVERHOSTADDR} host=common-name.pg-ssltest.test")

    # Without the CRL, succeeds.  With it, fails.
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca",
        "connects without client-side CRL")
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca sslcrl={_ssl('root+server.crl')}",
        "does not connect with client-side CRL file",
        expected_stderr=r"SSL error: certificate verify failed")
    # sslcrl='' is added here to override the invalid default, so as this does
    # not interfere with this case.
    node.connect_fails(
        f"{common_connstr} sslcrl='' sslrootcert={_ssl('root+server_ca.crt')} sslmode=verify-ca sslcrldir={_ssl('root+server-crldir')}",
        "does not connect with client-side CRL directory",
        expected_stderr=r"SSL error: certificate verify failed")

    # pg_stat_ssl
    node.command_like(
        [
            "psql",
            "--no-psqlrc",
            "--no-align",
            "--field-separator", ",",
            "--pset", "null=_null_",
            "--dbname", f"{common_connstr} sslrootcert=invalid",
            "--command", "SELECT * FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
        ],
        r"(?mx)^pid,ssl,version,cipher,bits,client_dn,client_serial,issuer_dn\r?\n"
        r"^\d+,t,TLSv[\d.]+,[\w-]+,\d+,_null_,_null_,_null_\r?$",
        "pg_stat_ssl view without client certificate")

    # Test min/max SSL protocol versions.
    node.connect_ok(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require ssl_min_protocol_version=TLSv1.2 ssl_max_protocol_version=TLSv1.2",
        "connection success with correct range of TLS protocol versions")
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require ssl_min_protocol_version=TLSv1.2 ssl_max_protocol_version=TLSv1.1",
        "connection failure with incorrect range of TLS protocol versions",
        expected_stderr=r"invalid SSL protocol version range")
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require ssl_min_protocol_version=incorrect_tls",
        "connection failure with an incorrect SSL protocol minimum bound",
        expected_stderr=r'invalid "ssl_min_protocol_version" value')
    node.connect_fails(
        f"{common_connstr} sslrootcert={_ssl('root+server_ca.crt')} sslmode=require ssl_max_protocol_version=incorrect_tls",
        "connection failure with an incorrect SSL protocol maximum bound",
        expected_stderr=r'invalid "ssl_max_protocol_version" value')

    # ---- Server-side tests ------------------------------------------------
    #
    # Test certificate authorization.

    common_connstr = (
        f"{default_ssl_connstr} sslrootcert={_ssl('root+server_ca.crt')} "
        f"sslmode=require dbname=certdb hostaddr={SERVERHOSTADDR} host=localhost")

    # no client cert
    node.connect_fails(
        f"{common_connstr} user=ssltestuser sslcert=invalid",
        "certificate authorization fails without client cert",
        expected_stderr=r"connection requires a valid client certificate")

    # correct client cert in unencrypted PEM
    node.connect_ok(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client.crt')}"
        + sslkey("client.key"),
        "certificate authorization succeeds with correct client cert in PEM format")

    # correct client cert in unencrypted DER
    node.connect_ok(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client.crt')}"
        + sslkey("client-der.key"),
        "certificate authorization succeeds with correct client cert in DER format")

    # correct client cert in encrypted PEM
    node.connect_ok(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client.crt')}"
        + sslkey("client-encrypted-pem.key")
        + " sslpassword='dUmmyP^#+'",
        "certificate authorization succeeds with correct client cert in encrypted PEM format")

    # correct client cert in encrypted DER
    node.connect_ok(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client.crt')}"
        + sslkey("client-encrypted-der.key")
        + " sslpassword='dUmmyP^#+'",
        "certificate authorization succeeds with correct client cert in encrypted DER format")

    # correct client cert with sslcertmode=allow or require
    if supports_sslcertmode_require:
        node.connect_ok(
            f"{common_connstr} user=ssltestuser sslcertmode=require sslcert={_ssl('client.crt')}"
            + sslkey("client.key"),
            "certificate authorization succeeds with correct client cert and sslcertmode=require")
    node.connect_ok(
        f"{common_connstr} user=ssltestuser sslcertmode=allow sslcert={_ssl('client.crt')}"
        + sslkey("client.key"),
        "certificate authorization succeeds with correct client cert and sslcertmode=allow")

    # client cert is not sent if sslcertmode=disable.
    node.connect_fails(
        f"{common_connstr} user=ssltestuser sslcertmode=disable sslcert={_ssl('client.crt')}"
        + sslkey("client.key"),
        "certificate authorization fails with correct client cert and sslcertmode=disable",
        expected_stderr=r"connection requires a valid client certificate")

    # correct client cert in encrypted PEM with wrong password
    node.connect_fails(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client.crt')}"
        + sslkey("client-encrypted-pem.key")
        + " sslpassword='wrong'",
        "certificate authorization fails with correct client cert and wrong password in encrypted PEM format",
        expected_stderr=r'private key file ".*client-encrypted-pem\.key": bad decrypt')

    # correct client cert using whole DN
    dn_connstr = f"{common_connstr} dbname=certdb_dn"

    node.connect_ok(
        f"{dn_connstr} user=ssltestuser sslcert={_ssl('client-dn.crt')}"
        + sslkey("client-dn.key"),
        "certificate authorization succeeds with DN mapping",
        log_like=[
            r'connection authenticated: identity="CN=ssltestuser-dn,OU=Testing,OU=Engineering,O=PGDG" method=cert'])

    # same thing but with a regex
    dn_connstr = f"{common_connstr} dbname=certdb_dn_re"

    node.connect_ok(
        f"{dn_connstr} user=ssltestuser sslcert={_ssl('client-dn.crt')}"
        + sslkey("client-dn.key"),
        "certificate authorization succeeds with DN regex mapping")

    # same thing but using explicit CN
    dn_connstr = f"{common_connstr} dbname=certdb_cn"

    node.connect_ok(
        f"{dn_connstr} user=ssltestuser sslcert={_ssl('client-dn.crt')}"
        + sslkey("client-dn.key"),
        "certificate authorization succeeds with CN mapping",
        # the full DN should still be used as the authenticated identity
        log_like=[
            r'connection authenticated: identity="CN=ssltestuser-dn,OU=Testing,OU=Engineering,O=PGDG" method=cert'])

    # The two encrypted-PEM-with-empty/no-password cases need Pty support and
    # are skipped here.

    # pg_stat_ssl
    #
    # If the openssl program isn't available, or fails to run, fall back to a
    # generic integer match rather than skipping the test.
    serialno = r"\d+"
    openssl = os.environ.get("OPENSSL", "")
    if openssl != "":
        try:
            serialstr = subprocess.run(
                [openssl, "x509", "-serial", "-noout", "-in", _ssl("client.crt")],
                stdout=subprocess.PIPE, text=True, check=True).stdout
            # OpenSSL prints serial numbers in hexadecimal.
            serialstr = serialstr.replace("serial=", "")
            serialstr = "".join(serialstr.split())
            serialno = str(int(serialstr, 16))
        except (subprocess.CalledProcessError, ValueError, OSError):
            serialno = r"\d+"

    node.command_like(
        [
            "psql",
            "--no-psqlrc",
            "--no-align",
            "--field-separator", ",",
            "--pset", "null=_null_",
            "--dbname",
            f"{common_connstr} user=ssltestuser sslcert={_ssl('client.crt')}"
            + sslkey("client.key"),
            "--command", "SELECT * FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
        ],
        r"(?mx)^pid,ssl,version,cipher,bits,client_dn,client_serial,issuer_dn\r?\n"
        r"^\d+,t,TLSv[\d.]+,[\w-]+,\d+,/?CN=ssltestuser," + serialno
        + r",/?CN=Test\ CA\ for\ PostgreSQL\ SSL\ regression\ test\ client\ certs\r?$",
        "pg_stat_ssl with client certificate")

    # client key with wrong permissions
    if not windows_os:
        node.connect_fails(
            f"{common_connstr} user=ssltestuser sslcert={_ssl('client.crt')}"
            + sslkey("client_wrongperms.key"),
            "certificate authorization fails because of file permissions",
            expected_stderr=r'private key file ".*client_wrongperms\.key" has group or world access')

    # client cert belonging to another user
    node.connect_fails(
        f"{common_connstr} user=anotheruser sslcert={_ssl('client.crt')}"
        + sslkey("client.key"),
        "certificate authorization fails with client cert belonging to another user",
        expected_stderr=r'certificate authentication failed for user "anotheruser"',
        # certificate authentication should be logged even on failure
        log_like=[r'connection authenticated: identity="CN=ssltestuser" method=cert'])

    # revoked client cert
    node.connect_fails(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client-revoked.crt')}"
        + sslkey("client-revoked.key"),
        "certificate authorization fails with revoked client cert",
        expected_stderr=r"SSL error: (ssl[a-z0-9/]*|tls) alert certificate revoked",
        log_like=[
            r"Client certificate verification failed at depth 0: certificate revoked",
            r'Failed certificate data \(unverified\): subject "/CN=ssltestuser", serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL regression test client certs"'],
        # revoked certificates should not authenticate the user
        log_unlike=[r"connection authenticated:"])

    # Check that connecting with auth-option verify-full in pg_hba:
    # works, iff username matches Common Name
    # fails, iff username doesn't match Common Name.
    common_connstr = (
        f"{default_ssl_connstr} sslrootcert={_ssl('root+server_ca.crt')} "
        f"sslmode=require dbname=verifydb hostaddr={SERVERHOSTADDR} host=localhost")

    node.connect_ok(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client.crt')}"
        + sslkey("client.key"),
        "auth_option clientcert=verify-full succeeds with matching username and Common Name",
        log_like=[r'connection authenticated: user="ssltestuser" method=trust'])

    node.connect_fails(
        f"{common_connstr} user=anotheruser sslcert={_ssl('client.crt')}"
        + sslkey("client.key"),
        "auth_option clientcert=verify-full fails with mismatching username and Common Name",
        expected_stderr=r'FATAL: .* "trust" authentication failed for user "anotheruser"',
        # verify-full does not provide authentication
        log_unlike=[r"connection authenticated:"])

    # Check that connecting with auth-option verify-ca in pg_hba:
    # works, when username doesn't match Common Name
    node.connect_ok(
        f"{common_connstr} user=yetanotheruser sslcert={_ssl('client.crt')}"
        + sslkey("client.key"),
        "auth_option clientcert=verify-ca succeeds with mismatching username and Common Name",
        log_like=[r'connection authenticated: user="yetanotheruser" method=trust'])

    # intermediate client_ca.crt is provided by client, and isn't in server's
    # ssl_ca_file
    switch_server_cert(certfile="server-cn-only", cafile="root_ca")
    common_connstr = (
        f"{default_ssl_connstr} user=ssltestuser dbname=certdb"
        + sslkey("client.key")
        + f" sslrootcert={_ssl('root+server_ca.crt')} hostaddr={SERVERHOSTADDR} host=localhost")

    node.connect_ok(
        f"{common_connstr} sslmode=require sslcert={_ssl('client+client_ca.crt')}",
        "intermediate client certificate is provided by client")

    node.connect_fails(
        f"{common_connstr} sslmode=require sslcert={_ssl('client.crt')}",
        "intermediate client certificate is missing",
        expected_stderr=r"SSL error: tlsv1 alert unknown ca",
        log_like=[
            r"Client certificate verification failed at depth 0: unable to get local issuer certificate",
            r'Failed certificate data \(unverified\): subject "/CN=ssltestuser", serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL regression test client certs"'])

    node.connect_fails(
        f"{common_connstr} sslmode=require sslcert={_ssl('client-long.crt')}"
        + sslkey("client-long.key"),
        "logged client certificate Subjects are truncated if they're too long",
        expected_stderr=r"SSL error: tlsv1 alert unknown ca",
        log_like=[
            r"Client certificate verification failed at depth 0: unable to get local issuer certificate",
            r'Failed certificate data \(unverified\): subject "\.\.\./CN=ssl-123456789012345678901234567890123456789012345678901234567890", serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL regression test client certs"'])

    # Use an invalid cafile here so that the next test won't be able to verify
    # the client CA.
    switch_server_cert(certfile="server-cn-only", cafile="server-cn-only")

    # intermediate CA is provided but doesn't have a trusted root (checks error
    # logging for cert chain depths > 0)
    node.connect_fails(
        f"{common_connstr} sslmode=require sslcert={_ssl('client+client_ca.crt')}",
        "intermediate client certificate is untrusted",
        expected_stderr=r"SSL error: tlsv1 alert unknown ca",
        log_like=[
            r"Client certificate verification failed at depth 1: unable to get local issuer certificate",
            # As of 5/2025, LibreSSL reports a different cert as being at fault;
            # it's wrong, but seems to be their bug not ours
            (r'Failed certificate data \(unverified\): subject "/CN=Test CA for PostgreSQL SSL regression test client certs", serial number \d+, issuer "/CN=Test root CA for PostgreSQL SSL regression test suite"'
             if not libressl
             else r'Failed certificate data \(unverified\): subject "/CN=ssltestuser", serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL regression test client certs"')])

    # test server-side CRL directory
    switch_server_cert(certfile="server-cn-only", crldir="root+client-crldir")

    # revoked client cert
    node.connect_fails(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client-revoked.crt')}"
        + sslkey("client-revoked.key"),
        "certificate authorization fails with revoked client cert with server-side CRL directory",
        expected_stderr=r"SSL error: (ssl[a-z0-9/]*|tls) alert certificate revoked",
        log_like=[
            r"Client certificate verification failed at depth 0: certificate revoked",
            r'Failed certificate data \(unverified\): subject "/CN=ssltestuser", serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL regression test client certs"'])

    # revoked client cert, non-ASCII subject
    node.connect_fails(
        f"{common_connstr} user=ssltestuser sslcert={_ssl('client-revoked-utf8.crt')}"
        + sslkey("client-revoked-utf8.key"),
        "certificate authorization fails with revoked UTF-8 client cert with server-side CRL directory",
        expected_stderr=r"SSL error: (ssl[a-z0-9/]*|tls) alert certificate revoked",
        log_like=[
            r"Client certificate verification failed at depth 0: certificate revoked",
            r'Failed certificate data \(unverified\): subject "/CN=\\xce\\x9f\\xce\\xb4\\xcf\\x85\\xcf\\x83\\xcf\\x83\\xce\\xad\\xce\\xb1\\xcf\\x82", serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL regression test client certs"'])

    if supports_sslcertmode_require:
        # Test client CAs
        connstr = (
            f"user=ssltestuser dbname=certdb hostaddr={SERVERHOSTADDR} "
            "sslmode=require sslsni=1")

        switch_server_cert(certfile="server-cn-only", cafile="")
        # example.org is unconfigured and should fail.
        node.connect_fails(
            f"{connstr} host=example.org sslcertmode=require sslcert={_ssl('client.crt')}"
            + sslkey("client.key"),
            "host: 'example.org', ca: '': connect with sslcert, no client CA configured",
            expected_stderr=r"client certificates can only be checked if a root certificate store is available")

        # example.com uses the client CA.
        switch_server_cert(certfile="server-cn-only", cafile="root+client_ca")
        # example.com is configured and should require a valid client cert.
        node.connect_fails(
            f"{connstr} host=example.com sslcertmode=disable",
            "host: 'example.com', ca: 'root+client_ca.crt': connect fails if no client certificate sent",
            expected_stderr=r"connection requires a valid client certificate")
        node.connect_ok(
            f"{connstr} host=example.com sslcertmode=require sslcert={_ssl('client.crt')}"
            + sslkey("client.key"),
            "host: 'example.com', ca: 'root+client_ca.crt': connect with sslcert, client certificate sent")

        # example.net uses the server CA (which is wrong).
        switch_server_cert(certfile="server-cn-only", cafile="root+server_ca")
        # example.net is configured and should require a client cert, but will
        # always fail verification.
        node.connect_fails(
            f"{connstr} host=example.net sslcertmode=disable",
            "host: 'example.net', ca: 'root+server_ca.crt': connect fails if no client certificate sent",
            expected_stderr=r"connection requires a valid client certificate")

        node.connect_fails(
            f"{connstr} host=example.net sslcertmode=require sslcert={_ssl('client.crt')}"
            + sslkey("client.key"),
            "host: 'example.net', ca: 'root+server_ca.crt': connect with sslcert, client certificate sent",
            expected_stderr=r"unknown ca")
