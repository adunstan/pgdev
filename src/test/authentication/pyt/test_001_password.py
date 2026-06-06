# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Set of tests for authentication and pg_hba.conf.

The following password methods are checked through this test:
 - Plain
 - MD5-encrypted
 - SCRAM-encrypted

There's also a few tests of the log_connections GUC here.

These tests require Unix-domain sockets, so the module is skipped when the
framework is running over TCP (Windows).
"""

import os
import time

import pytest

from libpq import Session
from pypg.util import USE_UNIX_SOCKETS

pytestmark = pytest.mark.skipif(
    not USE_UNIX_SOCKETS, reason="test requires Unix-domain sockets"
)


# Delete pg_hba.conf from the given node, add a new entry to it
# and then execute a reload to refresh it.
def reset_pg_hba(node, database, role, hba_method):
    os.unlink(os.path.join(node.data_dir, "pg_hba.conf"))
    # just for testing purposes, use a continuation line
    node.append_conf(
        f"local {database} {role}\\\n {hba_method}", filename="pg_hba.conf"
    )
    node.reload()


# Test access for a connection string, useful to wrap all tests into one.
# Extra named parameters are passed to connect_ok/fails as-is.
# (Named with a leading underscore so pytest does not collect it as a test.)
def _test_conn(node, connstr, method, expected_res, **params):
    status_string = "success" if expected_res == 0 else "failed"

    testname = f"authentication {status_string} for method {method}, connstr {connstr}"

    if expected_res == 0:
        node.connect_ok(connstr, testname, **params)
    else:
        # No checks of the error message, only the status code.
        node.connect_fails(connstr, testname, **params)


def test_001_password(create_pg):
    # Initialize primary node
    node = create_pg("primary", start=False)
    node.append_conf("log_connections = on\n")
    # Needed to allow connect_fails to inspect postmaster log:
    node.append_conf("log_min_messages = debug2")
    node.append_conf("password_expiration_warning_threshold = '1100d'")
    node.start()

    # Snapshot/restore env vars we mutate (PGPASSWORD, PGCHANNELBINDING,
    # PGPASSFILE) so the rest of the suite is unaffected.
    saved_env = {
        k: os.environ.get(k)
        for k in ("PGPASSWORD", "PGCHANNELBINDING", "PGPASSFILE", "PGDATABASE")
    }
    try:
        _run_body(node)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_body(node):
    # Set up roles for password_expiration_warning_threshold test
    current_year = time.localtime().tm_year
    expire_year = current_year - 1
    node.safe_sql(
        f"CREATE ROLE expired LOGIN VALID UNTIL '{expire_year}-01-01' PASSWORD 'pass'"
    )
    expire_year = current_year + 2
    node.safe_sql(
        "CREATE ROLE expiration_warnings LOGIN VALID UNTIL "
        f"'{expire_year}-01-01' PASSWORD 'pass'"
    )
    expire_year = current_year + 5
    node.safe_sql(
        f"CREATE ROLE no_warnings LOGIN VALID UNTIL '{expire_year}-01-01' PASSWORD 'pass'"
    )

    # Test behavior of log_connections GUC
    #
    # There wasn't another test file where these tests obviously fit, and we
    # don't want to incur the cost of spinning up a new cluster just to test
    # one GUC.

    # Make a database for the log_connections tests to avoid test fragility if
    # other tests are added to this file in the future
    node.safe_sql("CREATE DATABASE test_log_connections")

    log_connections = node.safe_sql(
        "SHOW log_connections;", dbname="test_log_connections"
    )
    assert log_connections == "on", "check log connections has expected value 'on'"

    node.connect_ok(
        "dbname=test_log_connections",
        "log_connections 'on' works as expected for backwards compatibility",
        log_like=[
            r"connection received",
            r"connection authenticated",
            r"connection authorized: user=\S+ database=test_log_connections",
        ],
        log_unlike=[r"connection ready"],
    )

    node.safe_sql(
        "ALTER SYSTEM SET log_connections = receipt,authorization,setup_durations;",
        dbname="test_log_connections",
    )
    node.safe_sql("SELECT pg_reload_conf();", dbname="test_log_connections")

    node.connect_ok(
        "dbname=test_log_connections",
        "log_connections with subset of specified options logs only those aspects",
        log_like=[
            r"connection received",
            r"connection authorized: user=\S+ database=test_log_connections",
            r"connection ready",
        ],
        log_unlike=[r"connection authenticated"],
    )

    node.safe_sql(
        "ALTER SYSTEM SET log_connections = 'all';",
        dbname="test_log_connections",
    )
    node.safe_sql("SELECT pg_reload_conf();", dbname="test_log_connections")

    node.connect_ok(
        "dbname=test_log_connections",
        "log_connections 'all' logs all available connection aspects",
        log_like=[
            r"connection received",
            r"connection authenticated",
            r"connection authorized: user=\S+ database=test_log_connections",
            r"connection ready",
        ],
    )

    # Authentication tests

    # could fail in FIPS mode
    md5_works = node.sql("select md5('')").error_message is None

    # Create 3 roles with different password methods for each one. The same
    # password is used for all of them.
    res = node.sql(
        "SET password_encryption='scram-sha-256';"
        " CREATE ROLE scram_role LOGIN PASSWORD 'pass';"
    )
    assert res.error_message is None, "created user with SCRAM password"

    res = node.sql(
        "SET password_encryption='md5'; CREATE ROLE md5_role LOGIN PASSWORD 'pass';"
    )
    if md5_works:
        assert res.error_message is None, "created user with md5 password"
    else:
        assert res.error_message is not None, "created user with md5 password"

    # Set up a table for tests of SYSTEM_USER.
    node.safe_sql(
        "CREATE TABLE sysuser_data (n) AS SELECT NULL FROM generate_series(1, 10);"
        " GRANT ALL ON sysuser_data TO scram_role;"
    )
    os.environ["PGPASSWORD"] = "pass"

    # Create a role that contains a comma to stress the parsing.
    node.safe_sql(
        "SET password_encryption='scram-sha-256';"
        " CREATE ROLE \"scram,role\" LOGIN PASSWORD 'pass';"
    )

    # Create a role with a non-default iteration count
    node.safe_sql(
        "SET password_encryption='scram-sha-256';"
        " SET scram_iterations=1024;"
        " CREATE ROLE scram_role_iter LOGIN PASSWORD 'pass';"
        " RESET scram_iterations;"
    )

    res = node.safe_sql(
        "SELECT substr(rolpassword,1,19)"
        " FROM pg_authid"
        " WHERE rolname = 'scram_role_iter'"
    )
    assert res == "SCRAM-SHA-256$1024:", "scram_iterations in server side ROLE"

    # set password using PQchangePassword
    session = Session(connstr=node.connstr("postgres"), libdir=node.libdir)
    try:
        session.do(
            "SET password_encryption='scram-sha-256';",
            "SET scram_iterations=42;",
        )
        res = session.set_password("scram_role_iter", "pass")
        assert res.status == 1, "set password ok"
        res = session.query_oneval(
            "SELECT substr(rolpassword,1,17)"
            " FROM pg_authid"
            " WHERE rolname = 'scram_role_iter'"
        )
        assert res == "SCRAM-SHA-256$42:", "scram_iterations correct"
    finally:
        session.close()

    # Create a database to test regular expression.
    node.safe_sql("CREATE database regex_testdb;")

    # For "trust" method, all users should be able to connect.
    reset_pg_hba(node, "all", "all", "trust")
    _test_conn(
        node,
        "user=scram_role",
        "trust",
        0,
        log_like=[r'connection authenticated: user="scram_role" method=trust'],
    )
    if md5_works:
        _test_conn(
            node,
            "user=md5_role",
            "trust",
            0,
            log_like=[r'connection authenticated: user="md5_role" method=trust'],
        )

    # SYSTEM_USER is null when not authenticated.
    res = node.safe_sql("SELECT SYSTEM_USER IS NULL;")
    assert res == "t", "users with trust authentication use SYSTEM_USER = NULL"

    # Test SYSTEM_USER with parallel workers when not authenticated.
    sess = node.connect(user="scram_role")
    try:
        res = sess.query_oneval(
            "SET min_parallel_table_scan_size TO 0;"
            "SET parallel_setup_cost TO 0;"
            "SET parallel_tuple_cost TO 0;"
            "SET max_parallel_workers_per_gather TO 2;"
            "SELECT bool_and(SYSTEM_USER IS NOT DISTINCT FROM n) FROM sysuser_data;"
        )
    finally:
        sess.close()
    assert (
        res == "t"
    ), "users with trust authentication use SYSTEM_USER = NULL in parallel workers"

    # Explicitly specifying an empty require_auth (the default) should always
    # succeed.
    node.connect_ok("user=scram_role require_auth=", "empty require_auth succeeds")

    # All these values of require_auth should fail, as trust is expected.
    node.connect_fails(
        "user=scram_role require_auth=gss",
        "GSS authentication required, fails with trust auth",
        expected_stderr=r'authentication method requirement "gss" failed: server did not complete authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=sspi",
        "SSPI authentication required, fails with trust auth",
        expected_stderr=r'authentication method requirement "sspi" failed: server did not complete authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=password",
        "password authentication required, fails with trust auth",
        expected_stderr=r'authentication method requirement "password" failed: server did not complete authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=md5",
        "MD5 authentication required, fails with trust auth",
        expected_stderr=r'authentication method requirement "md5" failed: server did not complete authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=scram-sha-256",
        "SCRAM authentication required, fails with trust auth",
        expected_stderr=r'authentication method requirement "scram-sha-256" failed: server did not complete authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=password,scram-sha-256",
        "password and SCRAM authentication required, fails with trust auth",
        expected_stderr=r'authentication method requirement "password,scram-sha-256" failed: server did not complete authentication',
    )

    # These negative patterns of require_auth should succeed.
    node.connect_ok(
        "user=scram_role require_auth=!gss",
        "GSS authentication can be forbidden, succeeds with trust auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=!sspi",
        "SSPI authentication can be forbidden, succeeds with trust auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=!password",
        "password authentication can be forbidden, succeeds with trust auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=!md5",
        "md5 authentication can be forbidden, succeeds with trust auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=!scram-sha-256",
        "SCRAM authentication can be forbidden, succeeds with trust auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=!password,!scram-sha-256",
        "multiple authentication types forbidden, succeeds with trust auth",
    )

    # require_auth=[!]none should interact correctly with trust auth.
    node.connect_ok(
        "user=scram_role require_auth=none",
        "all authentication types forbidden, succeeds with trust auth",
    )
    node.connect_fails(
        "user=scram_role require_auth=!none",
        "any authentication types required, fails with trust auth",
        expected_stderr=r"server did not complete authentication",
    )

    # Negative and positive require_auth methods can't be mixed.
    node.connect_fails(
        "user=scram_role require_auth=scram-sha-256,!md5",
        "negative require_auth methods cannot be mixed with positive ones",
        expected_stderr=r'negative require_auth method "!md5" cannot be mixed with non-negative methods',
    )
    node.connect_fails(
        "user=scram_role require_auth=!password,!none,scram-sha-256",
        "positive require_auth methods cannot be mixed with negative one",
        expected_stderr=r'require_auth method "scram-sha-256" cannot be mixed with negative methods',
    )

    # require_auth methods cannot have duplicated values.
    node.connect_fails(
        "user=scram_role require_auth=password,md5,password",
        "require_auth methods cannot include duplicates, positive case",
        expected_stderr=r'require_auth method "password" is specified more than once',
    )
    node.connect_fails(
        "user=scram_role require_auth=!password,!md5,!password",
        "require_auth methods cannot be duplicated, negative case",
        expected_stderr=r'require_auth method "!password" is specified more than once',
    )
    node.connect_fails(
        "user=scram_role require_auth=none,md5,none",
        "require_auth methods cannot be duplicated, none case",
        expected_stderr=r'require_auth method "none" is specified more than once',
    )
    node.connect_fails(
        "user=scram_role require_auth=!none,!md5,!none",
        "require_auth methods cannot be duplicated, !none case",
        expected_stderr=r'require_auth method "!none" is specified more than once',
    )
    node.connect_fails(
        "user=scram_role require_auth=scram-sha-256,scram-sha-256",
        "require_auth methods cannot be duplicated, scram-sha-256 case",
        expected_stderr=r'require_auth method "scram-sha-256" is specified more than once',
    )
    node.connect_fails(
        "user=scram_role require_auth=!scram-sha-256,!scram-sha-256",
        "require_auth methods cannot be duplicated, !scram-sha-256 case",
        expected_stderr=r'require_auth method "!scram-sha-256" is specified more than once',
    )

    # Unknown value defined in require_auth.
    node.connect_fails(
        "user=scram_role require_auth=none,abcdefg",
        "unknown require_auth methods are rejected",
        expected_stderr=r'invalid require_auth value: "abcdefg"',
    )

    # For plain "password" method, all users should also be able to connect.
    reset_pg_hba(node, "all", "all", "password")
    _test_conn(
        node,
        "user=scram_role",
        "password",
        0,
        log_like=[r'connection authenticated: identity="scram_role" method=password'],
    )
    if md5_works:
        _test_conn(
            node,
            "user=md5_role",
            "password",
            0,
            log_like=[r'connection authenticated: identity="md5_role" method=password'],
        )

    # require_auth succeeds here with a plaintext password.
    node.connect_ok(
        "user=scram_role require_auth=password",
        "password authentication required, works with password auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=!none",
        "any authentication required, works with password auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=scram-sha-256,password,md5",
        "multiple authentication types required, works with password auth",
    )

    # require_auth fails for other authentication types.
    node.connect_fails(
        "user=scram_role require_auth=md5",
        "md5 authentication required, fails with password auth",
        expected_stderr=r'authentication method requirement "md5" failed: server requested a cleartext password',
    )
    node.connect_fails(
        "user=scram_role require_auth=scram-sha-256",
        "SCRAM authentication required, fails with password auth",
        expected_stderr=r'authentication method requirement "scram-sha-256" failed: server requested a cleartext password',
    )
    node.connect_fails(
        "user=scram_role require_auth=none",
        "all authentication forbidden, fails with password auth",
        expected_stderr=r'authentication method requirement "none" failed: server requested a cleartext password',
    )

    # Disallowing password authentication fails, even if requested by server.
    node.connect_fails(
        "user=scram_role require_auth=!password",
        "password authentication forbidden, fails with password auth",
        expected_stderr=r"server requested a cleartext password",
    )
    node.connect_fails(
        "user=scram_role require_auth=!password,!md5,!scram-sha-256",
        "multiple authentication types forbidden, fails with password auth",
        expected_stderr=r' method requirement "!password,!md5,!scram-sha-256" failed: server requested a cleartext password',
    )

    # For "scram-sha-256" method, user "scram_role" should be able to connect.
    reset_pg_hba(node, "all", "all", "scram-sha-256")
    _test_conn(
        node,
        "user=scram_role",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="scram_role" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=scram_role_iter",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="scram_role_iter" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=md5_role",
        "scram-sha-256",
        2,
        log_unlike=[r"connection authenticated:"],
    )

    # require_auth should succeed with SCRAM when it is required.
    node.connect_ok(
        "user=scram_role require_auth=scram-sha-256",
        "SCRAM authentication required, works with SCRAM auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=!none",
        "any authentication required, works with SCRAM auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=password,scram-sha-256,md5",
        "multiple authentication types required, works with SCRAM auth",
    )

    # Authentication fails for other authentication types.
    node.connect_fails(
        "user=scram_role require_auth=password",
        "password authentication required, fails with SCRAM auth",
        expected_stderr=r'authentication method requirement "password" failed: server requested SASL authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=md5",
        "md5 authentication required, fails with SCRAM auth",
        expected_stderr=r'authentication method requirement "md5" failed: server requested SASL authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=none",
        "all authentication forbidden, fails with SCRAM auth",
        expected_stderr=r'authentication method requirement "none" failed: server requested SASL authentication',
    )

    # Authentication fails if SCRAM authentication is forbidden.
    node.connect_fails(
        "user=scram_role require_auth=!scram-sha-256",
        "SCRAM authentication forbidden, fails with SCRAM auth",
        expected_stderr=r"server requested SCRAM-SHA-256 authentication",
    )
    node.connect_fails(
        "user=scram_role require_auth=!password,!md5,!scram-sha-256",
        "multiple authentication types forbidden, fails with SCRAM auth",
        expected_stderr=r"server requested SCRAM-SHA-256 authentication",
    )

    # Test that bad passwords are rejected.
    os.environ["PGPASSWORD"] = "badpass"
    _test_conn(
        node,
        "user=scram_role",
        "scram-sha-256",
        2,
        log_unlike=[r"connection authenticated:"],
    )
    os.environ["PGPASSWORD"] = "pass"

    # For "md5" method, all users should be able to connect (SCRAM
    # authentication will be performed for the user with a SCRAM secret.)
    reset_pg_hba(node, "all", "all", "md5")
    _test_conn(
        node,
        "user=scram_role",
        "md5",
        0,
        log_like=[r'connection authenticated: identity="scram_role" method=md5'],
    )
    if md5_works:
        _test_conn(
            node,
            "user=md5_role",
            "md5",
            0,
            expected_stderr=r"authenticated with an MD5-encrypted password",
            log_like=[r'connection authenticated: identity="md5_role" method=md5'],
        )

    # require_auth succeeds with SCRAM required.
    node.connect_ok(
        "user=scram_role require_auth=scram-sha-256",
        "SCRAM authentication required, works with SCRAM auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=!none",
        "any authentication required, works with SCRAM auth",
    )
    node.connect_ok(
        "user=scram_role require_auth=md5,scram-sha-256,password",
        "multiple authentication types required, works with SCRAM auth",
    )

    # Authentication fails if other types are required.
    node.connect_fails(
        "user=scram_role require_auth=password",
        "password authentication required, fails with SCRAM auth",
        expected_stderr=r'authentication method requirement "password" failed: server requested SASL authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=md5",
        "MD5 authentication required, fails with SCRAM auth",
        expected_stderr=r'authentication method requirement "md5" failed: server requested SASL authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=none",
        "all authentication types forbidden, fails with SCRAM auth",
        expected_stderr=r'authentication method requirement "none" failed: server requested SASL authentication',
    )

    # Authentication fails if SCRAM is forbidden.
    node.connect_fails(
        "user=scram_role require_auth=!scram-sha-256",
        "password authentication forbidden, fails with SCRAM auth",
        expected_stderr=r'authentication method requirement "!scram-sha-256" failed: server requested SCRAM-SHA-256 authentication',
    )
    node.connect_fails(
        "user=scram_role require_auth=!password,!md5,!scram-sha-256",
        "multiple authentication types forbidden, fails with SCRAM auth",
        expected_stderr=r'authentication method requirement "!password,!md5,!scram-sha-256" failed: server requested SCRAM-SHA-256 authentication',
    )

    # Test password_expiration_warning_threshold
    node.connect_fails(
        "user=expired dbname=postgres",
        "connection fails due to expired password",
        expected_stderr=r'password authentication failed for user "expired"',
    )
    node.connect_ok(
        "user=expiration_warnings dbname=postgres",
        "connection succeeds with password expiration warning",
        expected_stderr=r"role password will expire soon",
    )
    node.connect_ok(
        "user=no_warnings dbname=postgres",
        "connection succeeds with no password expiration warning",
    )

    # Test SYSTEM_USER <> NULL with parallel workers.  Pass the password
    # explicitly rather than via PGPASSWORD: this is an in-process libpq
    # connection, and the in-process library does not portably read the
    # environment (which is why connect_ok/connect_fails shell out to psql).
    sess = node.connect(user="scram_role", password="pass")
    try:
        sess.do(
            "TRUNCATE sysuser_data;",
            "INSERT INTO sysuser_data SELECT 'md5:scram_role'"
            " FROM generate_series(1, 10);",
        )
        res = sess.query_oneval(
            "SET min_parallel_table_scan_size TO 0;"
            "SET parallel_setup_cost TO 0;"
            "SET parallel_tuple_cost TO 0;"
            "SET max_parallel_workers_per_gather TO 2;"
            "SELECT bool_and(SYSTEM_USER IS NOT DISTINCT FROM n) FROM sysuser_data;"
        )
    finally:
        sess.close()
    assert (
        res == "t"
    ), "users with md5 authentication use SYSTEM_USER = md5:role in parallel workers"

    # Tests for channel binding without SSL.
    # Using the password authentication method; channel binding can't work
    reset_pg_hba(node, "all", "all", "password")
    os.environ["PGCHANNELBINDING"] = "require"
    _test_conn(node, "user=scram_role", "scram-sha-256", 2)
    # SSL not in use; channel binding still can't work
    reset_pg_hba(node, "all", "all", "scram-sha-256")
    os.environ["PGCHANNELBINDING"] = "require"
    _test_conn(node, "user=scram_role", "scram-sha-256", 2)

    # Test .pgpass processing; but use a temp file, don't overwrite the real one!
    pgpassfile = os.path.join(node.basedir, "pgpass")

    os.environ.pop("PGPASSWORD", None)
    os.environ.pop("PGCHANNELBINDING", None)
    os.environ["PGPASSFILE"] = pgpassfile

    if os.path.exists(pgpassfile):
        os.unlink(pgpassfile)
    with open(pgpassfile, "a", encoding="utf-8") as fh:
        fh.write(
            "\n"
            "# This very long comment is just here to exercise handling of long lines in the file. "
            "This very long comment is just here to exercise handling of long lines in the file. "
            "This very long comment is just here to exercise handling of long lines in the file. "
            "This very long comment is just here to exercise handling of long lines in the file. "
            "This very long comment is just here to exercise handling of long lines in the file.\n"
            "*:*:postgres:scram_role:pass:this is not part of the password.\n"
        )
    os.chmod(pgpassfile, 0o600)

    reset_pg_hba(node, "all", "all", "password")
    _test_conn(node, "user=scram_role", "password from pgpass", 0)
    _test_conn(node, "user=md5_role", "password from pgpass", 2)

    with open(pgpassfile, "a", encoding="utf-8") as fh:
        fh.write("\n*:*:*:scram_role:p\\ass\n*:*:*:scram,role:p\\ass\n")

    _test_conn(node, "user=scram_role", "password from pgpass", 0)

    # Testing with regular expression for username.  The third regexp matches.
    reset_pg_hba(node, "all", "/^.*nomatch.*$, baduser, /^scr.*$", "password")
    _test_conn(
        node,
        "user=scram_role",
        "password, matching regexp for username",
        0,
        log_like=[r'connection authenticated: identity="scram_role" method=password'],
    )

    # The third regex does not match anymore.
    reset_pg_hba(node, "all", "/^.*nomatch.*$, baduser, /^sc_r.*$", "password")
    _test_conn(
        node,
        "user=scram_role",
        "password, non matching regexp for username",
        2,
        log_unlike=[r"connection authenticated:"],
    )

    # Test with a comma in the regular expression.  In this case, the use of
    # double quotes is mandatory so as this is not considered as two elements
    # of the user name list when parsing pg_hba.conf.
    reset_pg_hba(node, "all", '"/^.*m,.*e$"', "password")
    _test_conn(
        node,
        "user=scram,role",
        "password, matching regexp for username",
        0,
        log_like=[r'connection authenticated: identity="scram,role" method=password'],
    )

    # Testing with regular expression for dbname. The third regex matches.
    reset_pg_hba(node, "/^.*nomatch.*$, baddb, /^regex_t.*b$", "all", "password")
    _test_conn(
        node,
        "user=scram_role dbname=regex_testdb",
        "password, matching regexp for dbname",
        0,
        log_like=[r'connection authenticated: identity="scram_role" method=password'],
    )

    # The third regexp does not match anymore.
    reset_pg_hba(node, "/^.*nomatch.*$, baddb, /^regex_t.*ba$", "all", "password")
    _test_conn(
        node,
        "user=scram_role dbname=regex_testdb",
        "password, non matching regexp for dbname",
        2,
        log_unlike=[r"connection authenticated:"],
    )

    os.unlink(pgpassfile)
    os.environ.pop("PGPASSFILE", None)

    print("# Authentication tests with specific HBA policies on roles")

    # Create database and roles for membership tests
    reset_pg_hba(node, "all", "all", "trust")
    # Database and root role names match for "samerole" and "samegroup".
    node.safe_sql("CREATE DATABASE regress_regression_group;")
    node.safe_sql(
        "CREATE ROLE regress_regression_group LOGIN PASSWORD 'pass';"
        "CREATE ROLE regress_member LOGIN SUPERUSER IN ROLE regress_regression_group PASSWORD 'pass';"
        "CREATE ROLE regress_not_member LOGIN SUPERUSER PASSWORD 'pass';"
    )

    # Test role with exact matching, no members allowed.
    os.environ["PGPASSWORD"] = "pass"
    reset_pg_hba(node, "all", "regress_regression_group", "scram-sha-256")
    _test_conn(
        node,
        "user=regress_regression_group",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="regress_regression_group" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=regress_member",
        "scram-sha-256",
        2,
        log_unlike=[
            r'connection authenticated: identity="regress_member" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=regress_not_member",
        "scram-sha-256",
        2,
        log_unlike=[
            r'connection authenticated: identity="regress_not_member" method=scram-sha-256'
        ],
    )

    # Test role membership with '+', where all the members are allowed
    # to connect.
    reset_pg_hba(node, "all", "+regress_regression_group", "scram-sha-256")
    _test_conn(
        node,
        "user=regress_regression_group",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="regress_regression_group" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=regress_member",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="regress_member" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=regress_not_member",
        "scram-sha-256",
        2,
        log_unlike=[
            r'connection authenticated: identity="regress_not_member" method=scram-sha-256'
        ],
    )

    # Test role membership is respected for samerole.
    # connect_ok forces dbname=postgres unless the connstr overrides it, so
    # pass the database explicitly via PGDATABASE.
    os.environ["PGDATABASE"] = "regress_regression_group"
    reset_pg_hba(node, "samerole", "all", "scram-sha-256")
    _test_conn(
        node,
        "user=regress_regression_group dbname=regress_regression_group",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="regress_regression_group" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=regress_member dbname=regress_regression_group",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="regress_member" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=regress_not_member dbname=regress_regression_group",
        "scram-sha-256",
        2,
        log_unlike=[
            r'connection authenticated: identity="regress_not_member" method=scram-sha-256'
        ],
    )

    # Test role membership is respected for samegroup
    reset_pg_hba(node, "samegroup", "all", "scram-sha-256")
    _test_conn(
        node,
        "user=regress_regression_group dbname=regress_regression_group",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="regress_regression_group" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=regress_member dbname=regress_regression_group",
        "scram-sha-256",
        0,
        log_like=[
            r'connection authenticated: identity="regress_member" method=scram-sha-256'
        ],
    )
    _test_conn(
        node,
        "user=regress_not_member dbname=regress_regression_group",
        "scram-sha-256",
        2,
        log_unlike=[
            r'connection authenticated: identity="regress_not_member" method=scram-sha-256'
        ],
    )
