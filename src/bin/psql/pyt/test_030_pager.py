# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Set up "wc -l" as the pager (via PSQL_PAGER) so we can tell whether psql used
the pager, then drive an interactive psql session through a pty whose window is
sized 24x80 and check, for several queries/commands, that the pager was invoked
by matching the line-count number that "wc -l" prints.
"""

import re
import subprocess

import pytest


def _do_command(psql, send, pattern, annotation):
    """Send *send*, wait for *pattern*, and assert it matched.

    *pattern* is a compiled regex.  The match must be found in the captured
    output and the query must not have timed out.
    """
    out = psql.query_until(pattern, send)
    assert pattern.search(out) and not psql.timed_out, annotation


def test_030_pager(pg, interactive_psql):
    node = pg

    # Check that "wc -l" does what we expect, else forget it.
    result = subprocess.run(
        ["wc", "-l"],
        input=b"foo bar\nbaz\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wcstdout = result.stdout.decode().strip()
    wcstderr = result.stderr.decode()
    if not re.match(r"^ *2$", wcstdout) or wcstderr != "":
        pytest.skip('"wc -l" is needed to run this test')

    # create a view we'll use below
    node.safe_sql(
        """create view public.view_030_pager as select
1 as a,
2 as b,
3 as c,
4 as d,
5 as e,
6 as f,
7 as g,
8 as h,
9 as i,
10 as j,
11 as k,
12 as l,
13 as m,
14 as n,
15 as o,
16 as p,
17 as q,
18 as r,
19 as s,
20 as t,
21 as u,
22 as v,
23 as w,
24 as x,
25 as y,
26 as z""",
    )

    # fire up an interactive psql session.  We set up "wc -l" as the pager so
    # we can tell whether psql used the pager, and size the pty's window to
    # known values (24x80).
    psql = interactive_psql(
        node,
        dbname="postgres",
        extra_env={"PSQL_PAGER": "wc -l"},
        dimensions=(24, 80),
    )

    # Test invocation of the pager
    #
    # Note that interactive_psql starts psql with --no-align --tuples-only,
    # and that the output string will include psql's prompts and command echo.
    # So we have to test for patterns that can't match the command itself,
    # and we can't assume the match will extend across a whole line (there
    # might be a prompt ahead of it in the output).

    _do_command(
        psql,
        "SELECT 'test' AS t FROM generate_series(1,23);\n",
        re.compile(r"test\r?$", re.MULTILINE),
        "execute SELECT query that needs no pagination",
    )

    _do_command(
        psql,
        "SELECT 'test' AS t FROM generate_series(1,24);\n",
        re.compile(r"24\r?$", re.MULTILINE),
        "execute SELECT query that needs pagination",
    )

    _do_command(
        psql,
        "\\pset expanded\nSELECT generate_series(1,20) as g;\n",
        re.compile(r"39\r?$", re.MULTILINE),
        "execute SELECT query that needs pagination in expanded mode",
    )

    _do_command(
        psql,
        "\\pset tuples_only off\n\\d+ public.view_030_pager\n",
        re.compile(r"55\r?$", re.MULTILINE),
        "execute command with footer that needs pagination",
    )

    # send psql an explicit \q to shut it down, else pty won't close properly
    psql.quit()
