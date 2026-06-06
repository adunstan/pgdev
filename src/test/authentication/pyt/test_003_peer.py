# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for peer authentication and user name map.

Peer authentication only works over Unix-domain sockets, so the module is
skipped when the framework is running over TCP (Windows); it is also skipped
if the platform does not support peer authentication (checked at run time).
"""

import getpass
import os
import re

import pytest

from libpq.errors import PqConnectionError
from pypg.util import USE_UNIX_SOCKETS

pytestmark = pytest.mark.skipif(
    not USE_UNIX_SOCKETS, reason="test requires Unix-domain sockets"
)


# Delete pg_hba.conf from the given node, add a new entry to it
# and then execute a reload to refresh it.
def reset_pg_hba(node, hba_method):
    os.unlink(os.path.join(node.data_dir, "pg_hba.conf"))
    node.append_conf(f"local all all {hba_method}", filename="pg_hba.conf")
    node.reload()


# Delete pg_ident.conf from the given node, add a new entry to it
# and then execute a reload to refresh it.
def reset_pg_ident(node, map_name, system_user, pg_user):
    os.unlink(os.path.join(node.data_dir, "pg_ident.conf"))
    node.append_conf(f"{map_name} {system_user} {pg_user}", filename="pg_ident.conf")
    node.reload()


# Test access for a single role, useful to wrap all tests into one.
# (Named with a leading underscore so pytest does not collect it as a test.)
def _test_role(node, role, method, expected_res, test_details, **params):
    status_string = "success" if expected_res == 0 else "failed"

    connstr = f"user={role}"
    testname = (
        f"authentication {status_string} for method {method}, role {role} "
        f"{test_details}"
    )

    if expected_res == 0:
        node.connect_ok(connstr, testname, **params)
    else:
        # No checks of the error message, only the status code.
        node.connect_fails(connstr, testname, **params)


def test_003_peer(create_pg):
    node = create_pg("node", start=False)
    node.append_conf("log_connections = authentication\n")
    # Needed to allow connect_fails to inspect postmaster log:
    node.append_conf("log_min_messages = debug2")
    node.start()

    # Set pg_hba.conf with the peer authentication.
    reset_pg_hba(node, "peer")

    # Check if peer authentication is supported on this platform.
    log_offset = node.log_position()
    # Attempt a connection (as the current OS user) to make the server emit
    # the "not supported" message if peer auth is unavailable.  Where peer auth
    # is unsupported (e.g. Windows) the attempt itself fails, so tolerate the
    # connection error and decide from the server log.
    try:
        node.sql("SELECT 1")
    except PqConnectionError:
        pass
    if node.log_contains(
        r"peer authentication is not supported on this platform", log_offset
    ):
        pytest.skip("peer authentication is not supported on this platform")

    # Add a database role and a group, to use for the user name map.
    node.safe_sql("CREATE ROLE testmapuser LOGIN")
    node.safe_sql("CREATE ROLE testmapgroup NOLOGIN")
    node.safe_sql("GRANT testmapgroup TO testmapuser")
    # Note the double quotes here.
    node.safe_sql(r'CREATE ROLE "testmapgroupliteral\1" LOGIN')
    node.safe_sql(r'GRANT "testmapgroupliteral\1" TO testmapuser')

    # Extract as well the system user for the user name map.
    system_user = node.safe_sql("select (string_to_array(SYSTEM_USER, ':'))[2]")
    assert system_user == getpass.getuser()

    # While on it, check the status of huge pages, that can be either on
    # or off, but never unknown.
    huge_pages_status = node.safe_sql("SHOW huge_pages_status;")
    assert huge_pages_status != "unknown", "check huge_pages_status"

    # system_user is embedded into regex patterns below; escape it so any
    # metacharacters in the OS user name are treated literally (the
    # username is trusted to be regex-safe on the pg_ident.conf side, but
    # escaped on the pattern side).
    su_re = re.escape(system_user)

    # Tests without the user name map.
    # Failure as connection is attempted with a database role not mapping
    # to an authorized system user.
    _test_role(
        node,
        "testmapuser",
        "peer",
        2,
        "without user name map",
        log_like=[r'Peer authentication failed for user "testmapuser"'],
    )

    # Tests with a user name map.
    reset_pg_ident(node, "mypeermap", system_user, "testmapuser")
    reset_pg_hba(node, "peer map=mypeermap")

    # Success as the database role matches with the system user in the map.
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        "with user name map",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Tests with the "all" keyword.
    reset_pg_ident(node, "mypeermap", system_user, "all")
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        'with keyword "all" as database user in user name map',
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Tests with the "all" keyword, but quoted (no effect here).
    reset_pg_ident(node, "mypeermap", system_user, '"all"')
    _test_role(
        node,
        "testmapuser",
        "peer",
        2,
        'with quoted keyword "all" as database user in user name map',
        log_like=[r'no match in usermap "mypeermap" for user "testmapuser"'],
    )

    # Success as the regexp of the database user matches
    reset_pg_ident(node, "mypeermap", system_user, r"/^testm.*$")
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        "with regexp of database user in user name map",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Failure as the regexp of the database user does not match.
    reset_pg_ident(node, "mypeermap", system_user, r"/^doesnotmatch.*$")
    _test_role(
        node,
        "testmapuser",
        "peer",
        2,
        "with bad regexp of database user in user name map",
        log_like=[r'no match in usermap "mypeermap" for user "testmapuser"'],
    )

    # Test with regular expression in user name map.
    # Extract the last 3 characters from the system_user
    # or the entire system_user name (if its length is <= 3).
    # We trust this will not include any regex metacharacters.
    regex_test_string = system_user[-3:]

    # Success as the system user regular expression matches.
    reset_pg_ident(node, "mypeermap", rf"/^.*{regex_test_string}$", "testmapuser")
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        "with regexp of system user in user name map",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Success as both regular expressions match.
    reset_pg_ident(node, "mypeermap", rf"/^.*{regex_test_string}$", r"/^testm.*$")
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        "with regexps for both system and database user in user name map",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Success as the regular expression matches and database role is the "all"
    # keyword.
    reset_pg_ident(node, "mypeermap", rf"/^.*{regex_test_string}$", "all")
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        'with regexp of system user and keyword "all" in user name map',
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Create target role for \1 tests.
    mapped_name = f"test{regex_test_string}map{regex_test_string}user"
    node.safe_sql(f"CREATE ROLE {mapped_name} LOGIN")

    # Success as the regular expression matches and \1 is replaced in the given
    # subexpression.
    reset_pg_ident(
        node, "mypeermap", rf"/^.*({regex_test_string})$", r"test\1map\1user"
    )
    _test_role(
        node,
        mapped_name,
        "peer",
        0,
        r"with regular expression in user name map with \1 replaced",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Success as the regular expression matches and \1 is replaced in the given
    # subexpression, even if quoted.
    reset_pg_ident(
        node, "mypeermap", rf"/^.*({regex_test_string})$", r'"test\1map\1user"'
    )
    _test_role(
        node,
        mapped_name,
        "peer",
        0,
        r"with regular expression in user name map with quoted \1 replaced",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Failure as the regular expression does not include a subexpression, but
    # the database user contains \1, requesting a replacement.
    reset_pg_ident(node, "mypeermap", rf"/^{system_user}$", r"\1testmapuser")
    _test_role(
        node,
        "testmapuser",
        "peer",
        2,
        r"with regular expression in user name map with \1 not replaced",
        log_like=[
            rf'regular expression "\^{su_re}\$" has no subexpressions as '
            r'requested by backreference in "\\1testmapuser"'
        ],
    )

    # Concatenate system_user to system_user.
    bad_regex_test_string = system_user + system_user

    # Failure as the regexp of system user does not match.
    reset_pg_ident(node, "mypeermap", rf"/^.*{bad_regex_test_string}$", "testmapuser")
    _test_role(
        node,
        "testmapuser",
        "peer",
        2,
        "with regexp of system user in user name map",
        log_like=[r'no match in usermap "mypeermap" for user "testmapuser"'],
    )

    # Test using a group role match for the database user.
    reset_pg_ident(node, "mypeermap", system_user, "+testmapgroup")
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        "plain user with group",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )
    _test_role(
        node,
        "testmapgroup",
        "peer",
        2,
        "group user with group",
        log_like=[r'role "testmapgroup" is not permitted to log in'],
    )

    # Now apply quotes to the group match, nullifying its effect.
    reset_pg_ident(node, "mypeermap", system_user, '"+testmapgroup"')
    _test_role(
        node,
        "testmapuser",
        "peer",
        2,
        "plain user with quoted group name",
        log_like=[r'no match in usermap "mypeermap" for user "testmapuser"'],
    )

    # Test using a regexp for the system user, with a group membership
    # check for the database user.
    reset_pg_ident(node, "mypeermap", rf"/^.*{regex_test_string}$", "+testmapgroup")
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        "regexp of system user as group member",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )
    _test_role(
        node,
        "testmapgroup",
        "peer",
        2,
        "regexp of system user as non-member of group",
        log_like=[r'role "testmapgroup" is not permitted to log in'],
    )

    # Test that membership checks and regexes will use literal \1 instead of
    # replacing it, as subexpression replacement is not allowed in this case.
    reset_pg_ident(
        node,
        "mypeermap",
        rf"/^.*{regex_test_string}(.*)$",
        r"+testmapgroupliteral\1",
    )
    _test_role(
        node,
        "testmapuser",
        "peer",
        0,
        r"membership check with literal \1",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )

    # Do the same with a quoted regular expression for the database user this
    # time.  No replacement of \1 is done.
    reset_pg_ident(
        node,
        "mypeermap",
        rf"/^.*{regex_test_string}(.*)$",
        r'"/^testmapgroupliteral\\1$"',
    )
    _test_role(
        node,
        r"testmapgroupliteral\\1",
        "peer",
        0,
        r"regexp of database user with literal \1",
        log_like=[rf'connection authenticated: identity="{su_re}" method=peer'],
    )
