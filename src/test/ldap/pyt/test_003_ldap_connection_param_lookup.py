# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Tests for libpq's client-side LDAP lookup of connection parameters.

This exercises libpq's *client*-side LDAP lookup of connection parameters
(an ldap:// URL in a service file that libpq resolves to obtain host/port),
together with the various service-name / service-file selection rules
(``service=``, ``postgres://?service=``, PGSERVICE, PGSERVICEFILE,
PGSYSCONFDIR, and the default ``pg_service.conf``).  This is unrelated to
server-side ldap *authentication* (see test_001_auth.py).
"""

import os
import shutil

import pytest

from libpq import Session
from libpq.errors import ConnectionError as PqConnectionError


def _attempt(libdir, connstr):
    """Make one raw libpq connection attempt and run a trivial query.

    Returns a (ok, output, error) tuple.  We deliberately build the Session
    from the bare *connstr* WITHOUT prepending the node's host/port (which is
    what node.connect_ok/connect_fails do via _full_connstr).  The whole point
    of this test is that libpq must resolve host/port itself from the LDAP
    lookup driven by the service file, so injecting an explicit host/port would
    defeat the lookup (an explicit host/port in the conninfo overrides the
    service-provided one).
    """
    sess = None
    try:
        # user="" suppresses Session's automatic ` user=<os-user>` append,
        # which would otherwise be glued onto a URI connstr and corrupt it.
        # The OS user is the initdb superuser, so no explicit user is needed.
        sess = Session(connstr=connstr, libdir=libdir, user="")
        res = sess.query("SELECT 1")
        if res.error_message is not None:
            return False, "", res.error_message
        return True, res.psqlout, ""
    except PqConnectionError as exc:
        return False, "", str(exc)
    finally:
        if sess is not None:
            sess.close()


def _connect_ok(libdir, connstr, test_name, *, sql=None, expected_stdout=None):
    """Assert that a bare-connstr connection succeeds."""
    sess = None
    try:
        # See _attempt(): user="" avoids corrupting URI connstrings.
        sess = Session(connstr=connstr, libdir=libdir, user="")
        res = sess.query(sql if sql is not None else "SELECT 1")
        err = res.error_message
        out = res.psqlout
    except PqConnectionError as exc:
        err = str(exc)
        out = ""
    finally:
        if sess is not None:
            sess.close()

    assert err is None or err == "", \
        f"{test_name}: connection should succeed\n{err}"
    if expected_stdout is not None:
        import re
        assert re.search(expected_stdout, out), (
            f"{test_name}: stdout matches {expected_stdout!r}, got {out!r}"
        )


def _connect_fails(libdir, connstr, test_name, *, expected_stderr=None):
    """Assert that a bare-connstr connection fails."""
    ok, _out, err = _attempt(libdir, connstr)
    assert not ok, f"{test_name}: connection should fail"
    if expected_stderr is not None:
        import re
        assert re.search(expected_stderr, err), (
            f"{test_name}: stderr matches {expected_stderr!r}, got {err!r}"
        )


def test_003_ldap_connection_param_lookup(create_pg, ldap_server, tmp_path):
    # Faithful skip ordering.
    if os.environ.get("with_ldap") != "yes":
        pytest.skip("LDAP not supported by this build")
    if "ldap" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip("Potentially unsafe test LDAP not enabled in PG_TEST_EXTRA")
    # The ldap_server fixture covers the slapd-availability skip.

    # This tests scenarios related to the service name and the service file,
    # for the connection options and their environment variables.
    #
    # dummy_node is created only to drive client connection attempts (it gives
    # us a libdir for libpq); the real server we connect to is "node".
    dummy_node = create_pg("dummy_node", start=False)
    libdir = dummy_node.libdir

    node = create_pg("node")

    # setting up LDAP server
    ldap_rootpw = "secret"
    ldap = ldap_server(ldap_rootpw, "anonymous")  # use anonymous auth
    authdata = os.path.join(os.path.dirname(__file__), "..", "authdata.ldif")
    ldap.ldapadd_file(authdata)
    ldap.ldapsetpw("uid=test1,dc=example,dc=net", "secret1")
    ldap.ldapsetpw("uid=test2,dc=example,dc=net", "secret2")

    td = str(tmp_path)

    # create ldap file based on postgres connection info
    ldif_valid = os.path.join(td, "connection_params.ldif")
    with open(ldif_valid, "w") as fh:
        fh.write(
            "\nversion:1\n"
            "dn:cn=mydatabase,dc=example,dc=net\n"
            "changetype:add\n"
            "objectclass:top\n"
            "objectclass:device\n"
            "cn:mydatabase\n"
            f"description:host={node.host}\n"
            f"description:port={node.port}\n"
        )

    ldap.ldapadd_file(ldif_valid)

    (ldap_server_name, ldap_port, ldaps_port, ldap_url,
     ldaps_url, ldap_basedn, ldap_rootdn) = ldap.prop(
        "server", "port", "s_port", "url", "s_url", "basedn", "rootdn")

    # don't bother to check the server's cert (though perhaps we should)
    os.environ["LDAPTLS_REQCERT"] = "never"

    # setting up PostgreSQL instance

    # Create the set of service files used in the tests.

    # File that includes a valid service name, that uses a decomposed
    # connection string for its contents, split on spaces.
    srvfile_valid = os.path.join(td, "pg_service_valid.conf")
    with open(srvfile_valid, "w") as fh:
        fh.write(
            "\n[my_srv]\n"
            f"ldap://localhost:{ldap_port}/dc=example,dc=net"
            "?description?one?(cn=mydatabase)\n"
        )

    # File defined with no contents, used as default value for
    # PGSERVICEFILE, so that no lookup is attempted in the user's home
    # directory.
    srvfile_empty = os.path.join(td, "pg_service_empty.conf")
    with open(srvfile_empty, "w") as fh:
        fh.write("")

    # Missing service file.
    srvfile_missing = os.path.join(td, "pg_service_missing.conf")

    # Snapshot the env vars we mutate so the test does not leak state into
    # other tests in the same process.
    saved_env = {k: os.environ.get(k)
                 for k in ("PGSYSCONFDIR", "PGSERVICEFILE", "PGSERVICE",
                           "PGDATABASE")}

    def _restore_env():
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    try:
        # Set the fallback directory lookup of the service file to the
        # temporary directory of this test.  PGSYSCONFDIR is used if the
        # service file defined in PGSERVICEFILE cannot be found, or when a
        # service file is found but not the service name.
        os.environ["PGSYSCONFDIR"] = td

        # The LDAP lookup supplies only host/port, not a database name; set
        # PGDATABASE=postgres so libpq does not fall back to a database named
        # after the OS user.
        os.environ["PGDATABASE"] = "postgres"

        # Force PGSERVICEFILE to a default location, so as this test never
        # tries to look at a home directory.  This value needs to remain at
        # the top before running any tests, and should never be changed.
        os.environ["PGSERVICEFILE"] = srvfile_empty

        # Checks combinations of service name and a valid service file.
        os.environ["PGSERVICEFILE"] = srvfile_valid
        os.environ.pop("PGSERVICE", None)

        _connect_ok(
            libdir, "service=my_srv",
            'connection with correct "service" string and PGSERVICEFILE',
            sql="SELECT 'connect1_1'",
            expected_stdout=r"connect1_1")

        _connect_ok(
            libdir, "postgres://?service=my_srv",
            'connection with correct "service" URI and PGSERVICEFILE',
            sql="SELECT 'connect1_2'",
            expected_stdout=r"connect1_2")

        _connect_fails(
            libdir, "service=undefined-service",
            'connection with incorrect "service" string and PGSERVICEFILE',
            expected_stderr=r'definition of service "undefined-service" not found')

        os.environ["PGSERVICE"] = "my_srv"

        _connect_ok(
            libdir, "",
            "connection with correct PGSERVICE and PGSERVICEFILE",
            sql="SELECT 'connect1_3'",
            expected_stdout=r"connect1_3")

        os.environ["PGSERVICE"] = "undefined-service"

        _connect_fails(
            libdir, "",
            "connection with incorrect PGSERVICE and PGSERVICEFILE",
            expected_stderr=r'definition of service "undefined-service" not found')

        # Restore the service-related env to the block's outer state.
        os.environ["PGSERVICEFILE"] = srvfile_empty
        os.environ.pop("PGSERVICE", None)

        # Checks case of incorrect service file.
        os.environ["PGSERVICEFILE"] = srvfile_missing

        _connect_fails(
            libdir, "service=my_srv",
            'connection with correct "service" string and incorrect PGSERVICEFILE',
            expected_stderr=r'service file ".*pg_service_missing.conf" not found')

        os.environ["PGSERVICEFILE"] = srvfile_empty

        # Checks case of service file named "pg_service.conf" in PGSYSCONFDIR.
        # Create copy of valid file.
        srvfile_default = os.path.join(td, "pg_service.conf")
        shutil.copy(srvfile_valid, srvfile_default)

        _connect_ok(
            libdir, "service=my_srv",
            'connection with correct "service" string and pg_service.conf',
            sql="SELECT 'connect2_1'",
            expected_stdout=r"connect2_1")

        _connect_ok(
            libdir, "postgres://?service=my_srv",
            'connection with correct "service" URI and default pg_service.conf',
            sql="SELECT 'connect2_2'",
            expected_stdout=r"connect2_2")

        _connect_fails(
            libdir, "service=undefined-service",
            'connection with incorrect "service" string and default pg_service.conf',
            expected_stderr=r'definition of service "undefined-service" not found')

        os.environ["PGSERVICE"] = "my_srv"

        _connect_ok(
            libdir, "",
            "connection with correct PGSERVICE and default pg_service.conf",
            sql="SELECT 'connect2_3'",
            expected_stdout=r"connect2_3")

        os.environ["PGSERVICE"] = "undefined-service"

        _connect_fails(
            libdir, "",
            "connection with incorrect PGSERVICE and default pg_service.conf",
            expected_stderr=r'definition of service "undefined-service" not found')

        os.environ.pop("PGSERVICE", None)

        # Remove default pg_service.conf.
        os.unlink(srvfile_default)
    finally:
        _restore_env()

    node.teardown()
