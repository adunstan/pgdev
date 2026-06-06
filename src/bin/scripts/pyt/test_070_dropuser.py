# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for dropuser option handling, the SQL it issues, and error cases."""

import re


def test_dropuser(pg_bin, create_pg):
    pg_bin.program_help_ok("dropuser")
    pg_bin.program_version_ok("dropuser")
    pg_bin.program_options_handling_ok("dropuser")

    node = create_pg("main")
    # issues_sql_like needs the statements logged.
    node.append_conf("log_statement = 'all'\nlog_min_duration_statement = -1")
    node.restart()

    node.safe_sql("CREATE ROLE regress_foobar1")
    node.issues_sql_like(
        ["dropuser", "regress_foobar1"],
        re.compile(r"statement: DROP ROLE regress_foobar1"),
        "SQL DROP ROLE run",
    )

    node.command_fails_like(
        ["dropuser", "regress_nonexistent"],
        re.compile(r'role "regress_nonexistent" does not exist'),
        "fails with nonexistent user",
    )
