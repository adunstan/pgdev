# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_amcheck error handling with nonexistent roles, databases, and objects."""

import re


def test_nonesuch(create_pg):
    # Test set-up.  With trust auth the role-does-not-exist error is raised
    # when connecting as no_such_user, so no extra initdb args are needed here.
    node = create_pg("test")

    # Load the amcheck extension, upon which pg_amcheck depends
    node.safe_sql("CREATE EXTENSION amcheck")

    #########################################
    # Test non-existent databases

    # Failing to connect to the initial database is an error.
    node.command_checks_all(
        ["pg_amcheck", "qqq"],
        1,
        [re.compile(r"^$")],
        [re.compile(r'FATAL:  database "qqq" does not exist')],
        "checking a non-existent database",
    )

    # Failing to resolve a database pattern is an error by default.
    node.command_checks_all(
        ["pg_amcheck", "--database", "qqq", "--database", "postgres"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: error: no connectable databases to check matching "qqq"'
            )
        ],
        "checking an unresolvable database pattern",
    )

    # But only a warning under --no-strict-names
    node.command_checks_all(
        [
            "pg_amcheck",
            "--no-strict-names",
            "--database",
            "qqq",
            "--database",
            "postgres",
        ],
        0,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "qqq"'
            )
        ],
        "checking an unresolvable database pattern under --no-strict-names",
    )

    # Check that a substring of an existent database name does not get interpreted
    # as a matching pattern.
    node.command_checks_all(
        ["pg_amcheck", "--database", "post", "--database", "postgres"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: error: no connectable databases to check matching "post"'
            )
        ],
        "checking an unresolvable database pattern (substring of existent database)",
    )

    # Check that a superstring of an existent database name does not get interpreted
    # as a matching pattern.
    node.command_checks_all(
        ["pg_amcheck", "--database", "postgresql", "--database", "postgres"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: error: no connectable databases to check matching "postgresql"'
            )
        ],
        "checking an unresolvable database pattern (superstring of existent database)",
    )

    #########################################
    # Test connecting with a non-existent user

    # Failing to connect to the initial database due to bad username is an error.
    node.command_checks_all(
        ["pg_amcheck", "--username", "no_such_user", "postgres"],
        1,
        [re.compile(r"^$")],
        [re.compile(r'role "no_such_user" does not exist')],
        "checking with a non-existent user",
    )

    #########################################
    # Test checking databases without amcheck installed

    # Attempting to check a database by name where amcheck is not installed should
    # raise a warning.  If all databases are skipped, having no relations to check
    # raises an error.
    node.command_checks_all(
        ["pg_amcheck", "template1"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: warning: skipping database "template1": amcheck is not installed'
            ),
            re.compile(r"pg_amcheck: error: no relations to check"),
        ],
        "checking a database by name without amcheck installed, no other databases",
    )

    # Again, but this time with another database to check, so no error is raised.
    node.command_checks_all(
        ["pg_amcheck", "--database", "template1", "--database", "postgres"],
        0,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: warning: skipping database "template1": amcheck is not installed'
            )
        ],
        "checking a database by name without amcheck installed, with other databases",
    )

    # Again, but by way of checking all databases
    node.command_checks_all(
        ["pg_amcheck", "--all"],
        0,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: warning: skipping database "template1": amcheck is not installed'
            )
        ],
        "checking a database by pattern without amcheck installed, with other databases",
    )

    #########################################
    # Test unreasonable patterns

    # Check three-part unreasonable pattern that has zero-length names
    node.command_checks_all(
        ["pg_amcheck", "--database", "postgres", "--table", ".."],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: error: no connectable databases to check matching "\.\."'
            )
        ],
        'checking table pattern ".."',
    )

    # Again, but with non-trivial schema and relation parts
    node.command_checks_all(
        ["pg_amcheck", "--database", "postgres", "--table", ".foo.bar"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: error: no connectable databases to check matching "\.foo\.bar"'
            )
        ],
        'checking table pattern ".foo.bar"',
    )

    # Check two-part unreasonable pattern that has zero-length names
    node.command_checks_all(
        ["pg_amcheck", "--database", "postgres", "--table", "."],
        1,
        [re.compile(r"^$")],
        [re.compile(r'pg_amcheck: error: no heap tables to check matching "\."')],
        'checking table pattern "."',
    )

    # Check that a multipart database name is rejected
    node.command_checks_all(
        ["pg_amcheck", "--database", "localhost.postgres"],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper qualified name \(too many dotted names\): localhost\.postgres"
            )
        ],
        "multipart database patterns are rejected",
    )

    # Check that a three-part schema name is rejected
    node.command_checks_all(
        ["pg_amcheck", "--schema", "localhost.postgres.pg_catalog"],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper qualified name \(too many dotted names\): localhost\.postgres\.pg_catalog"
            )
        ],
        "three part schema patterns are rejected",
    )

    # Check that a four-part table name is rejected
    node.command_checks_all(
        ["pg_amcheck", "--table", "localhost.postgres.pg_catalog.pg_class"],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper relation name \(too many dotted names\): localhost\.postgres\.pg_catalog\.pg_class"
            )
        ],
        "four part table patterns are rejected",
    )

    # Check that too many dotted names still draws an error under --no-strict-names
    # That flag means that it is ok for the object to be missing, not that it is ok
    # for the object name to be ungrammatical
    node.command_checks_all(
        [
            "pg_amcheck",
            "--no-strict-names",
            "--table",
            "this.is.a.really.long.dotted.string",
        ],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper relation name \(too many dotted names\): this\.is\.a\.really\.long\.dotted\.string"
            )
        ],
        "ungrammatical table names still draw errors under --no-strict-names",
    )
    node.command_checks_all(
        [
            "pg_amcheck",
            "--no-strict-names",
            "--schema",
            "postgres.long.dotted.string",
        ],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper qualified name \(too many dotted names\): postgres\.long\.dotted\.string"
            )
        ],
        "ungrammatical schema names still draw errors under --no-strict-names",
    )
    node.command_checks_all(
        [
            "pg_amcheck",
            "--no-strict-names",
            "--database",
            "postgres.long.dotted.string",
        ],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper qualified name \(too many dotted names\): postgres\.long\.dotted\.string"
            )
        ],
        "ungrammatical database names still draw errors under --no-strict-names",
    )

    # Likewise for exclusion patterns
    node.command_checks_all(
        ["pg_amcheck", "--no-strict-names", "--exclude-table", "a.b.c.d"],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper relation name \(too many dotted names\): a\.b\.c\.d"
            )
        ],
        "ungrammatical table exclusions still draw errors under --no-strict-names",
    )
    node.command_checks_all(
        ["pg_amcheck", "--no-strict-names", "--exclude-schema", "a.b.c"],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper qualified name \(too many dotted names\): a\.b\.c"
            )
        ],
        "ungrammatical schema exclusions still draw errors under --no-strict-names",
    )
    node.command_checks_all(
        ["pg_amcheck", "--no-strict-names", "--exclude-database", "a.b"],
        2,
        [re.compile(r"^$")],
        [
            re.compile(
                r"pg_amcheck: error: improper qualified name \(too many dotted names\): a\.b"
            )
        ],
        "ungrammatical database exclusions still draw errors under --no-strict-names",
    )

    #########################################
    # Test checking non-existent databases, schemas, tables, and indexes

    # Use --no-strict-names and a single existent table so we only get warnings
    # about the failed pattern matches
    node.command_checks_all(
        [
            "pg_amcheck",
            "--no-strict-names",
            "--table",
            "no_such_table",
            "--table",
            "no*such*table",
            "--index",
            "no_such_index",
            "--index",
            "no*such*index",
            "--relation",
            "no_such_relation",
            "--relation",
            "no*such*relation",
            "--database",
            "no_such_database",
            "--database",
            "no*such*database",
            "--relation",
            "none.none",
            "--relation",
            "none.none.none",
            "--relation",
            "postgres.none.none",
            "--relation",
            "postgres.pg_catalog.none",
            "--relation",
            "postgres.none.pg_class",
            "--table",
            "postgres.pg_catalog.pg_class",  # This exists
        ],
        0,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: warning: no heap tables to check matching "no_such_table"'
            ),
            re.compile(
                r'pg_amcheck: warning: no heap tables to check matching "no\*such\*table"'
            ),
            re.compile(
                r'pg_amcheck: warning: no btree indexes to check matching "no_such_index"'
            ),
            re.compile(
                r'pg_amcheck: warning: no btree indexes to check matching "no\*such\*index"'
            ),
            re.compile(
                r'pg_amcheck: warning: no relations to check matching "no_such_relation"'
            ),
            re.compile(
                r'pg_amcheck: warning: no relations to check matching "no\*such\*relation"'
            ),
            re.compile(
                r'pg_amcheck: warning: no heap tables to check matching "no\*such\*table"'
            ),
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "no_such_database"'
            ),
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "no\*such\*database"'
            ),
            re.compile(
                r'pg_amcheck: warning: no relations to check matching "none\.none"'
            ),
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "none\.none\.none"'
            ),
            re.compile(
                r'pg_amcheck: warning: no relations to check matching "postgres\.none\.none"'
            ),
            re.compile(
                r'pg_amcheck: warning: no relations to check matching "postgres\.pg_catalog\.none"'
            ),
            re.compile(
                r'pg_amcheck: warning: no relations to check matching "postgres\.none\.pg_class"'
            ),
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "no_such_database"'
            ),
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "no\*such\*database"'
            ),
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "none\.none\.none"'
            ),
        ],
        "many unmatched patterns and one matched pattern under --no-strict-names",
    )

    #########################################
    # Test that an invalid / partially dropped database won't be targeted

    # CREATE DATABASE cannot run inside a transaction block, so issue each
    # statement separately (the in-process Session wraps multi-statement input
    # in a transaction).
    node.safe_sql("CREATE DATABASE regression_invalid")
    node.safe_sql(
        "UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'regression_invalid'"
    )

    node.command_checks_all(
        ["pg_amcheck", "--database", "regression_invalid"],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: error: no connectable databases to check matching "regression_invalid"'
            ),
        ],
        "checking handling of invalid database",
    )

    node.command_checks_all(
        [
            "pg_amcheck",
            "--database",
            "postgres",
            "--table",
            "regression_invalid.public.foo",
        ],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: error: no connectable databases to check matching "regression_invalid.public.foo"'
            ),
        ],
        "checking handling of object in invalid database",
    )

    #########################################
    # Test checking otherwise existent objects but in databases where they do not exist

    node.safe_sql(
        """
        CREATE TABLE public.foo (f integer);
        CREATE INDEX foo_idx ON foo(f);
    """
    )
    node.safe_sql("CREATE DATABASE another_db")

    node.command_checks_all(
        [
            "pg_amcheck",
            "--database",
            "postgres",
            "--no-strict-names",
            "--table",
            "template1.public.foo",
            "--table",
            "another_db.public.foo",
            "--table",
            "no_such_database.public.foo",
            "--index",
            "template1.public.foo_idx",
            "--index",
            "another_db.public.foo_idx",
            "--index",
            "no_such_database.public.foo_idx",
        ],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: warning: skipping database "template1": amcheck is not installed'
            ),
            re.compile(
                r'pg_amcheck: warning: no heap tables to check matching "template1\.public\.foo"'
            ),
            re.compile(
                r'pg_amcheck: warning: no heap tables to check matching "another_db\.public\.foo"'
            ),
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "no_such_database\.public\.foo"'
            ),
            re.compile(
                r'pg_amcheck: warning: no btree indexes to check matching "template1\.public\.foo_idx"'
            ),
            re.compile(
                r'pg_amcheck: warning: no btree indexes to check matching "another_db\.public\.foo_idx"'
            ),
            re.compile(
                r'pg_amcheck: warning: no connectable databases to check matching "no_such_database\.public\.foo_idx"'
            ),
            re.compile(r"pg_amcheck: error: no relations to check"),
        ],
        "checking otherwise existent objects in the wrong databases",
    )

    #########################################
    # Test schema exclusion patterns

    # Check with only schema exclusion patterns
    node.command_checks_all(
        [
            "pg_amcheck",
            "--all",
            "--no-strict-names",
            "--exclude-schema",
            "public",
            "--exclude-schema",
            "pg_catalog",
            "--exclude-schema",
            "pg_toast",
            "--exclude-schema",
            "information_schema",
        ],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: warning: skipping database "template1": amcheck is not installed'
            ),
            re.compile(r"pg_amcheck: error: no relations to check"),
        ],
        "schema exclusion patterns exclude all relations",
    )

    # Check with schema exclusion patterns overriding relation and schema inclusion patterns
    node.command_checks_all(
        [
            "pg_amcheck",
            "--all",
            "--no-strict-names",
            "--schema",
            "public",
            "--schema",
            "pg_catalog",
            "--schema",
            "pg_toast",
            "--schema",
            "information_schema",
            "--table",
            "pg_catalog.pg_class",
            "--exclude-schema",
            "*",
        ],
        1,
        [re.compile(r"^$")],
        [
            re.compile(
                r'pg_amcheck: warning: skipping database "template1": amcheck is not installed'
            ),
            re.compile(r"pg_amcheck: error: no relations to check"),
        ],
        "schema exclusion pattern overrides all inclusion patterns",
    )
