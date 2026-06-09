# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Pytest fixtures for PostgreSQL tests.

Loaded as a plugin (``-p pypg.fixtures``).  Provides the building blocks tests
use: ``pg_bin`` to run client programs, ``create_pg`` to spin up servers, and
``pg``/``conn`` for the common single-server case.  Servers are cleaned up
automatically at the end of the test.
"""

import os
import pathlib
import re
import shutil
import socket
import subprocess

import pytest

from . import _env
from .command import PgBin
from .server import PostgresServer
from .util import short_tempdir


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


@pytest.fixture(scope="session", autouse=True)
def _check_libpq_abi(libdir):
    """Skip the suite when this Python cannot load the build's libpq.

    The in-process libpq layer is loaded via ctypes, so the interpreter must
    match libpq's ABI.  A 64-bit Python cannot dlopen the 32-bit libpq from a
    ``-m32`` build, which would otherwise fail every test with an OSError; skip
    with a clear reason instead.  See findlib.libpq_abi_skip_reason.
    """
    from libpq.findlib import libpq_abi_skip_reason

    reason = libpq_abi_skip_reason(libdir)
    if reason:
        pytest.skip(reason)


@pytest.fixture(scope="session")
def pg_bin(bindir):
    """A PgBin for running client programs that do not need a server."""
    return PgBin(bindir)


def _safe_node_name(request):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)


@pytest.fixture
def test_datadir(request, tmp_path_factory):
    """The per-test directory for servers and other test data.

    Under the meson/testwrap harness this is rooted at the per-file TESTDATADIR
    (as PostgreSQL::Test::Utils uses for tmp_check); for a standalone pytest run
    it falls back to a directory minted from pytest's tmp_path_factory.  Using
    TESTDATADIR rather than pytest's shared pytest-of-<user> base matters
    because that base is created with mode 0o700: on Windows that is an
    owner-only ACL the postmaster's restricted access token cannot use, so
    server-created paths under it (data dirs, tablespaces) fail with
    "Permission denied".

    TESTDATADIR is shared by every test function in a file, so append a
    per-function subdirectory to keep functions from colliding.
    """
    root = os.environ.get("TESTDATADIR")
    safe = _safe_node_name(request)
    if not root:
        return tmp_path_factory.mktemp(safe, numbered=True)
    path = pathlib.Path(root) / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def tmp_path(test_datadir):
    """Override pytest's built-in tmp_path so per-test scratch space lives
    under the harness directory (TESTDATADIR) rather than pytest's own base.

    pytest creates its base (pytest-of-<user>) with mode 0o700, which on Windows
    is an owner-only ACL the postmaster's restricted access token cannot use --
    initdb, tablespaces and other server-created paths under it then fail with
    "Permission denied".  Rooting at TESTDATADIR (created by the harness with an
    inheritable ACL) avoids that and is where servers already live.
    """
    return test_datadir


def _free_port():
    """Return an unused TCP port number (used to name the unix socket)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def create_pg(bindir, libdir, test_datadir):
    """Factory creating PostgresServer instances, torn down after the test.

    ``create_pg(name="main", start=True, initdb_extra=None,
    allows_streaming=False, has_archiving=False, has_restoring=False,
    port=None, own_host=False)`` returns an initialized (and, by default,
    started) server; ``initdb_extra`` is a list of extra arguments passed to
    initdb (e.g. ``["--no-data-checksums"]``).  The streaming/archiving/
    restoring flags are forwarded to :meth:`PostgresServer.init`.

    ``port`` pins an explicit port (otherwise a free one is chosen); ``own_host``
    binds the node to its own loopback address (127.0.0.1, .2, .3, ...) over
    TCP.  Together they let several nodes share one port, distinguished by IP
    -- needed for DNS-based load-balancing tests.

    Data dirs live under the test's own data directory; the unix socket lives
    in a short /tmp directory to stay within the socket path length limit.
    """
    servers = []
    sockdirs = []
    # Per-test loopback-IP counter for own_host nodes (127.0.0.1, .2, .3, ...).
    own_host_counter = [0]

    def _create(
        name="main",
        *,
        start=True,
        initdb_extra=None,
        allows_streaming=False,
        has_archiving=False,
        has_restoring=False,
        port=None,
        own_host=False,
    ):
        sockdir = short_tempdir()
        sockdirs.append(sockdir)
        listen_host = None
        if own_host:
            own_host_counter[0] += 1
            if own_host_counter[0] > 254:
                raise RuntimeError("too many own_host nodes")
            listen_host = f"127.0.0.{own_host_counter[0]}"
        server = PostgresServer(
            name,
            bindir,
            libdir,
            str(test_datadir / name),
            _free_port() if port is None else port,
            sockdir,
            listen_host=listen_host,
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
def tempdir_short():
    """A temporary directory with a short pathname, removed after the test.

    Some uses need a path short enough to fit tar's ~100-byte symlink-target
    limit -- notably tablespace locations, whose symlinks are written into a
    base backup's tar stream.  The per-test data directory can exceed that (its
    deep layout is especially long on macOS), so use a directory directly under
    the system temp area instead.
    """
    d = short_tempdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def pg(create_pg):
    """A single started PostgresServer for the test."""
    return create_pg("main")


@pytest.fixture
def conn(pg):
    """A persistent libpq Session connected to the ``pg`` server's postgres
    database, closed at the end of the test."""
    sess = pg.connect()
    yield sess
    sess.close()


@pytest.fixture
def ldap_server(test_datadir):
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
        basedir = test_datadir / f"ldap{counter[0]}"
        basedir.mkdir()
        server = ldapserver.LdapServer(basedir, rootpw, authtype, certdir)
        servers.append(server)
        return server

    yield _create

    for server in servers:
        server.stop()


@pytest.fixture
def kerberos(test_datadir):
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
        basedir = test_datadir / f"krb{counter[0]}"
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
def ssl_server(bindir, test_datadir):
    """An SSLServer (OpenSSL backend) for configuring a cluster for SSL.

    Skips the test unless this build uses OpenSSL (with_ssl=openssl).  Client
    keys are copied, with private permissions, under the test data directory.
    """
    from .ssl_server import SSLServer

    if os.environ.get("with_ssl") != "openssl":
        pytest.skip("SSL not supported by this build")

    repo = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    ssl_dir = os.path.join(repo, "src", "test", "ssl", "ssl")
    keydir = test_datadir / "ssl-keys"
    keydir.mkdir()
    return SSLServer(ssl_dir, keydir, bindir)
