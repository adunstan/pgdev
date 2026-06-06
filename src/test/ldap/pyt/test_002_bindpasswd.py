# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Tests for LDAP authentication using ldapbindpasswd."""

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


def test_002_bindpasswd(create_pg, ldap_server):
    # Faithful skip ordering.
    if os.environ.get("with_ldap") != "yes":
        pytest.skip("LDAP not supported by this build")
    if "ldap" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip("Potentially unsafe test LDAP not enabled in PG_TEST_EXTRA")
    # The ldap_server fixture covers the slapd-availability skip.

    # setting up LDAP server
    ldap_rootpw = "secret"
    ldap = ldap_server(ldap_rootpw, "users")  # no anonymous auth
    authdata = os.path.join(os.path.dirname(__file__), "..", "authdata.ldif")
    ldap.ldapadd_file(authdata)
    ldap.ldapsetpw("uid=test1,dc=example,dc=net", "secret1")
    ldap.ldapsetpw("uid=test2,dc=example,dc=net", "secret2")

    (ldap_server_name, ldap_port, ldap_basedn, ldap_rootdn) = ldap.prop(
        "server", "port", "basedn", "rootdn")

    # setting up PostgreSQL instance
    node = create_pg("node", start=False)
    node.append_conf("log_connections = all\n")
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

    # use ldapbindpasswd

    # Note: the malformed ldapbinddn (unbalanced quote) is intentional and
    # exercises the failure path.
    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}" '
        f'ldapbinddn="{ldap_rootdn} ldapbindpasswd=wrong'
    )

    os.environ["PGPASSWORD"] = "secret1"
    _access(node, "test1", 2,
            "search+bind authentication fails with wrong ldapbindpasswd")

    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}" '
        f'ldapbinddn="{ldap_rootdn}" ldapbindpasswd="{ldap_rootpw}"'
    )

    _access(node, "test1", 0,
            "search+bind authentication succeeds with ldapbindpasswd")
