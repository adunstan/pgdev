# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Tests scenarios related to the service name and the service file.

Covers the connection options and their environment variables (PGSERVICE /
PGSERVICEFILE / PGSYSCONFDIR, and the "service" / "servicefile" connection
keywords).

Connections are made with a psql subprocess so that the service environment
variables are inherited the way a real client sees them; a successful
connection runs the SELECT and checks its output, a failed connection exits
non-zero with an error message we match.  (An in-process libpq connection is
not used here: the in-process library does not portably read the environment,
and on Windows it cannot connect to the server's socket from this process.)

The service file points at the real, started ``pg`` server.  The framework's
environment setup clears the PG* connection variables, so the only connection
information in play comes from the service file / service keywords under test.
"""

import getpass
import os
import re
import shutil
from contextlib import contextmanager

import pytest

# The login role: the cluster uses trust auth, so any user connects.  We pin it
# explicitly in every connection string so that nothing has to inject a
# "user=" keyword (which would corrupt the URI-form connection strings).
USER = getpass.getuser()


@contextmanager
def _env(**overrides):
    """Temporarily set/unset environment variables, restoring on exit.

    A value of None unsets the variable for the duration of the block.
    """
    saved = {}
    try:
        for key, val in overrides.items():
            saved[key] = os.environ.get(key)
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        yield
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _kw_user(connstr):
    """Append a user= keyword to a (keyword-form) connection string."""
    return (connstr + f" user='{USER}'").strip()


def _uri_user(uri):
    """Append a user query parameter to a URI-form connection string."""
    sep = "&" if "?" in uri else "?"
    return f"{uri}{sep}user={USER}"


def _psql(node, connstr, sql):
    """Run psql with *connstr* verbatim and return ``(ok, stdout, stderr)``.

    The connection string is passed as-is (no host/port prepended), so the
    service / servicefile under test fully determines the connection target.
    psql is a subprocess, so it inherits PGSERVICE / PGSERVICEFILE /
    PGSYSCONFDIR from the environment.
    """
    res = node.pg_bin.result(
        ["psql", "-w", "-X", "-A", "-q", "-t", "-d", connstr, "-c", sql]
    )
    return res.returncode == 0, res.stdout, res.stderr


def _connect_ok(node, connstr, expected, **env):
    """Assert psql connects with *connstr* and its output matches *expected*."""
    with _env(**env):
        ok, out, err = _psql(node, connstr, f"SELECT '{expected}'")
    assert ok, f"connection should succeed for {connstr!r}: got {err!r}"
    assert re.search(expected, out), f"stdout matches for {connstr!r}: got {out!r}"


def _connect_fails(node, connstr, pattern, **env):
    """Assert psql fails to connect with *connstr* and *pattern* is in the error."""
    with _env(**env):
        ok, _out, err = _psql(node, connstr, "SELECT 1")
    assert not ok, f"connection should fail for {connstr!r}"
    assert re.search(pattern, err), (
        f"error matches /{pattern}/ for {connstr!r}: got {err!r}"
    )


def _connect_servicefile_is(node, connstr, expected_servicefile, **env):
    """Assert psql connects and the service file it resolved matches.

    psql exposes the service file libpq actually used as the :SERVICEFILE
    variable.
    """
    with _env(**env):
        ok, out, err = _psql(node, connstr, r"\echo :SERVICEFILE")
    assert ok, f"connection should succeed for {connstr!r}: got {err!r}"
    actual = out.strip()
    assert actual == expected_servicefile, (
        f"resolved servicefile for {connstr!r}: expected "
        f"{expected_servicefile!r}, got {actual!r}"
    )


@pytest.fixture
def service_setup(pg, tmp_path):
    """Build the set of service files used by the tests, pointing at ``pg``.

    Returns a dict of the paths plus the base environment (PGSYSCONFDIR and a
    default empty PGSERVICEFILE) that every test starts from.
    """
    td = tmp_path

    # File that includes a valid service name, using a decomposed connection
    # string for its contents (one parameter per line).  The connection
    # parameters are written unquoted, as a service file gives each value
    # verbatim to the right of the "=".
    srvfile_valid = td / "pg_service_valid.conf"
    lines = [
        "[my_srv]",
        f"host={pg.host}",
        f"port={pg.port}",
        "dbname=postgres",
    ]
    srvfile_valid.write_text("\n".join(lines) + "\n")

    # File defined with no contents, used as the default value for
    # PGSERVICEFILE so that no lookup is attempted in the user's home dir.
    srvfile_empty = td / "pg_service_empty.conf"
    srvfile_empty.write_text("")

    # Default service file in PGSYSCONFDIR.
    srvfile_default = td / "pg_service.conf"

    # Missing service file.
    srvfile_missing = td / "pg_service_missing.conf"

    # Service file with a nested "service" defined.
    srvfile_nested = td / "pg_service_nested.conf"
    shutil.copy(srvfile_valid, srvfile_nested)
    with open(srvfile_nested, "a") as fh:
        fh.write("service=invalid_srv\n")

    # Service file with a nested "servicefile" defined.
    srvfile_nested_2 = td / "pg_service_nested_2.conf"
    shutil.copy(srvfile_valid, srvfile_nested_2)
    with open(srvfile_nested_2, "a") as fh:
        fh.write(f"servicefile={srvfile_default}\n")

    # Use forward slashes for every path.  Several of these go into connection
    # strings as a servicefile= value, where libpq treats backslash as an
    # escape character and would mangle a Windows path; forward slashes are a
    # valid path separator on Windows for files, environment values and
    # libpq's own servicefile bookkeeping, so they work everywhere.
    def fwd(path):
        return str(path).replace("\\", "/")

    return {
        "td": fwd(td),
        "valid": fwd(srvfile_valid),
        "empty": fwd(srvfile_empty),
        "default": fwd(srvfile_default),
        "missing": fwd(srvfile_missing),
        "nested": fwd(srvfile_nested),
        "nested_2": fwd(srvfile_nested_2),
        # PGSYSCONFDIR is the fallback directory lookup of the service file.
        # PGSERVICEFILE is forced to a default (empty) location so the test
        # never looks at a home directory.
        "base_env": {"PGSYSCONFDIR": fwd(td), "PGSERVICEFILE": fwd(srvfile_empty)},
        "node": pg,
    }


def test_service_with_pgservicefile(service_setup):
    """Combinations of service name and a valid service file via PGSERVICEFILE."""
    s = service_setup
    node = s["node"]
    env = dict(s["base_env"], PGSERVICEFILE=s["valid"])

    _connect_ok(node, _kw_user("service=my_srv"), "connect1_1", **env)
    _connect_ok(node, _uri_user("postgres://?service=my_srv"), "connect1_2", **env)
    _connect_fails(
        node,
        _kw_user("service=undefined-service"),
        r'definition of service "undefined-service" not found',
        **env,
    )

    _connect_ok(
        node, _kw_user(""), "connect1_3", **dict(env, PGSERVICE="my_srv")
    )
    _connect_fails(
        node,
        _kw_user(""),
        r'definition of service "undefined-service" not found',
        **dict(env, PGSERVICE="undefined-service"),
    )


def test_service_with_incorrect_pgservicefile(service_setup):
    """Incorrect (missing) service file referenced by PGSERVICEFILE."""
    s = service_setup
    env = dict(s["base_env"], PGSERVICEFILE=s["missing"])
    _connect_fails(
        s["node"],
        _kw_user("service=my_srv"),
        r'service file ".*pg_service_missing\.conf" not found',
        **env,
    )


def test_service_with_default_pg_service_conf(service_setup):
    """Service file named "pg_service.conf" found in PGSYSCONFDIR."""
    s = service_setup
    node = s["node"]
    # Create copy of the valid file at the default PGSYSCONFDIR location.
    shutil.copy(s["valid"], s["default"])
    try:
        env = dict(s["base_env"])  # PGSERVICEFILE stays at the empty default
        _connect_ok(node, _kw_user("service=my_srv"), "connect2_1", **env)
        _connect_ok(
            node, _uri_user("postgres://?service=my_srv"), "connect2_2", **env
        )
        _connect_fails(
            node,
            _kw_user("service=undefined-service"),
            r'definition of service "undefined-service" not found',
            **env,
        )
        _connect_ok(
            node, _kw_user(""), "connect2_3", **dict(env, PGSERVICE="my_srv")
        )
        # The given servicefile (empty) does not define the service, so it is
        # found in the default pg_service.conf; libpq then reports the default
        # file as the resolved servicefile.
        _connect_servicefile_is(
            node,
            _kw_user(f"service=my_srv servicefile='{s['empty']}'"),
            s["default"],
            **env,
        )
        _connect_fails(
            node,
            _kw_user(""),
            r'definition of service "undefined-service" not found',
            **dict(env, PGSERVICE="undefined-service"),
        )
    finally:
        os.unlink(s["default"])


def test_service_nested(service_setup):
    """Nested "service" / "servicefile" specifications are rejected."""
    s = service_setup
    node = s["node"]

    _connect_fails(
        node,
        _kw_user("service=my_srv"),
        r'nested "service" specifications not supported in service file',
        **dict(s["base_env"], PGSERVICEFILE=s["nested"]),
    )
    _connect_fails(
        node,
        _kw_user("service=my_srv"),
        r'nested "servicefile" specifications not supported in service file',
        **dict(s["base_env"], PGSERVICEFILE=s["nested_2"]),
    )


def test_servicefile_option(service_setup):
    """The "servicefile" connection option works in keyword and URI forms."""
    s = service_setup
    node = s["node"]
    env = dict(s["base_env"])  # PGSERVICEFILE stays at the empty default

    # No backslash escaping needed on non-Windows (paths use forward slashes).
    valid = s["valid"]

    _connect_ok(
        node,
        _kw_user(f"service=my_srv servicefile='{valid}'"),
        "connect3_1",
        **env,
    )

    # Encode slashes (and backslash, and colon) for the URI form.
    encoded = valid.replace("\\", "%5C").replace("/", "%2F").replace(":", "%3A")

    _connect_ok(
        node,
        _uri_user(f"postgresql:///?service=my_srv&servicefile={encoded}"),
        "connect3_2",
        **env,
    )

    _connect_ok(
        node,
        _kw_user(f"servicefile='{valid}'"),
        "connect3_3",
        **dict(env, PGSERVICE="my_srv"),
    )
    _connect_ok(
        node,
        _uri_user(f"postgresql://?servicefile={encoded}"),
        "connect3_4",
        **dict(env, PGSERVICE="my_srv"),
    )


def test_servicefile_option_priority(service_setup):
    """The "servicefile" option takes priority over PGSERVICEFILE."""
    s = service_setup
    node = s["node"]
    valid = s["valid"]
    env = dict(s["base_env"], PGSERVICEFILE="non-existent-file.conf")

    _connect_fails(
        node,
        _kw_user("service=my_srv"),
        r'service file "non-existent-file\.conf" not found',
        **env,
    )
    _connect_ok(
        node,
        _kw_user(f"service=my_srv servicefile='{valid}'"),
        "connect4_1",
        **env,
    )
