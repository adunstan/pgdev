# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Pytest fixtures for PostgreSQL tests.

Loaded as a plugin (``-p pypg.fixtures``).  Provides the building blocks tests
use: ``pg_bin`` to run client programs, ``create_pg`` to spin up servers, and
``pg``/``conn`` for the common single-server case.  Servers are cleaned up
automatically at the end of the test.
"""

import os
import shutil
import socket
import subprocess
import tempfile

import pytest

from . import _env
from .command import PgBin
from .server import PostgresServer


@pytest.fixture(scope="session", autouse=True)
def _prepare_env():
    """Force C locale and clear PG* variables before anything starts."""
    _env.prepare_environment()


def _pg_config_value(pg_config, option):
    return subprocess.run(
        [pg_config, option], stdout=subprocess.PIPE, text=True, check=True
    ).stdout.strip()


@pytest.fixture(scope="session")
def pg_config():
    path = shutil.which("pg_config")
    if not path:
        pytest.skip("pg_config not found on PATH")
    return path


@pytest.fixture(scope="session")
def bindir(pg_config):
    return _pg_config_value(pg_config, "--bindir")


@pytest.fixture(scope="session")
def libdir(pg_config):
    return _pg_config_value(pg_config, "--libdir")


@pytest.fixture(scope="session")
def pg_bin(bindir):
    """A PgBin for running client programs that do not need a server."""
    return PgBin(bindir)


def _free_port():
    """Return an unused TCP port number (used to name the unix socket)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def create_pg(bindir, libdir, tmp_path):
    """Factory creating PostgresServer instances, torn down after the test.

    ``create_pg(name="main", start=True, initdb_extra=None,
    allows_streaming=False, has_archiving=False, has_restoring=False)`` returns
    an initialized (and, by default, started) server; ``initdb_extra`` is a
    list of extra arguments passed to initdb (e.g. ``["--no-data-checksums"]``).
    The streaming/archiving/restoring flags are forwarded to
    :meth:`PostgresServer.init`.  Data dirs live under the test's tmp_path; the
    unix socket lives in a short /tmp directory to stay within the socket path
    length limit.
    """
    servers = []
    sockdirs = []

    def _create(
        name="main",
        *,
        start=True,
        initdb_extra=None,
        allows_streaming=False,
        has_archiving=False,
        has_restoring=False,
    ):
        sockdir = tempfile.mkdtemp(prefix="pgt")
        sockdirs.append(sockdir)
        server = PostgresServer(
            name,
            bindir,
            libdir,
            str(tmp_path / name),
            _free_port(),
            sockdir,
        )
        server.init(
            extra=initdb_extra,
            allows_streaming=allows_streaming,
            has_archiving=has_archiving,
            has_restoring=has_restoring,
        )
        if start:
            server.start()
        servers.append(server)
        return server

    yield _create

    for server in servers:
        server.teardown()
    for sockdir in sockdirs:
        shutil.rmtree(sockdir, ignore_errors=True)


@pytest.fixture
def pg(create_pg):
    """A single started PostgresServer for the test."""
    return create_pg("main")


@pytest.fixture
def conn(pg):
    """A libpq Session connected to the ``pg`` server's postgres database."""
    return pg.session()


@pytest.fixture
def ldap_server(tmp_path):
    """Factory creating LdapServer (slapd) instances, stopped after the test.

    ``ldap_server(rootpw, authtype)`` returns a running server (authtype is
    'users' or 'anonymous').  Skips the test if no usable slapd is found.  The
    SSL certs come from src/test/ssl/ssl.
    """
    from . import ldapserver

    if not ldapserver.AVAILABLE:
        pytest.skip(ldapserver.SETUP_ERROR)

    # repo root is four levels up from this file (src/test/pytest/pypg).
    repo = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    certdir = os.path.join(repo, "src", "test", "ssl", "ssl")

    servers = []
    counter = [0]

    def _create(rootpw, authtype):
        counter[0] += 1
        basedir = tmp_path / f"ldap{counter[0]}"
        basedir.mkdir()
        server = ldapserver.LdapServer(basedir, rootpw, authtype, certdir)
        servers.append(server)
        return server

    yield _create

    for server in servers:
        server.stop()


@pytest.fixture
def kerberos(tmp_path):
    """Factory creating a Kerberos KDC, stopped after the test.

    ``kerberos(host, hostaddr, realm, srvnam="postgres")`` sets up a realm +
    service principal and starts krb5kdc, setting KRB5_CONFIG/KRB5_KDC_PROFILE/
    KRB5CCNAME in the environment (so a postmaster started afterward inherits
    them).  Those env vars are restored at teardown.  Skips if MIT krb5 is not
    installed.
    """
    from . import kerberos as krb

    if not krb.AVAILABLE:
        pytest.skip(krb.SETUP_ERROR)

    saved_env = {k: os.environ.get(k)
                 for k in ("KRB5_CONFIG", "KRB5_KDC_PROFILE", "KRB5CCNAME")}
    kdcs = []
    counter = [0]

    def _create(host, hostaddr, realm, srvnam="postgres"):
        counter[0] += 1
        basedir = tmp_path / f"krb{counter[0]}"
        basedir.mkdir()
        kdc = krb.Kerberos(basedir, host, hostaddr, realm, srvnam)
        kdcs.append(kdc)
        return kdc

    yield _create

    for kdc in kdcs:
        kdc.stop()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def oauth_server():
    """Factory starting the mock OAuth provider (oauth_server.py).

    ``oauth_server(script_path)`` returns a running OAuthServer with a ``.port``
    attribute; it is stopped at teardown.
    """
    from .oauthserver import OAuthServer

    servers = []

    def _create(script):
        srv = OAuthServer(script)
        servers.append(srv)
        return srv

    yield _create

    for srv in servers:
        srv.stop()


@pytest.fixture
def ssl_server(bindir, tmp_path):
    """An SSLServer (OpenSSL backend) for configuring a cluster for SSL.

    Skips the test unless this build uses OpenSSL (with_ssl=openssl).  Client
    keys are copied, with private permissions, under tmp_path.
    """
    from .ssl_server import SSLServer

    if os.environ.get("with_ssl") != "openssl":
        pytest.skip("SSL not supported by this build")

    repo = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    ssl_dir = os.path.join(repo, "src", "test", "ssl", "ssl")
    keydir = tmp_path / "ssl-keys"
    keydir.mkdir()
    return SSLServer(ssl_dir, keydir, bindir)
