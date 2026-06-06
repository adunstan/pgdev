# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Test that a custom hook can mutate the LDAP bind password.

Verify that LDAP authentication succeeds when the ldap_password_func module
rewrites the configured bind password before it is used.
"""

import os

import pytest


def _access(node, role, expected_res, test_name, **params):
    """Attempt a connection as *role* and assert success or failure.

    *expected_res* of 0 means the connection should succeed; anything else
    means it should fail (only the status code is checked on failure).
    """
    connstr = f"user={role}"
    if expected_res == 0:
        node.connect_ok(connstr, test_name, **params)
    else:
        # No checks of the error message, only the status code.
        node.connect_fails(connstr, test_name, **params)


def test_001_mutated_bindpasswd(create_pg, ldap_server):
    # Skip ordering: check build support, then PG_TEST_EXTRA opt-in.
    if os.environ.get("with_ldap") != "yes":
        pytest.skip("LDAP not supported by this build")
    if "ldap" not in os.environ.get("PG_TEST_EXTRA", "").split():
        pytest.skip("Potentially unsafe test LDAP not enabled in PG_TEST_EXTRA")
    # The ldap_server fixture covers the slapd-availability skip.

    clear_ldap_rootpw = "FooBaR1"
    rot13_ldap_rootpw = "SbbOnE1"

    ldap = ldap_server(clear_ldap_rootpw, "users")  # no anonymous auth
    authdata = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "ldap", "authdata.ldif"
    )
    ldap.ldapadd_file(authdata)
    ldap.ldapsetpw("uid=test1,dc=example,dc=net", "secret1")

    (ldap_server_name, ldap_port, ldap_basedn, ldap_rootdn) = ldap.prop(
        "server", "port", "basedn", "rootdn")

    # setting up PostgreSQL instance
    node = create_pg("node", start=False)
    node.append_conf(
        "log_connections = 'receipt,authentication,authorization'\n")
    node.append_conf("shared_preload_libraries = 'ldap_password_func'")
    node.start()

    node.safe_sql("CREATE USER test1;")

    # running tests

    # use ldapbindpasswd
    os.environ["PGPASSWORD"] = "secret1"

    def set_hba(line):
        """Replace pg_hba.conf with a single ldap line and restart."""
        hba = os.path.join(node.data_dir, "pg_hba.conf")
        os.unlink(hba)
        node.append_conf(line, filename="pg_hba.conf")
        node.restart()

    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}" '
        f'ldapbinddn="{ldap_rootdn}" ldapbindpasswd=wrong'
    )
    _access(node, "test1", 2,
            "search+bind authentication fails with wrong ldapbindpasswd")

    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}" '
        f'ldapbinddn="{ldap_rootdn}" ldapbindpasswd="{clear_ldap_rootpw}"'
    )
    _access(node, "test1", 2,
            "search+bind authentication fails with clear password")

    set_hba(
        f'local all all ldap ldapserver={ldap_server_name} '
        f'ldapport={ldap_port} ldapbasedn="{ldap_basedn}" '
        f'ldapbinddn="{ldap_rootdn}" ldapbindpasswd="{rot13_ldap_rootpw}"'
    )
    _access(node, "test1", 0,
            "search+bind authentication succeeds with rot13ed password")
