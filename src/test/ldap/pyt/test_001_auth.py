# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for LDAP authentication."""

import os

import pytest


def _access(node, role, expected_res, test_name, **params):
    """Attempt a connection and check the expected outcome.

    *expected_res* of 0 means the connection should succeed; anything else
    means it should fail (only the status code is checked on failure).
    """
    connstr = f"user={role}"
    if expected_res == 0:
        node.connect_ok(connstr, test_name, **params)
    else:
        # No checks of the error message, only the status code.
        node.connect_fails(connstr, test_name, **params)


def test_001_auth(create_pg, ldap_server):
    # Faithful skip ordering.
    if os.environ.get("with_ldap") != "yes":
        pytest.skip("LDAP not supported by this build")
    if "ldap" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip("Potentially unsafe test LDAP not enabled in PG_TEST_EXTRA")
    # The ldap_server fixture covers the slapd-availability skip.

    # setting up LDAP server
    ldap_rootpw = "secret"
    ldap = ldap_server(ldap_rootpw, "anonymous")  # use anonymous auth
    authdata = os.path.join(os.path.dirname(__file__), "..", "authdata.ldif")
    ldap.ldapadd_file(authdata)
    ldap.ldapsetpw("uid=test1,dc=example,dc=net", "secret1")
    ldap.ldapsetpw("uid=test2,dc=example,dc=net", "secret2")

    (ldap_server_name, ldap_port, ldaps_port, ldap_url,
     ldaps_url, ldap_basedn, ldap_rootdn) = ldap.prop(
        "server", "port", "s_port", "url", "s_url", "basedn", "rootdn")

    # don't bother to check the server's cert (though perhaps we should)
    os.environ["LDAPTLS_REQCERT"] = "never"

    # setting up PostgreSQL instance
    node = create_pg("node", start=False)
    node.append_conf("log_connections = all\n")
    # Needed to allow connect_fails to inspect postmaster log:
    node.append_conf("log_min_messages = debug2")
    node.start()

    node.safe_sql("CREATE USER test0;")
    node.safe_sql("CREATE USER test1;")
    node.safe_sql('CREATE USER "test2@example.net";')

    def set_hba(line):
        """Replace pg_hba.conf with a single ldap line and restart."""
        hba = os.path.join(node.data_dir, "pg_hba.conf")
        os.unlink(hba)
        node.append_conf(line, filename="pg_hba.conf")
        node.restart()

    # running tests

    # simple bind
    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapprefix="uid=" '
        f'ldapsuffix=",dc=example,dc=net"'
    )

    os.environ["PGPASSWORD"] = "wrong"
    _access(
        node, "test0", 2,
        "simple bind authentication fails if user not found in LDAP",
        log_unlike=[r"connection authenticated:"])
    _access(
        node, "test1", 2,
        "simple bind authentication fails with wrong password",
        log_unlike=[r"connection authenticated:"])

    os.environ["PGPASSWORD"] = "secret1"
    _access(
        node, "test1", 0,
        "simple bind authentication succeeds",
        log_like=[
            r'connection authenticated: identity="uid=test1,dc=example,dc=net" method=ldap'
        ])

    # require_auth=password should complete successfully; other methods should fail.
    node.connect_ok(
        "user=test1 require_auth=password",
        "password authentication required, works with ldap auth")
    node.connect_fails(
        "user=test1 require_auth=scram-sha-256",
        "SCRAM authentication required, fails with ldap auth")

    # search+bind
    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}"'
    )

    os.environ["PGPASSWORD"] = "wrong"
    _access(node, "test0", 2,
            "search+bind authentication fails if user not found in LDAP")
    _access(node, "test1", 2,
            "search+bind authentication fails with wrong password")
    os.environ["PGPASSWORD"] = "secret1"
    _access(
        node, "test1", 0,
        "search+bind authentication succeeds",
        log_like=[
            r'connection authenticated: identity="uid=test1,dc=example,dc=net" method=ldap'
        ])

    # multiple servers
    set_hba(
        f'local all all ldap ldapserver="{ldap_server_name} {ldap_server_name}" '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}"'
    )

    os.environ["PGPASSWORD"] = "wrong"
    _access(node, "test0", 2,
            "search+bind authentication fails if user not found in LDAP")
    _access(node, "test1", 2,
            "search+bind authentication fails with wrong password")
    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 0, "search+bind authentication succeeds")

    # LDAP URLs
    set_hba(
        f'local all all ldap ldapurl="{ldap_url}" ldapprefix="uid=" '
        f'ldapsuffix=",dc=example,dc=net"'
    )

    os.environ["PGPASSWORD"] = "wrong"
    _access(node, "test0", 2,
            "simple bind with LDAP URL authentication fails if user not found in LDAP")
    _access(node, "test1", 2,
            "simple bind with LDAP URL authentication fails with wrong password")
    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 0,
            "simple bind with LDAP URL authentication succeeds")

    set_hba(f'local all all ldap ldapurl="{ldap_url}/{ldap_basedn}?uid?sub"')

    os.environ["PGPASSWORD"] = "wrong"
    _access(node, "test0", 2,
            "search+bind with LDAP URL authentication fails if user not found in LDAP")
    _access(node, "test1", 2,
            "search+bind with LDAP URL authentication fails with wrong password")
    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 0,
            "search+bind with LDAP URL authentication succeeds")

    # search filters
    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}" '
        f'ldapsearchfilter="(|(uid=$username)(mail=$username))"'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(
        node, "test1", 0,
        "search filter finds by uid",
        log_like=[
            r'connection authenticated: identity="uid=test1,dc=example,dc=net" method=ldap'
        ])
    os.environ["PGPASSWORD"] = "secret2"
    _access(
        node, "test2@example.net", 0,
        "search filter finds by mail",
        log_like=[
            r'connection authenticated: identity="uid=test2,dc=example,dc=net" method=ldap'
        ])

    # search filters in LDAP URLs
    set_hba(
        f'local all all ldap '
        f'ldapurl="{ldap_url}/{ldap_basedn}??sub?(|(uid=$username)(mail=$username))"'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 0, "search filter finds by uid")
    os.environ["PGPASSWORD"] = "secret2"
    _access(node, "test2@example.net", 0, "search filter finds by mail")

    # This is not documented: You can combine ldapurl and other ldap*
    # settings.  ldapurl is always parsed first, then the other settings
    # override.  It might be useful in a case like this.
    set_hba(
        f'local all all ldap ldapurl="{ldap_url}/{ldap_basedn}??sub" '
        f'ldapsearchfilter="(|(uid=$username)(mail=$username))"'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 0, "combined LDAP URL and search filter")

    # diagnostic message

    # note bad ldapprefix with a question mark that triggers a diagnostic message
    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapprefix="?uid=" ldapsuffix=""'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 2, "any attempt fails due to bad search pattern")

    # TLS

    # request StartTLS with ldaptls=1
    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}" '
        f'ldapsearchfilter="(uid=$username)" ldaptls=1'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 0, "StartTLS")

    # request LDAPS with ldapscheme=ldaps
    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} ldapscheme=ldaps '
        f'ldapport={ldaps_port} ldapbasedn="{ldap_basedn}" '
        f'ldapsearchfilter="(uid=$username)"'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 0, "LDAPS")

    # request LDAPS with ldapurl=ldaps://...
    set_hba(
        f'local all all ldap '
        f'ldapurl="{ldaps_url}/{ldap_basedn}??sub?(uid=$username)"'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 0, "LDAPS with URL")

    # bad combination of LDAPS and StartTLS
    set_hba(
        f'local all all ldap '
        f'ldapurl="{ldaps_url}/{ldap_basedn}??sub?(uid=$username)" ldaptls=1'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 2, "bad combination of LDAPS and StartTLS")
