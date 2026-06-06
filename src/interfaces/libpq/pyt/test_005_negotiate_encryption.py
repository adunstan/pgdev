# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test negotiation of SSL and GSSAPI encryption.

OVERVIEW
--------

Test negotiation of SSL and GSSAPI encryption.

We test all combinations of:

- all the libpq client options that affect the protocol negotiations
  (gssencmode, sslmode, sslnegotiation)
- server accepting or rejecting the authentication due to pg_hba.conf entries
- SSL and GSS enabled/disabled in the server

That's a lot of combinations, so we use a table-driven approach.  Each
combination is represented by a line in a table.  The line lists the options
specifying the test case, and an expected outcome.  The expected outcome
includes whether the connection succeeds or fails, and whether it uses SSL,
GSS or no encryption.  It also includes a condensed trace of what steps were
taken during the negotiation.

See the docstring of :func:`_parse_log_events` for the EVENTS / OUTCOME table
format.

NOTES
-----

The whole combination table is checked as follows:

* The connection is attempted in-process via libpq (a fresh ``Session``), not
  by forking psql.  The full conninfo string carries
  host=/hostaddr=/user=/gssencmode=/sslmode=/sslnegotiation=, and the outcome
  is the value returned by ``SELECT current_enc()`` on success, or ``fail`` if
  the connection (or that query) fails.

* The EVENTS trace is scraped from the *server* log, relying on the server's
  ``trace_connection_negotiation``, ``log_connections`` and
  ``log_disconnections`` output.  The in-process Session drives the same wire
  protocol negotiation, and the same server log lines are produced and parsed.

Framework specifics:

* TCP listening is configured with ``listen_addresses = '127.0.0.1'`` (the
  PostgresServer fixture is unix-socket-only by default).

* injection_points availability is probed via ``pg_available_extensions``.

* SSL on/off is toggled by appending ``ssl = on`` / ``ssl = off`` to
  postgresql.conf and reloading (a later setting wins).

The kerberos and ssl_server fixtures are obtained lazily (via
``request.getfixturevalue``) only when the corresponding support is enabled,
so the GSS/SSL sub-blocks skip independently (and the whole module skips
cleanly when libpq_encryption is not enabled).
"""

import os
import re

import pytest

from libpq import Session
from libpq.errors import ConnectionError as PqConnectionError

# -- module-level gating -----------------------------------------------------

if not re.search(r"\blibpq_encryption\b", os.environ.get("PG_TEST_EXTRA", "")):
    pytest.skip(
        "Potentially unsafe test libpq_encryption not enabled in PG_TEST_EXTRA",
        allow_module_level=True,
    )

# Only run the GSSAPI tests when compiled with GSSAPI support and PG_TEST_EXTRA
# includes 'kerberos'.
GSS_SUPPORTED = os.environ.get("with_gssapi") == "yes"
KERBEROS_ENABLED = bool(
    re.search(r"\bkerberos\b", os.environ.get("PG_TEST_EXTRA", ""))
)
SSL_SUPPORTED = os.environ.get("with_ssl") == "openssl"

HOST = "enc-test-localhost.postgresql.example.com"
HOSTADDR = "127.0.0.1"
SERVERCIDR = "127.0.0.1/32"

DBNAME = "postgres"
GSSUSER_PASSWORD = "secret1"

ALL_TEST_USERS = ["testuser", "ssluser", "nossluser", "gssuser", "nogssuser"]
ALL_GSSENCMODES = ["disable", "prefer", "require"]
ALL_SSLMODES = ["disable", "allow", "prefer", "require"]
ALL_SSLNEGOTIATIONS = ["postgres", "direct"]


# -- table parsing helpers (NOT named test_*) --------------------------------

def _expand_expected_line(user, gssencmode, sslmode, sslnegotiation, expected):
    """Expand '*' wildcards on a test table line into concrete keys."""
    result = {}
    if user == "*":
        for x in ALL_TEST_USERS:
            result.update(
                _expand_expected_line(x, gssencmode, sslmode, sslnegotiation,
                                      expected))
    elif gssencmode == "*":
        for x in ALL_GSSENCMODES:
            result.update(
                _expand_expected_line(user, x, sslmode, sslnegotiation,
                                      expected))
    elif sslmode == "*":
        for x in ALL_SSLMODES:
            result.update(
                _expand_expected_line(user, gssencmode, x, sslnegotiation,
                                      expected))
    elif sslnegotiation == "*":
        for x in ALL_SSLNEGOTIATIONS:
            result.update(
                _expand_expected_line(user, gssencmode, sslmode, x,
                                      expected))
    else:
        result[f"{user} {gssencmode} {sslmode} {sslnegotiation}"] = expected
    return result


_LINE_RE = re.compile(
    r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S.*)\s*->\s*(\S+)\s*$"
)


def _parse_table(table):
    """Parse a test table.  See the comment at the top of the file for format."""
    expected = {}
    user = gssencmode = sslmode = sslnegotiation = None

    for raw_line in table.split("\n"):
        # Trim comments and surrounding whitespace.
        line = re.sub(r"#.*$", "", raw_line).strip()
        if line == "":
            continue

        m = _LINE_RE.match(line)
        if not m:
            raise AssertionError(f'could not parse line "{line}"')

        if m.group(1) != ".":
            user = m.group(1)
        if m.group(2) != ".":
            gssencmode = m.group(2)
        if m.group(3) != ".":
            sslmode = m.group(3)
        if m.group(4) != ".":
            sslnegotiation = m.group(4)

        # Normalize the whitespace in the "EVENTS -> OUTCOME" part.
        events = re.split(r",\s*", m.group(5))
        outcome = m.group(6)
        events_str = ", ".join(events).rstrip()
        events_and_outcome = f"{events_str} -> {outcome}"

        expected.update(
            _expand_expected_line(user, gssencmode, sslmode, sslnegotiation,
                                  events_and_outcome))
    return expected


def _parse_log_events(log_contents):
    """Scrape the server log for the negotiation events.

    Each recognised log line emits one condensed event token; no events at all
    is represented by ``-``.
    """
    events = []
    for line in log_contents.split("\n"):
        if "connection received" in line:
            events.append("reconnect" if events else "connect")
        if "SSLRequest accepted" in line:
            events.append("sslaccept")
        if "SSLRequest rejected" in line:
            events.append("sslreject")
        if "direct SSL connection accepted" in line:
            events.append("directsslaccept")
        if "direct SSL connection rejected" in line:
            events.append("directsslreject")
        if "GSSENCRequest accepted" in line:
            events.append("gssaccept")
        if "GSSENCRequest rejected" in line:
            events.append("gssreject")
        if "no pg_hba.conf entry" in line:
            events.append("authfail")
        if "connection authenticated" in line:
            events.append("authok")
        if "error triggered for injection point backend-" in line:
            events.append("backenderror")
        if "protocol version 2 error triggered" in line:
            events.append("v2error")

    if not events:
        events.append("-")
    return events


# -- connection test driver (NOT named test_*) -------------------------------

class _Harness:
    """Holds the node and accumulates pass/fail results for the run."""

    def __init__(self, node):
        self.node = node
        self.failures = []
        self.count = 0

    def connect_test(self, connstr, expected_events_and_outcome):
        """Attempt a connection and verify the events and outcome.

        The outcome is the value of ``SELECT current_enc()`` on success or
        ``fail`` on any failure, and the EVENTS are scraped from the server log
        lines produced since the attempt began.
        """
        self.count += 1
        test_name = f" '{connstr}' -> {expected_events_and_outcome}"

        connstr_full = ""
        if "dbname=" not in connstr:
            connstr_full += "dbname=postgres "
        if "host=" not in connstr:
            connstr_full += f"host={HOST} hostaddr={HOSTADDR} "
        # The framework gives each node its own port, so add it here (later
        # keywords win in libpq, so an explicit port= in connstr -- there is
        # none -- would still take precedence).
        if "port=" not in connstr:
            connstr_full += f"port={self.node.port} "
        connstr_full += connstr

        # Record the current log size; afterwards we look only at new lines.
        log_location = self.node.log_position()

        outcome = "fail"
        stderr = ""
        sess = None
        try:
            sess = Session(connstr=connstr_full, libdir=self.node.libdir)
            res = sess.query("SELECT current_enc()")
            if res.error_message is None:
                outcome = res.psqlout.strip()
            else:
                stderr = res.error_message
        except PqConnectionError as exc:
            stderr = str(exc)
        finally:
            if sess is not None:
                sess.close()

        # Parse the EVENTS from the new portion of the log file.  Wait briefly
        # for the disconnection record so the trailing events are present.
        log_contents = self._slurp_log(log_location)
        events = _parse_log_events(log_contents)

        events_and_outcome = ", ".join(events) + f" -> {outcome}"
        if events_and_outcome != expected_events_and_outcome:
            self.failures.append(
                f"FAIL:{test_name}\n"
                f"     got:      {events_and_outcome}\n"
                f"     expected: {expected_events_and_outcome}\n"
                f"     stderr:   {stderr.strip()}"
            )

    def _slurp_log(self, offset):
        """Return log text written since *offset*, allowing it to settle.

        The server writes its log records slightly asynchronously from the
        client's point of view, so poll until two consecutive reads agree
        (content stopped growing).  An empty result is legitimate -- a purely
        client-side failure (e.g. gssencmode=require with no ccache, or a
        direct-SSL request rejected before connecting) never reaches the server
        -- so empty-and-stable returns promptly rather than waiting out a long
        timeout.
        """
        import time

        deadline = time.monotonic() + 5.0
        prev = self.node.log_content()[offset:]
        while True:
            time.sleep(0.05)
            content = self.node.log_content()[offset:]
            if content == prev:
                return content
            prev = content
            if time.monotonic() > deadline:
                return content


def _test_matrix(harness, test_users, gssencmodes, sslmodes, sslnegotiations,
                 expected):
    """Test the cube of parameters: user, gssencmode, sslmode, sslnegotiation."""
    for test_user in test_users:
        for gssencmode in gssencmodes:
            for client_mode in sslmodes:
                for negotiation in sslnegotiations:
                    key = f"{test_user} {gssencmode} {client_mode} {negotiation}"
                    expected_events = expected.get(
                        key, "<line missing from expected output table>")
                    harness.connect_test(
                        f"user={test_user} gssencmode={gssencmode} "
                        f"sslmode={client_mode} sslnegotiation={negotiation}",
                        expected_events)


# -- the test ----------------------------------------------------------------

def test_005_negotiate_encryption(create_pg, request, tmp_path):
    ###
    ### Prepare test server for GSSAPI and SSL authentication, with a few
    ### different test users and helper functions.  We don't actually enable
    ### SSL and kerberos in the server yet, we will do that later.
    ###
    node = create_pg("node", start=False)
    node.append_conf(f"""
listen_addresses = '{HOSTADDR}'

# Capturing the EVENTS that occur during tests requires these settings
log_connections = 'receipt,authentication,authorization'
log_disconnections = on
trace_connection_negotiation = on
lc_messages = 'C'
""")
    pgdata = node.data_dir

    krb = None
    if GSS_SUPPORTED and KERBEROS_ENABLED:
        # note: setting up Kerberos
        realm = "EXAMPLE.COM"
        kerberos = request.getfixturevalue("kerberos")
        krb = kerberos(HOST, HOSTADDR, realm)
        node.append_conf(f"krb_server_keyfile = '{krb.keytab}'\n")

    ssl_server = None
    if SSL_SUPPORTED:
        ssl_server = request.getfixturevalue("ssl_server")
        certdir = ssl_server.ssl_dir
        import shutil
        shutil.copy(os.path.join(certdir, "server-cn-only.crt"),
                    os.path.join(pgdata, "server.crt"))
        shutil.copy(os.path.join(certdir, "server-cn-only.key"),
                    os.path.join(pgdata, "server.key"))
        os.chmod(os.path.join(pgdata, "server.key"), 0o600)

        # Start with SSL disabled.
        node.append_conf("ssl = off\n")

    node.start()

    # Check if the extension injection_points is available, as it may be
    # possible that this script is run with installcheck, where the module
    # would not be installed by default.
    injection_points_supported = node.safe_sql(
        "SELECT count(*) > 0 FROM pg_available_extensions "
        "WHERE name = 'injection_points'").strip() == "t"

    node.safe_sql("CREATE USER localuser;")
    node.safe_sql("CREATE USER testuser;")
    node.safe_sql("CREATE USER ssluser;")
    node.safe_sql("CREATE USER nossluser;")
    node.safe_sql("CREATE USER gssuser;")
    node.safe_sql("CREATE USER nogssuser;")
    if injection_points_supported:
        node.safe_sql("CREATE EXTENSION injection_points;")

    unixdir = node.safe_sql("SHOW unix_socket_directories;").strip()

    # Helper function that returns the encryption method in use in the
    # connection.
    node.safe_sql(r"""
CREATE FUNCTION current_enc() RETURNS text LANGUAGE plpgsql AS $$
DECLARE
  ssl_in_use bool;
  gss_in_use bool;
BEGIN
  ssl_in_use = (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid());
  gss_in_use = (SELECT encrypted FROM pg_stat_gssapi WHERE pid = pg_backend_pid());

  raise log 'ssl %  gss %', ssl_in_use, gss_in_use;

  IF ssl_in_use AND gss_in_use THEN
    RETURN 'ssl+gss';   -- shouldn't happen
  ELSIF ssl_in_use THEN
    RETURN 'ssl';
  ELSIF gss_in_use THEN
    RETURN 'gss';
  ELSE
    RETURN 'plain';
  END IF;
END;
$$;
""")

    # Only accept SSL connections from $servercidr.  Our tests don't depend on
    # this but seems best to keep it as narrow as possible for security reasons.
    hba = (
        "# TYPE        DATABASE        USER            ADDRESS                 METHOD             OPTIONS\n"
        "local         postgres        localuser                               trust\n"
        f"host          postgres        testuser        {SERVERCIDR}             trust\n"
        f"hostnossl     postgres        nossluser       {SERVERCIDR}             trust\n"
        f"hostnogssenc  postgres        nogssuser       {SERVERCIDR}             trust\n"
    )
    if SSL_SUPPORTED:
        hba += f"hostssl       postgres        ssluser         {SERVERCIDR}             trust\n"
    if GSS_SUPPORTED and KERBEROS_ENABLED:
        hba += f"hostgssenc    postgres        gssuser         {SERVERCIDR}             trust\n"
    with open(os.path.join(pgdata, "pg_hba.conf"), "w") as fh:
        fh.write(hba)
    node.reload()

    # After the pg_hba.conf rewrite above, the only local-socket entry is for
    # 'localuser', so the framework's own safe_sql (which connects as the OS
    # user over the unix socket) no longer works.  Run the later administrative
    # statements via a dedicated localuser session.  This is also what
    # backend-side injection_points_attach needs.
    def admin_psql(sql):
        with Session(
            connstr=f"host={unixdir} port={node.port} dbname=postgres "
                    f"user=localuser",
            libdir=node.libdir,
        ) as sess:
            sess.query_safe(sql)

    # Ok, all prepared.  Run the tests.
    harness = _Harness(node)

    ###
    ### Run tests with GSS and SSL disabled in the server
    ###
    if SSL_SUPPORTED:
        test_table = r"""
# USER      GSSENCMODE   SSLMODE      SSLNEGOTIATION EVENTS                      -> OUTCOME
testuser    disable      disable      postgres       connect, authok             -> plain
.           .            allow        postgres       connect, authok             -> plain
.           .            prefer       postgres       connect, sslreject, authok  -> plain
.           .            require      postgres       connect, sslreject          -> fail
.           .            .            direct         connect, directsslreject    -> fail
.           prefer       disable      postgres       connect, authok             -> plain
.           .            allow        postgres       connect, authok             -> plain
.           .            prefer       postgres       connect, sslreject, authok  -> plain
.           .            require      postgres       connect, sslreject          -> fail
.           .            .            direct         connect, directsslreject    -> fail

# sslnegotiation=direct is not accepted unless sslmode=require or stronger
*           *            disable      direct         -     -> fail
*           *            allow        direct         -     -> fail
*           *            prefer       direct         -     -> fail
"""
    else:
        # Compiled without SSL support
        test_table = r"""
# USER      GSSENCMODE   SSLMODE      SSLNEGOTIATION EVENTS                      -> OUTCOME
testuser    disable      disable      postgres       connect, authok             -> plain
.           .            allow        postgres       connect, authok             -> plain
.           .            prefer       postgres       connect, authok             -> plain
.           prefer       disable      postgres       connect, authok             -> plain
.           .            allow        postgres       connect, authok             -> plain
.           .            prefer       postgres       connect, authok             -> plain

# Without SSL support, sslmode=require and sslnegotiation=direct are
# not accepted at all
*           *            require      *              -     -> fail
*           *            *            direct         -     -> fail
"""

    # All attempts with gssencmode=require fail without connecting because no
    # credential cache has been configured in the client.  (Or if GSS support
    # is not compiled in, they will fail because of that.)
    test_table += r"""
testuser    require      *            *              - -> fail
"""

    # note: Running tests with SSL and GSS disabled in the server
    _test_matrix(harness, ["testuser"], ALL_GSSENCMODES, ALL_SSLMODES,
                 ALL_SSLNEGOTIATIONS, _parse_table(test_table))

    ###
    ### Run tests with GSS disabled and SSL enabled in the server
    ###
    if SSL_SUPPORTED:
        test_table = r"""
# USER      GSSENCMODE   SSLMODE      SSLNEGOTIATION EVENTS                                          -> OUTCOME
testuser    disable      disable      postgres       connect, authok                                 -> plain
.           .            allow        postgres       connect, authok                                 -> plain
.           .            prefer       postgres       connect, sslaccept, authok                      -> ssl
.           .            require      postgres       connect, sslaccept, authok                      -> ssl
.           .            .            direct         connect, directsslaccept, authok                -> ssl
ssluser     .            disable      postgres       connect, authfail                               -> fail
.           .            allow        postgres       connect, authfail, reconnect, sslaccept, authok -> ssl
.           .            prefer       postgres       connect, sslaccept, authok                      -> ssl
.           .            require      postgres       connect, sslaccept, authok                      -> ssl
.           .            .            direct         connect, directsslaccept, authok                -> ssl
nossluser   .            disable      postgres       connect, authok                                 -> plain
.           .            allow        postgres       connect, authok                                 -> plain
.           .            prefer       postgres       connect, sslaccept, authfail, reconnect, authok -> plain
.           .            require      postgres       connect, sslaccept, authfail                    -> fail
.           .            require      direct         connect, directsslaccept, authfail              -> fail

# sslnegotiation=direct is not accepted unless sslmode=require or stronger
*           *            disable      direct         -     -> fail
*           *            allow        direct         -     -> fail
*           *            prefer       direct         -     -> fail
"""

        # Enable SSL in the server
        node.append_conf("ssl = on\n")
        node.reload()

        # note: Running tests with SSL enabled in server
        _test_matrix(harness, ["testuser", "ssluser", "nossluser"],
                     ["disable"], ALL_SSLMODES, ALL_SSLNEGOTIATIONS,
                     _parse_table(test_table))

        if injection_points_supported:
            admin_psql(
                "SELECT injection_points_attach('backend-initialize', 'error');")
            harness.connect_test(
                "user=testuser sslmode=prefer",
                "connect, backenderror -> fail")
            node.restart()

            admin_psql(
                "SELECT injection_points_attach('backend-initialize-v2-error', 'error');")
            harness.connect_test(
                "user=testuser sslmode=prefer",
                "connect, v2error -> fail")
            node.restart()

            admin_psql(
                "SELECT injection_points_attach('backend-ssl-startup', 'error');")
            harness.connect_test(
                "user=testuser sslmode=prefer",
                "connect, sslaccept, backenderror, reconnect, authok -> plain")
            node.restart()

        # Disable SSL again
        node.append_conf("ssl = off\n")
        node.reload()

    ###
    ### Run tests with GSS enabled, SSL disabled in the server
    ###
    if GSS_SUPPORTED and KERBEROS_ENABLED:
        krb.create_principal("gssuser", GSSUSER_PASSWORD)
        krb.create_ticket("gssuser", GSSUSER_PASSWORD)

        test_table = r"""
# USER      GSSENCMODE   SSLMODE      SSLNEGOTIATION EVENTS                       -> OUTCOME
testuser    disable      disable      postgres       connect, authok              -> plain
.           .            allow        postgres       connect, authok              -> plain
.           .            prefer       postgres       connect, sslreject, authok   -> plain
.           .            require      postgres       connect, sslreject                -> fail
.           .            .            direct         connect, directsslreject          -> fail
.           prefer       *            postgres       connect, gssaccept, authok        -> gss
.           prefer       require      direct         connect, gssaccept, authok        -> gss
.           require      *            postgres       connect, gssaccept, authok        -> gss
.           .            require      direct         connect, gssaccept, authok        -> gss

gssuser     disable      disable      postgres       connect, authfail                  -> fail
.           .            allow        postgres       connect, authfail, reconnect, sslreject -> fail
.           .            prefer       postgres       connect, sslreject, authfail       -> fail
.           .            require      postgres       connect, sslreject                 -> fail
.           .            .            direct         connect, directsslreject           -> fail
.           prefer       *            postgres       connect, gssaccept, authok   -> gss
.           prefer       require      direct         connect, gssaccept, authok   -> gss
.           require      *            postgres       connect, gssaccept, authok   -> gss
.           .            require      direct         connect, gssaccept, authok   -> gss

nogssuser   disable      disable      postgres       connect, authok              -> plain
.           .            allow        postgres       connect, authok              -> plain
.           .            prefer       postgres       connect, sslreject, authok   -> plain
.           .            require      postgres       connect, sslreject                 -> fail
.           .            .            direct         connect, directsslreject           -> fail
.           prefer       disable      postgres       connect, gssaccept, authfail, reconnect, authok             -> plain
.           .            allow        postgres       connect, gssaccept, authfail, reconnect, authok             -> plain
.           .            prefer       postgres       connect, gssaccept, authfail, reconnect, sslreject, authok  -> plain
.           .            require      postgres       connect, gssaccept, authfail, reconnect, sslreject          -> fail
.           .            .            direct         connect, gssaccept, authfail, reconnect, directsslreject          -> fail
.           require      disable      postgres       connect, gssaccept, authfail -> fail
.           .            allow        postgres       connect, gssaccept, authfail -> fail
.           .            prefer       postgres       connect, gssaccept, authfail -> fail
.           .            require      postgres       connect, gssaccept, authfail -> fail   # If both GSSAPI and sslmode are required, and GSS is not available -> fail
.           .            .            direct         connect, gssaccept, authfail -> fail   # If both GSSAPI and sslmode are required, and GSS is not available -> fail

# sslnegotiation=direct is not accepted unless sslmode=require or stronger
*           *            disable      direct         -     -> fail
*           *            allow        direct         -     -> fail
*           *            prefer       direct         -     -> fail
"""

        # The expected events and outcomes above assume that SSL support is
        # enabled.  When libpq is compiled without SSL support, all attempts to
        # connect with sslmode=require or sslnegotiation=direct would fail
        # immediately without even connecting to the server.  Skip those,
        # because we tested them earlier already.
        if SSL_SUPPORTED:
            sslmodes, sslnegotiations = ALL_SSLMODES, ALL_SSLNEGOTIATIONS
        else:
            sslmodes, sslnegotiations = ["disable"], ["postgres"]

        # note: Running tests with GSS enabled in server
        _test_matrix(harness, ["testuser", "gssuser", "nogssuser"],
                     ALL_GSSENCMODES, sslmodes, sslnegotiations,
                     _parse_table(test_table))

        if injection_points_supported:
            admin_psql(
                "SELECT injection_points_attach('backend-initialize', 'error');")
            harness.connect_test(
                "user=testuser gssencmode=prefer sslmode=disable",
                "connect, backenderror, reconnect, backenderror -> fail")
            node.restart()

            admin_psql(
                "SELECT injection_points_attach('backend-initialize-v2-error', 'error');")
            harness.connect_test(
                "user=testuser gssencmode=prefer sslmode=disable",
                "connect, v2error, reconnect, v2error -> fail")
            node.restart()

            admin_psql(
                "SELECT injection_points_attach('backend-gssapi-startup', 'error');")
            harness.connect_test(
                "user=testuser gssencmode=prefer sslmode=disable",
                "connect, gssaccept, backenderror, reconnect, authok -> plain")
            node.restart()

    ###
    ### Tests with both GSS and SSL enabled in the server
    ###
    if SSL_SUPPORTED and GSS_SUPPORTED and KERBEROS_ENABLED:
        # Sanity check that GSSAPI is still enabled from previous test.
        harness.connect_test(
            "user=testuser gssencmode=prefer sslmode=prefer",
            "connect, gssaccept, authok -> gss")

        # Enable SSL
        node.append_conf("ssl = on\n")
        node.reload()

        test_table = r"""
# USER      GSSENCMODE   SSLMODE      SSLNEGOTIATION EVENTS                       -> OUTCOME
testuser    disable      disable      postgres       connect, authok              -> plain
.           .            allow        postgres       connect, authok              -> plain
.           .            prefer       postgres       connect, sslaccept, authok   -> ssl
.           .            require      postgres       connect, sslaccept, authok   -> ssl
.           .            .            direct         connect, directsslaccept, authok   -> ssl
.           prefer       disable      postgres       connect, gssaccept, authok   -> gss
.           .            allow        postgres       connect, gssaccept, authok   -> gss
.           .            prefer       postgres       connect, gssaccept, authok   -> gss
.           .            require      postgres       connect, gssaccept, authok   -> gss     # If both GSS and SSL is possible, GSS is chosen over SSL, even if sslmode=require
.           .            .            direct         connect, gssaccept, authok   -> gss
.           require      disable      postgres       connect, gssaccept, authok   -> gss
.           .            allow        postgres       connect, gssaccept, authok   -> gss
.           .            prefer       postgres       connect, gssaccept, authok   -> gss
.           .            require      postgres       connect, gssaccept, authok   -> gss     # If both GSS and SSL is possible, GSS is chosen over SSL, even if sslmode=require
.           .            .            direct         connect, gssaccept, authok   -> gss

gssuser     disable      disable      postgres       connect, authfail            -> fail
.           .            allow        postgres       connect, authfail, reconnect, sslaccept, authfail -> fail
.           .            prefer       postgres       connect, sslaccept, authfail, reconnect, authfail -> fail
.           .            require      postgres       connect, sslaccept, authfail       -> fail
.           .            .            direct         connect, directsslaccept, authfail -> fail
.           prefer       disable      postgres       connect, gssaccept, authok   -> gss
.           .            allow        postgres       connect, gssaccept, authok   -> gss
.           .            prefer       postgres       connect, gssaccept, authok   -> gss
.           .            require      postgres       connect, gssaccept, authok   -> gss   # GSS is chosen over SSL, even though sslmode=require
.           .            .            direct         connect, gssaccept, authok   -> gss
.           require      disable      postgres       connect, gssaccept, authok   -> gss
.           .            allow        postgres       connect, gssaccept, authok   -> gss
.           .            prefer       postgres       connect, gssaccept, authok   -> gss
.           .            require      postgres       connect, gssaccept, authok   -> gss     # If both GSS and SSL is possible, GSS is chosen over SSL, even if sslmode=require
.           .            .            direct         connect, gssaccept, authok   -> gss

ssluser     disable      disable      postgres       connect, authfail            -> fail
.           .            allow        postgres       connect, authfail, reconnect, sslaccept, authok       -> ssl
.           .            prefer       postgres       connect, sslaccept, authok         -> ssl
.           .            require      postgres       connect, sslaccept, authok         -> ssl
.           .            .            direct         connect, directsslaccept, authok   -> ssl
.           prefer       disable      postgres       connect, gssaccept, authfail, reconnect, authfail -> fail
.           .            allow        postgres       connect, gssaccept, authfail, reconnect, authfail, reconnect, sslaccept, authok       -> ssl
.           .            prefer       postgres       connect, gssaccept, authfail, reconnect, sslaccept, authok       -> ssl
.           .            require      postgres       connect, gssaccept, authfail, reconnect, sslaccept, authok       -> ssl
.           .            .            direct         connect, gssaccept, authfail, reconnect, directsslaccept, authok -> ssl
.           require      disable      postgres       connect, gssaccept, authfail -> fail
.           .            allow        postgres       connect, gssaccept, authfail -> fail
.           .            prefer       postgres       connect, gssaccept, authfail -> fail
.           .            require      postgres       connect, gssaccept, authfail -> fail         # If both GSS and SSL are required, the sslmode=require is effectively ignored and GSS is required
.           .            .            direct         connect, gssaccept, authfail -> fail

nogssuser   disable      disable      postgres       connect, authok              -> plain
.           .            allow        postgres       connect, authok              -> plain
.           .            prefer       postgres       connect, sslaccept, authok   -> ssl
.           .            require      postgres       connect, sslaccept, authok   -> ssl
.           .            .            direct         connect, directsslaccept, authok   -> ssl
.           prefer       disable      postgres       connect, gssaccept, authfail, reconnect, authok              -> plain
.           .            allow        postgres       connect, gssaccept, authfail, reconnect, authok              -> plain
.           .            prefer       postgres       connect, gssaccept, authfail, reconnect, sslaccept, authok         -> ssl
.           .            require      postgres       connect, gssaccept, authfail, reconnect, sslaccept, authok         -> ssl
.           .            .            direct         connect, gssaccept, authfail, reconnect, directsslaccept, authok   -> ssl
.           require      disable      postgres       connect, gssaccept, authfail -> fail
.           .            allow        postgres       connect, gssaccept, authfail -> fail
.           .            prefer       postgres       connect, gssaccept, authfail -> fail
.           .            require      postgres       connect, gssaccept, authfail -> fail   # If both GSS and SSL are required, the sslmode=require is effectively ignored and GSS is required
.           .            .            direct         connect, gssaccept, authfail -> fail

nossluser   disable      disable      postgres       connect, authok              -> plain
.           .            allow        postgres       connect, authok              -> plain
.           .            prefer       postgres       connect, sslaccept, authfail, reconnect, authok       -> plain
.           .            require      postgres       connect, sslaccept, authfail       -> fail
.           .            .            direct         connect, directsslaccept, authfail -> fail
.           prefer       *            postgres       connect, gssaccept, authok   -> gss
.           .            require      direct         connect, gssaccept, authok   -> gss
.           require      *            postgres       connect, gssaccept, authok   -> gss
.           .            require      direct         connect, gssaccept, authok   -> gss

# sslnegotiation=direct is not accepted unless sslmode=require or stronger
*           *            disable      direct         -     -> fail
*           *            allow        direct         -     -> fail
*           *            prefer       direct         -     -> fail
"""

        # note: Running tests with both GSS and SSL enabled in server
        _test_matrix(
            harness,
            ["testuser", "gssuser", "ssluser", "nogssuser", "nossluser"],
            ALL_GSSENCMODES, ALL_SSLMODES, ALL_SSLNEGOTIATIONS,
            _parse_table(test_table))

    ###
    ### Test negotiation over unix domain sockets.
    ###
    if unixdir != "":
        # libpq doesn't attempt SSL or GSSAPI over Unix domain sockets.  The
        # server would reject them too.
        harness.connect_test(
            f"user=localuser gssencmode=prefer sslmode=prefer host={unixdir}",
            "connect, authok -> plain")
        harness.connect_test(
            f"user=localuser gssencmode=require sslmode=prefer host={unixdir}",
            "- -> fail")

    # Report all accumulated failures at once.
    assert not harness.failures, (
        f"{len(harness.failures)} of {harness.count} negotiation cases failed:\n"
        + "\n".join(harness.failures)
    )
