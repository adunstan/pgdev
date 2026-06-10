# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test password normalization in SCRAM.

These tests can only run with Unix-domain sockets, so the module is skipped
when the framework is running over TCP (Windows).

The passwords below contain non-ASCII characters, taken from the example
strings of RFC4013.txt, Section "3. Examples".  They are byte-exact UTF-8
strings.  The cluster is initialised with ``--locale=C --encoding=UTF8`` (the
framework default).
"""

import os

import pytest

from pypg.util import USE_UNIX_SOCKETS

pytestmark = pytest.mark.skipif(
    not USE_UNIX_SOCKETS, reason="test requires Unix-domain sockets"
)


def _write_pgpass(path, password):
    """Write a password file (matching any connection) holding the exact
    *password* bytes.

    The password is delivered through a password file rather than PGPASSWORD
    because libpq reads the file content verbatim, with no character-set
    conversion, so the exact bytes reach the SCRAM exchange on every platform.
    An environment variable would instead be re-encoded through the process
    code page on Windows, corrupting non-ASCII bytes.
    """
    escaped = password.replace(b"\\", b"\\\\").replace(b":", b"\\:")
    with open(path, "wb") as fh:
        fh.write(b"*:*:*:*:" + escaped + b"\n")
    os.chmod(path, 0o600)


# Delete pg_hba.conf from the given node, add a new entry to it
# and then execute a reload to refresh it.
def reset_pg_hba(node, hba_method):
    os.unlink(os.path.join(node.data_dir, "pg_hba.conf"))
    node.append_conf(f"local all all {hba_method}", filename="pg_hba.conf")
    node.reload()


# Test access for a single role, useful to wrap all tests into one.
# (Named with a leading underscore so pytest does not collect it as a test.)
# *password* is a bytes object, written to *pgpass* so libpq sees the exact
# bytes.  *expected_res* is 0 for a successful login, non-zero otherwise.
def _test_login(node, pgpass, role, password, expected_res):
    status_string = "success" if expected_res == 0 else "failed"

    connstr = f"user={role}"
    testname = (
        f"authentication {status_string} for role {role} "
        f"with password {password!r}"
    )

    _write_pgpass(pgpass, password)
    if expected_res == 0:
        node.connect_ok(connstr, testname)
    else:
        # No checks of the error message, only the status code.
        node.connect_fails(connstr, testname)


def test_002_saslprep(create_pg, tmp_path):
    # Initialize primary node.  Force UTF-8 encoding, so that we can use
    # non-ASCII characters in the passwords below (the framework's init
    # already uses --locale=C --encoding=UTF8).
    node = create_pg("primary")

    pgpass = os.path.join(str(tmp_path), "saslprep_pgpass.conf")

    # Point libpq at our password file and make sure no PGPASSWORD overrides
    # it; restore both so the rest of the suite is unaffected.
    saved_file = os.environ.get("PGPASSFILE")
    saved_pw = os.environ.get("PGPASSWORD")
    os.environ["PGPASSFILE"] = pgpass
    os.environ.pop("PGPASSWORD", None)
    try:
        _run_body(node, pgpass)
    finally:
        if saved_file is None:
            os.environ.pop("PGPASSFILE", None)
        else:
            os.environ["PGPASSFILE"] = saved_file
        if saved_pw is not None:
            os.environ["PGPASSWORD"] = saved_pw


def _run_body(node, pgpass):
    # These tests are based on the example strings from RFC4013.txt,
    # Section "3. Examples":
    #
    # #  Input            Output     Comments
    # -  -----            ------     --------
    # 1  I<U+00AD>X       IX         SOFT HYPHEN mapped to nothing
    # 2  user             user       no transformation
    # 3  USER             USER       case preserved, will not match #2
    # 4  <U+00AA>         a          output is NFKC, input in ISO 8859-1
    # 5  <U+2168>         IX         output is NFKC, will match #1
    # 6  <U+0007>                    Error - prohibited character
    # 7  <U+0627><U+0031>            Error - bidirectional check

    # Create test roles.
    node.safe_sql(
        "SET password_encryption='scram-sha-256';\n"
        "SET client_encoding='utf8';\n"
        "CREATE ROLE saslpreptest1_role LOGIN PASSWORD 'IX';\n"
        "CREATE ROLE saslpreptest4a_role LOGIN PASSWORD 'a';\n"
        "CREATE ROLE saslpreptest4b_role LOGIN PASSWORD E'\\xc2\\xaa';\n"
        "CREATE ROLE saslpreptest6_role LOGIN PASSWORD E'foo\\x07bar';\n"
        "CREATE ROLE saslpreptest7_role LOGIN PASSWORD E'foo\\u0627\\u0031bar';\n"
    )

    # Require password from now on.
    reset_pg_hba(node, "scram-sha-256")

    # Check that #1 and #5 are treated the same as just 'IX'
    _test_login(node, pgpass, "saslpreptest1_role", b"I\xc2\xadX", 0)
    _test_login(node, pgpass, "saslpreptest1_role", b"\xe2\x85\xa8", 0)

    # but different from lower case 'ix'
    _test_login(node, pgpass, "saslpreptest1_role", b"ix", 2)

    # Check #4
    _test_login(node, pgpass, "saslpreptest4a_role", b"a", 0)
    _test_login(node, pgpass, "saslpreptest4a_role", b"\xc2\xaa", 0)
    _test_login(node, pgpass, "saslpreptest4b_role", b"a", 0)
    _test_login(node, pgpass, "saslpreptest4b_role", b"\xc2\xaa", 0)

    # Check #6 and #7 - In PostgreSQL, contrary to the spec, if the password
    # contains prohibited characters, we use it as is, without normalization.
    _test_login(node, pgpass, "saslpreptest6_role", b"foo\x07bar", 0)
    _test_login(node, pgpass, "saslpreptest6_role", b"foobar", 2)

    _test_login(node, pgpass, "saslpreptest7_role", b"foo\xd8\xa71bar", 0)
    _test_login(node, pgpass, "saslpreptest7_role", b"foo1\xd8\xa7bar", 2)
    _test_login(node, pgpass, "saslpreptest7_role", b"foobar", 2)
