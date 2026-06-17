# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for GSSAPI/Kerberos authentication and encryption.

Sets up a KDC and then runs a variety of tests to make sure that the
GSSAPI/Kerberos authentication and encryption are working properly, that the
options in pg_hba.conf and pg_ident.conf are handled correctly, that the
server-side pg_stat_gssapi view reports what we expect to see for each test and
that SYSTEM_USER returns what we expect to see.

Also tests that GSSAPI delegation is working properly and that those
credentials can be used to make dblink / postgres_fdw connections.
"""

import os
import re

import pytest

from libpq.session import Session, PqConnectionError


def test_001_auth(create_pg, kerberos, tmp_path):
    # Faithful skip ordering.
    if os.environ.get("with_gssapi") != "yes":
        pytest.skip("GSSAPI/Kerberos not supported by this build")
    if "kerberos" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip(
            "Potentially unsafe test GSSAPI/Kerberos not enabled in PG_TEST_EXTRA"
        )
    # The kerberos fixture covers the krb5-binaries skip.

    pgpass = str(tmp_path / ".pgpass")

    dbname = "postgres"
    username = "test1"
    application = "001_auth"

    # Construct a pgpass file to make sure we don't use it
    with open(pgpass, "w", encoding="utf-8") as fh:
        fh.write("*:*:*:*:abc123")
    os.chmod(pgpass, 0o600)

    # setting up Kerberos
    host = "auth-test-localhost.postgresql.example.com"
    hostaddr = "127.0.0.1"
    realm = "EXAMPLE.COM"
    srvnam = os.environ.get("with_krb_srvnam", "postgres")

    krb = kerberos(host, hostaddr, realm, srvnam=srvnam)

    test1_password = "secret1"
    krb.create_principal("test1", test1_password)

    # setting up PostgreSQL instance.  Must listen on TCP for Kerberos, and the
    # KDC (kerberos fixture) was created first so the postmaster inherits the
    # KRB5_* environment.
    node = create_pg("node", start=False)
    node.append_conf(
        f"""
listen_addresses = '{hostaddr}'
krb_server_keyfile = '{krb.keytab}'
log_connections = all
log_min_messages = debug2
lc_messages = 'C'
"""
    )
    # The dblink / postgres_fdw cases below open server-side connections with a
    # bare "port=" (no host), which libpq resolves via the PGHOST environment
    # variable.  Export PGHOST to the node's socket dir so the backend's
    # outgoing connections reach this node's
    # unix socket (and so they are rejected for lack of delegated credentials,
    # not for a missing socket).
    os.environ["PGHOST"] = node.host
    node.start()

    port = node.port

    node.safe_sql("CREATE USER test1;")
    node.safe_sql("CREATE USER test2 WITH ENCRYPTED PASSWORD 'abc123';")
    node.safe_sql("CREATE EXTENSION postgres_fdw;")
    node.safe_sql("CREATE EXTENSION dblink;")
    node.safe_sql(
        f"CREATE SERVER s1 FOREIGN DATA WRAPPER postgres_fdw OPTIONS "
        f"(host '{host}', hostaddr '{hostaddr}', port '{port}', dbname 'postgres');"
    )
    node.safe_sql(
        f"CREATE SERVER s2 FOREIGN DATA WRAPPER postgres_fdw OPTIONS "
        f"(port '{port}', dbname 'postgres', passfile '{pgpass}');"
    )

    node.safe_sql("GRANT USAGE ON FOREIGN SERVER s1 TO test1;")

    node.safe_sql("CREATE USER MAPPING FOR test1 SERVER s1 OPTIONS (user 'test1');")
    node.safe_sql("CREATE USER MAPPING FOR test1 SERVER s2 OPTIONS (user 'test2');")

    node.safe_sql("CREATE TABLE t1 (c1 int);")
    node.safe_sql("INSERT INTO t1 VALUES (1);")
    node.safe_sql(
        "CREATE FOREIGN TABLE tf1 (c1 int) SERVER s1 OPTIONS "
        "(schema_name 'public', table_name 't1');"
    )
    node.safe_sql("GRANT SELECT ON t1 TO test1;")
    node.safe_sql("GRANT SELECT ON tf1 TO test1;")

    node.safe_sql(
        "CREATE FOREIGN TABLE tf2 (c1 int) SERVER s2 OPTIONS "
        "(schema_name 'public', table_name 't1');"
    )
    node.safe_sql("GRANT SELECT ON tf2 TO test1;")

    # Set up a table for SYSTEM_USER parallel worker testing.
    node.safe_sql(
        f"CREATE TABLE ids (id) AS SELECT 'gss:test1@{realm}' "
        f"FROM generate_series(1, 10);"
    )
    node.safe_sql("GRANT SELECT ON ids TO public;")

    # running tests

    def _connstr(role, gssencmode):
        # need to connect over TCP/IP for Kerberos; host/hostaddr override the
        # unix-socket host that connect_ok/connect_fails prepend.  The expected
        # "connection authorized" log line carries the application name, so set
        # it explicitly here.
        cs = (
            f"user={role} host={host} hostaddr={hostaddr} "
            f"application_name={application}"
        )
        if gssencmode:
            cs += f" {gssencmode}"
        return cs

    def test_access(role, query, expected_res, gssencmode, test_name, *expect_log_msgs):
        """Test connection success or failure, and if success, that query
        returns true.
        """
        connstr = _connstr(role, gssencmode)

        # Match every expected message literally.
        log_like = [re.escape(m) for m in expect_log_msgs] or None

        if expected_res == 0:
            # The result is assumed to match "true", or "t", here.
            node.connect_ok(
                connstr, test_name, sql=query, expected_stdout=r"^t$", log_like=log_like
            )
        else:
            # connect_fails does not run a query; the connection itself fails
            # (or, with an authenticated-but-unmapped user, the auth log lines
            # are still emitted and checked via log_like).
            node.connect_fails(connstr, test_name, log_like=log_like)

    def test_query(role, query, expected, gssencmode, test_name):
        """As above, but test for an arbitrary query result."""
        connstr = _connstr(role, gssencmode)
        node.connect_ok(connstr, test_name, sql=query, expected_stdout=expected)

    def gss_connstr(gssencmode):
        """Full conninfo (incl. node host/port/dbname) for a GSS connection.

        Used for the dblink / postgres_fdw cases.
        """
        return (
            f"{node.connstr('postgres')} user=test1 "
            f"host={host} hostaddr={hostaddr} {gssencmode}"
        )

    def gss_psql(query, gssencmode):
        """Run *query* over a fresh GSS connection; return (rc, out, stderr).

        Emulates ``$node->psql``: rc 3 means an error was raised running the
        query (psql's ON_ERROR_STOP exit code), rc 0 means success.
        """
        sess = None
        try:
            sess = Session(connstr=gss_connstr(gssencmode), libdir=node.libdir)
            res = sess.query(query)
            stderr = sess.get_notices_str() + (res.error_message or "")
            if res.error_message is not None:
                return 3, "", stderr
            return 0, res.psqlout, stderr
        except PqConnectionError as exc:
            return 2, "", str(exc)
        finally:
            if sess is not None:
                sess.close()

    def set_hba(*lines):
        """Replace pg_hba.conf with the given line(s)."""
        os.unlink(os.path.join(node.data_dir, "pg_hba.conf"))
        node.append_conf("\n".join(lines) + "\n", filename="pg_hba.conf")

    set_hba(
        "local all test2 scram-sha-256",
        f"host all all {hostaddr}/32 gss map=mymap",
    )
    node.restart()

    test_access("test1", "SELECT true", 2, "", "fails without ticket")

    krb.create_ticket("test1", test1_password)

    test_access(
        "test1",
        "SELECT true",
        2,
        "",
        "fails without mapping",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        'no match in usermap "mymap" for user "test1"',
    )

    node.append_conf(f"mymap  /^(.*)@{realm}$  \\1", filename="pg_ident.conf")
    node.restart()

    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "",
        "succeeds with mapping with default gssencmode and host hba, "
        "ticket not forwardable",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )

    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssencmode=prefer",
        "succeeds with GSS-encrypted access preferred with host hba, "
        "ticket not forwardable",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )

    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssencmode=require",
        "succeeds with GSS-encrypted access required with host hba, "
        "ticket not forwardable",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )

    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssencmode=prefer gssdelegation=1",
        "succeeds with GSS-encrypted access preferred with host hba and "
        "credentials not delegated even though asked for (ticket not "
        "forwardable)",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )
    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssencmode=require gssdelegation=1",
        "succeeds with GSS-encrypted access required with host hba and "
        "credentials not delegated even though asked for (ticket not "
        "forwardable)",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )

    # Test that we can transport a reasonable amount of data.
    test_query(
        "test1",
        "SELECT * FROM generate_series(1, 100000);",
        r"(?s)^1\n.*\n1024\n.*\n9999\n.*\n100000$",
        "gssencmode=require",
        "receiving 100K lines works",
    )

    # Sending 100K lines: the in-process libpq layer has no COPY-in support
    # and does not interpret psql backslash meta-commands, so send the same
    # number of rows in a single large query string over the encrypted channel
    # (exercising the client->server send path) and count them.
    big_send = (
        "CREATE TEMP TABLE mytab (f1 int primary key); "
        "INSERT INTO mytab SELECT generate_series(1, 100000); "
        "SELECT COUNT(*) FROM mytab;"
    )
    test_query(
        "test1",
        big_send,
        r"(?s)^100000$",
        "gssencmode=require",
        "sending 100K lines works",
    )

    # require_auth=gss succeeds if required.
    node.connect_ok(
        f"user=test1 host={host} hostaddr={hostaddr} "
        f"gssencmode=disable require_auth=gss",
        "GSS authentication requested, works with non-encrypted GSS",
    )
    node.connect_ok(
        f"user=test1 host={host} hostaddr={hostaddr} "
        f"gssencmode=require require_auth=gss",
        "GSS authentication requested, works with encrypted GSS auth",
    )

    # require_auth=sspi fails if required.
    node.connect_fails(
        f"user=test1 host={host} hostaddr={hostaddr} "
        f"gssencmode=disable require_auth=sspi",
        "SSPI authentication requested, fails with non-encrypted GSS",
        expected_stderr=r'authentication method requirement "sspi" failed: '
        r"server requested GSSAPI authentication",
    )
    node.connect_fails(
        f"user=test1 host={host} hostaddr={hostaddr} "
        f"gssencmode=require require_auth=sspi",
        "SSPI authentication requested, fails with encrypted GSS",
        expected_stderr=r'authentication method requirement "sspi" failed: '
        r"server did not complete authentication",
    )

    # Test that SYSTEM_USER works.
    test_query(
        "test1",
        "SELECT SYSTEM_USER;",
        rf"(?s)^gss:test1@{realm}$",
        "gssencmode=require",
        "testing system_user",
    )

    # Test that SYSTEM_USER works with parallel workers.
    test_query(
        "test1",
        """
        SET min_parallel_table_scan_size TO 0;
        SET parallel_setup_cost TO 0;
        SET parallel_tuple_cost TO 0;
        SET max_parallel_workers_per_gather TO 2;
        SELECT bool_and(SYSTEM_USER = id) FROM ids;""",
        r"(?s)^t$",
        "gssencmode=require",
        "testing system_user with parallel workers",
    )

    set_hba(
        "    local all test2 scram-sha-256",
        f"\thostgssenc all all {hostaddr}/32 gss map=mymap",
    )

    # Re-create the ticket, with the forwardable flag set
    krb.create_ticket("test1", test1_password, forwardable=True)

    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "from pg_stat_gssapi where pid = pg_backend_pid();",
        0,
        "gssencmode=prefer gssdelegation=1",
        "succeeds with GSS-encrypted access preferred and hostgssenc hba and "
        "credentials not forwarded (server does not accept them, default)",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )
    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "from pg_stat_gssapi where pid = pg_backend_pid();",
        0,
        "gssencmode=require gssdelegation=1",
        "succeeds with GSS-encrypted access required and hostgssenc hba and "
        "credentials not forwarded (server does not accept them, default)",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )

    node.append_conf("gss_accept_delegation=off")
    node.restart()

    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "from pg_stat_gssapi where pid = pg_backend_pid();",
        0,
        "gssencmode=prefer gssdelegation=1",
        "succeeds with GSS-encrypted access preferred and hostgssenc hba and "
        "credentials not forwarded (server does not accept them, explicitly "
        "disabled)",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )
    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "from pg_stat_gssapi where pid = pg_backend_pid();",
        0,
        "gssencmode=require gssdelegation=1",
        "succeeds with GSS-encrypted access required and hostgssenc hba and "
        "credentials not forwarded (server does not accept them, explicitly "
        "disabled)",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )

    node.append_conf("gss_accept_delegation=on")
    node.restart()

    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND credentials_delegated "
        "from pg_stat_gssapi where pid = pg_backend_pid();",
        0,
        "gssencmode=prefer gssdelegation=1",
        "succeeds with GSS-encrypted access preferred and hostgssenc hba and "
        "credentials forwarded",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=yes, principal=test1@{realm})",
    )
    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND credentials_delegated "
        "from pg_stat_gssapi where pid = pg_backend_pid();",
        0,
        "gssencmode=require gssdelegation=1",
        "succeeds with GSS-encrypted access required and hostgssenc hba and "
        "credentials forwarded",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=yes, principal=test1@{realm})",
    )
    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssencmode=prefer",
        "succeeds with GSS-encrypted access preferred and hostgssenc hba and "
        "credentials not forwarded",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )
    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND NOT credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssencmode=require gssdelegation=0",
        "succeeds with GSS-encrypted access required and hostgssenc hba and "
        "credentials explicitly not forwarded",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=no, principal=test1@{realm})",
    )

    no_deleg_re = r"password or GSSAPI delegated credentials required"

    rc, out, stderr = gss_psql(
        f"SELECT * FROM dblink('user=test1 dbname={dbname} host={host} "
        f"hostaddr={hostaddr} port={port}','select 1') as t1(c1 int);",
        "gssencmode=require gssdelegation=0",
    )
    assert rc == 3, "dblink attempt fails without delegated credentials"
    assert re.search(
        no_deleg_re, stderr
    ), "dblink does not work without delegated credentials"
    assert re.search(r"^$", out), "dblink does not work without delegated credentials"

    rc, out, stderr = gss_psql(
        f"SELECT * FROM dblink('user=test2 dbname={dbname} port={port} "
        f"passfile={pgpass}','select 1') as t1(c1 int);",
        "gssencmode=require gssdelegation=0",
    )
    assert (
        rc == 3
    ), "dblink does not work without delegated credentials and with passfile"
    assert re.search(
        no_deleg_re, stderr
    ), "dblink does not work without delegated credentials and with passfile"
    assert re.search(
        r"^$", out
    ), "dblink does not work without delegated credentials and with passfile"

    rc, out, stderr = gss_psql("TABLE tf1;", "gssencmode=require gssdelegation=0")
    assert rc == 3, "postgres_fdw does not work without delegated credentials"
    assert re.search(
        no_deleg_re, stderr
    ), "postgres_fdw does not work without delegated credentials"
    assert re.search(
        r"^$", out
    ), "postgres_fdw does not work without delegated credentials"

    rc, out, stderr = gss_psql("TABLE tf2;", "gssencmode=require gssdelegation=0")
    assert (
        rc == 3
    ), "postgres_fdw does not work without delegated credentials and with passfile"
    assert re.search(
        no_deleg_re, stderr
    ), "postgres_fdw does not work without delegated credentials and with passfile"
    assert re.search(
        r"^$", out
    ), "postgres_fdw does not work without delegated credentials and with passfile"

    test_access(
        "test1",
        "SELECT true",
        2,
        "gssencmode=disable",
        "fails with GSS encryption disabled and hostgssenc hba",
    )

    # require_auth=gss succeeds if required.
    node.connect_ok(
        f"user=test1 host={host} hostaddr={hostaddr} "
        f"gssencmode=require require_auth=gss",
        "GSS authentication requested, works with GSS encryption",
    )
    node.connect_ok(
        f"user=test1 host={host} hostaddr={hostaddr} "
        f"gssencmode=require require_auth=gss,scram-sha-256",
        "multiple authentication types requested, works with GSS encryption",
    )

    set_hba(
        "    local all test2 scram-sha-256",
        f"\thostnogssenc all all {hostaddr}/32 gss map=mymap",
    )
    node.restart()

    test_access(
        "test1",
        "SELECT gss_authenticated AND NOT encrypted AND credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssencmode=prefer gssdelegation=1",
        "succeeds with GSS-encrypted access preferred and hostnogssenc hba, "
        "but no encryption",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=no, "
        f"delegated_credentials=yes, principal=test1@{realm})",
    )
    test_access(
        "test1",
        "SELECT true",
        2,
        "gssencmode=require",
        "fails with GSS-encrypted access required and hostnogssenc hba",
    )
    test_access(
        "test1",
        "SELECT gss_authenticated AND NOT encrypted AND credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssencmode=disable gssdelegation=1",
        "succeeds with GSS encryption disabled and hostnogssenc hba",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=no, "
        f"delegated_credentials=yes, principal=test1@{realm})",
    )

    test_query(
        "test1",
        f"SELECT * FROM dblink('user=test1 dbname={dbname} host={host} "
        f"hostaddr={hostaddr} port={port}','select 1') as t1(c1 int);",
        r"(?s)^1$",
        "gssencmode=prefer gssdelegation=1",
        "dblink works not-encrypted (server not configured to accept "
        "encrypted GSSAPI connections)",
    )

    test_query(
        "test1",
        "TABLE tf1;",
        r"(?s)^1$",
        "gssencmode=prefer gssdelegation=1",
        "postgres_fdw works not-encrypted (server not configured to accept "
        "encrypted GSSAPI connections)",
    )

    rc, out, stderr = gss_psql(
        f"SELECT * FROM dblink('user=test2 dbname={dbname} port={port} "
        f"passfile={pgpass}','select 1') as t1(c1 int);",
        "gssencmode=prefer gssdelegation=1",
    )
    assert rc == 3, "dblink does not work with delegated credentials and with passfile"
    assert re.search(
        no_deleg_re, stderr
    ), "dblink does not work with delegated credentials and with passfile"
    assert re.search(
        r"^$", out
    ), "dblink does not work with delegated credentials and with passfile"

    rc, out, stderr = gss_psql("TABLE tf2;", "gssencmode=prefer gssdelegation=1")
    assert (
        rc == 3
    ), "postgres_fdw does not work with delegated credentials and with passfile"
    assert re.search(
        no_deleg_re, stderr
    ), "postgres_fdw does not work with delegated credentials and with passfile"
    assert re.search(
        r"^$", out
    ), "postgres_fdw does not work with delegated credentials and with passfile"

    # Truncate pg_ident.conf and reset pg_hba.conf for include_realm=0.
    with open(os.path.join(node.data_dir, "pg_ident.conf"), "w", encoding="utf-8"):
        pass
    set_hba(
        "    local all test2 scram-sha-256",
        f"\thost all all {hostaddr}/32 gss include_realm=0",
    )
    node.restart()

    test_access(
        "test1",
        "SELECT gss_authenticated AND encrypted AND credentials_delegated "
        "FROM pg_stat_gssapi WHERE pid = pg_backend_pid();",
        0,
        "gssdelegation=1",
        "succeeds with include_realm=0 and defaults",
        f'connection authenticated: identity="test1@{realm}" method=gss',
        f"connection authorized: user={username} database={dbname} "
        f"application_name={application} GSS (authenticated=yes, encrypted=yes, "
        f"delegated_credentials=yes, principal=test1@{realm})",
    )

    test_query(
        "test1",
        f"SELECT * FROM dblink('user=test1 dbname={dbname} host={host} "
        f"hostaddr={hostaddr} port={port} password=1234','select 1') "
        f"as t1(c1 int);",
        r"(?s)^1$",
        "gssencmode=require gssdelegation=1",
        "dblink works encrypted",
    )

    test_query(
        "test1",
        "TABLE tf1;",
        r"(?s)^1$",
        "gssencmode=require gssdelegation=1",
        "postgres_fdw works encrypted",
    )

    # Reset pg_hba.conf, and cause a usermap failure with an authentication
    # that has passed.
    set_hba(
        "    local all test2 scram-sha-256",
        f"\thost all all {hostaddr}/32 gss include_realm=0 krb_realm=EXAMPLE.ORG",
    )
    node.restart()

    test_access(
        "test1",
        "SELECT true",
        2,
        "",
        "fails with wrong krb_realm, but still authenticates",
        f'connection authenticated: identity="test1@{realm}" method=gss',
    )
