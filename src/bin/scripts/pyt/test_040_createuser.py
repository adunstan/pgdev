# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for createuser option handling, the SQL it issues, and error cases."""

import re


def test_createuser(pg_bin, create_pg):
    pg_bin.program_help_ok("createuser")
    pg_bin.program_version_ok("createuser")
    pg_bin.program_options_handling_ok("createuser")

    node = create_pg("main", start=False)
    node.append_conf(
        "log_statement = 'all'\n"
        "log_min_messages = 'debug1'\n"
        "log_min_duration_statement = -1\n"
    )
    node.start()

    node.issues_sql_like(
        ["createuser", "regress_user1"],
        re.compile(
            r"statement: CREATE ROLE regress_user1 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS;"
        ),
        "SQL CREATE USER run",
    )
    node.issues_sql_like(
        ["createuser", "--no-login", "regress_role1"],
        re.compile(
            r"statement: CREATE ROLE regress_role1 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT NOLOGIN NOREPLICATION NOBYPASSRLS;"
        ),
        "create a non-login role",
    )
    node.issues_sql_like(
        ["createuser", "--createrole", "regress user2"],
        re.compile(
            r'statement: CREATE ROLE "regress user2" NOSUPERUSER NOCREATEDB '
            r"CREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS;"
        ),
        "create a CREATEROLE user",
    )
    node.issues_sql_like(
        ["createuser", "--superuser", "regress_user3"],
        re.compile(
            r"statement: CREATE ROLE regress_user3 SUPERUSER CREATEDB "
            r"CREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS;"
        ),
        "create a superuser",
    )
    node.issues_sql_like(
        [
            "createuser",
            "--with-admin", "regress_user1",
            "--with-admin", "regress user2",
            "regress user #4",
        ],
        re.compile(
            r'statement: CREATE ROLE "regress user #4" NOSUPERUSER NOCREATEDB '
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS ADMIN "
            r'regress_user1,"regress user2";'
        ),
        "add a role as a member with admin option of the newly created role",
    )
    node.issues_sql_like(
        [
            "createuser",
            "REGRESS_USER5",
            "--with-member", "regress_user3",
            "--with-member", "regress user #4",
        ],
        re.compile(
            r'statement: CREATE ROLE "REGRESS_USER5" NOSUPERUSER NOCREATEDB '
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS ROLE "
            r'regress_user3,"regress user #4";'
        ),
        "add a role as a member of the newly created role",
    )
    node.issues_sql_like(
        ["createuser", "--valid-until", "2029 12 31", "regress_user6"],
        re.compile(
            r"statement: CREATE ROLE regress_user6 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS "
            r"VALID UNTIL '2029 12 31';"
        ),
        "create a role with a password expiration date",
    )
    node.issues_sql_like(
        ["createuser", "--bypassrls", "regress_user7"],
        re.compile(
            r"statement: CREATE ROLE regress_user7 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION BYPASSRLS;"
        ),
        "create a BYPASSRLS role",
    )
    node.issues_sql_like(
        ["createuser", "--no-bypassrls", "regress_user8"],
        re.compile(
            r"statement: CREATE ROLE regress_user8 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS;"
        ),
        "create a role without BYPASSRLS",
    )
    node.issues_sql_like(
        ["createuser", "--with-admin", "regress_user1", "regress_user9"],
        re.compile(
            r"statement: CREATE ROLE regress_user9 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS "
            r"ADMIN regress_user1;"
        ),
        "--with-admin",
    )
    node.issues_sql_like(
        ["createuser", "--with-member", "regress_user1", "regress_user10"],
        re.compile(
            r"statement: CREATE ROLE regress_user10 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS "
            r"ROLE regress_user1;"
        ),
        "--with-member",
    )
    node.issues_sql_like(
        ["createuser", "--role", "regress_user1", "regress_user11"],
        re.compile(
            r"statement: CREATE ROLE regress_user11 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS "
            r"IN ROLE regress_user1;"
        ),
        "--role",
    )
    node.issues_sql_like(
        ["createuser", "regress_user12", "--member-of", "regress_user1"],
        re.compile(
            r"statement: CREATE ROLE regress_user12 NOSUPERUSER NOCREATEDB "
            r"NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS "
            r"IN ROLE regress_user1;"
        ),
        "--member-of",
    )

    node.command_fails(
        ["createuser", "regress_user1"],
        "fails if role already exists",
    )
    node.command_fails(
        [
            "createuser",
            "regress_user1",
            "--with-member", "regress_user2",
            "regress_user3",
        ],
        "fails for too many non-options",
    )
