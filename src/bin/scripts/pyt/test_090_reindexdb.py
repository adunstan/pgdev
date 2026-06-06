# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for reindexdb option handling, the SQL it issues, and error cases."""

import os
import re


def test_reindexdb(pg_bin, create_pg, monkeypatch):
    pg_bin.program_help_ok("reindexdb")
    pg_bin.program_version_ok("reindexdb")
    pg_bin.program_options_handling_ok("reindexdb")

    node = create_pg("main", start=False)
    # issues_sql_like needs the statements logged.
    node.append_conf("log_statement = 'all'\nlog_min_duration_statement = -1")
    node.start()

    monkeypatch.setenv("PGOPTIONS", "--client-min-messages=WARNING")

    # Create a tablespace for testing.
    tbspace_path = os.path.join(node.basedir, "regress_reindex_tbspace")
    os.mkdir(tbspace_path)
    tbspace_name = "reindex_tbspace"
    node.safe_sql(f"CREATE TABLESPACE {tbspace_name} LOCATION '{tbspace_path}';")

    # Use text as data type to get a toast table.
    node.safe_sql(
        "CREATE TABLE test1 (a text); CREATE INDEX test1x ON test1 (a);"
    )
    # Collect toast table and index names of this relation, for later use.
    toast_table = node.safe_sql(
        "SELECT reltoastrelid::regclass FROM pg_class "
        "WHERE oid = 'test1'::regclass;"
    )
    toast_index = node.safe_sql(
        "SELECT indexrelid::regclass FROM pg_index "
        f"WHERE indrelid = '{toast_table}'::regclass;"
    )

    # Set of SQL queries to cross-check the state of relfilenodes across
    # REINDEX operations.  A set of relfilenodes is saved from the catalogs
    # and then compared with pg_class.
    node.safe_sql(
        "CREATE TABLE index_relfilenodes "
        "(parent regclass, indname text, indoid oid, relfilenode oid);"
    )
    # Save the relfilenode of a set of toast indexes, one from the catalog
    # pg_constraint and one from the test table.
    fetch_toast_relfilenodes = """SELECT b.oid::regclass, c.oid::regclass::text, c.oid, c.relfilenode
  FROM pg_class a
    JOIN pg_class b ON (a.oid = b.reltoastrelid)
    JOIN pg_index i on (a.oid = i.indrelid)
    JOIN pg_class c on (i.indexrelid = c.oid)
  WHERE b.oid IN ('pg_constraint'::regclass, 'test1'::regclass)"""
    # Same for relfilenodes of normal indexes.  This saves the relfilenode
    # from an index of pg_constraint, and from the index of the test table.
    fetch_index_relfilenodes = """SELECT i.indrelid, a.oid::regclass::text, a.oid, a.relfilenode
  FROM pg_class a
    JOIN pg_index i ON (i.indexrelid = a.oid)
  WHERE a.relname IN ('pg_constraint_oid_index', 'test1x')"""
    save_relfilenodes = (
        f"INSERT INTO index_relfilenodes {fetch_toast_relfilenodes};"
        + f"INSERT INTO index_relfilenodes {fetch_index_relfilenodes};"
    )

    # Query to compare a set of relfilenodes saved with the contents of
    # pg_class.  Note that this does not join using OIDs, as CONCURRENTLY
    # would change them when reindexing.  A filter is applied on the toast
    # index names, even if this does not make a difference between the catalog
    # and normal ones.  The ordering based on the name is enough to ensure a
    # fixed output, where the name of the parent table is included to provide
    # more context.
    compare_relfilenodes = r"""SELECT b.parent::regclass,
  regexp_replace(b.indname::text, '(pg_toast.pg_toast_)\d+(_index)', '\1<oid>\2'),
  CASE WHEN a.oid = b.indoid THEN 'OID is unchanged'
    ELSE 'OID has changed' END,
  CASE WHEN a.relfilenode = b.relfilenode THEN 'relfilenode is unchanged'
    ELSE 'relfilenode has changed' END
  FROM index_relfilenodes b
    JOIN pg_class a ON b.indname::text = a.oid::regclass::text
  ORDER BY b.parent::text, b.indname::text"""

    # Save the set of relfilenodes and compare them.
    node.safe_sql(save_relfilenodes)
    node.issues_sql_like(
        ["reindexdb", "postgres"],
        re.compile(r"statement: REINDEX DATABASE postgres;"),
        "SQL REINDEX run",
    )
    relnode_info = node.safe_sql(compare_relfilenodes)
    assert relnode_info == (
        "pg_constraint|pg_constraint_oid_index|OID is unchanged|relfilenode is unchanged\n"
        "pg_constraint|pg_toast.pg_toast_<oid>_index|OID is unchanged|relfilenode is unchanged\n"
        "test1|pg_toast.pg_toast_<oid>_index|OID is unchanged|relfilenode has changed\n"
        "test1|test1x|OID is unchanged|relfilenode has changed"
    ), "relfilenode change after REINDEX DATABASE"

    # Re-save and run the second one.
    node.safe_sql(f"TRUNCATE index_relfilenodes; {save_relfilenodes}")
    node.issues_sql_like(
        ["reindexdb", "--system", "postgres"],
        re.compile(r"statement: REINDEX SYSTEM postgres;"),
        "reindex system tables",
    )
    relnode_info = node.safe_sql(compare_relfilenodes)
    assert relnode_info == (
        "pg_constraint|pg_constraint_oid_index|OID is unchanged|relfilenode has changed\n"
        "pg_constraint|pg_toast.pg_toast_<oid>_index|OID is unchanged|relfilenode has changed\n"
        "test1|pg_toast.pg_toast_<oid>_index|OID is unchanged|relfilenode is unchanged\n"
        "test1|test1x|OID is unchanged|relfilenode is unchanged"
    ), "relfilenode change after REINDEX SYSTEM"

    node.issues_sql_like(
        ["reindexdb", "--table", "test1", "postgres"],
        re.compile(r"statement: REINDEX TABLE public\.test1;"),
        "reindex specific table",
    )
    node.issues_sql_like(
        ["reindexdb", "--table", "test1", "--tablespace", tbspace_name, "postgres"],
        re.compile(
            rf"statement: REINDEX \(TABLESPACE {tbspace_name}\) TABLE public\.test1;"
        ),
        "reindex specific table on tablespace",
    )
    node.issues_sql_like(
        ["reindexdb", "--index", "test1x", "postgres"],
        re.compile(r"statement: REINDEX INDEX public\.test1x;"),
        "reindex specific index",
    )
    node.issues_sql_like(
        ["reindexdb", "--schema", "pg_catalog", "postgres"],
        re.compile(r"statement: REINDEX SCHEMA pg_catalog;"),
        "reindex specific schema",
    )
    node.issues_sql_like(
        ["reindexdb", "--verbose", "--table", "test1", "postgres"],
        re.compile(r"statement: REINDEX \(VERBOSE\) TABLE public\.test1;"),
        "reindex with verbose output",
    )
    node.issues_sql_like(
        [
            "reindexdb",
            "--verbose",
            "--table",
            "test1",
            "--tablespace",
            tbspace_name,
            "postgres",
        ],
        re.compile(
            rf"statement: REINDEX \(VERBOSE, TABLESPACE {tbspace_name}\) TABLE public\.test1;"
        ),
        "reindex with verbose output and tablespace",
    )

    # Same with --concurrently.
    # Save the state of the relations and compare them after the DATABASE
    # rebuild.
    node.safe_sql(f"TRUNCATE index_relfilenodes; {save_relfilenodes}")
    node.issues_sql_like(
        ["reindexdb", "--concurrently", "postgres"],
        re.compile(r"statement: REINDEX DATABASE CONCURRENTLY postgres;"),
        "SQL REINDEX CONCURRENTLY run",
    )
    relnode_info = node.safe_sql(compare_relfilenodes)
    assert relnode_info == (
        "pg_constraint|pg_constraint_oid_index|OID is unchanged|relfilenode is unchanged\n"
        "pg_constraint|pg_toast.pg_toast_<oid>_index|OID is unchanged|relfilenode is unchanged\n"
        "test1|pg_toast.pg_toast_<oid>_index|OID has changed|relfilenode has changed\n"
        "test1|test1x|OID has changed|relfilenode has changed"
    ), "OID change after REINDEX DATABASE CONCURRENTLY"

    node.issues_sql_like(
        ["reindexdb", "--concurrently", "--table", "test1", "postgres"],
        re.compile(r"statement: REINDEX TABLE CONCURRENTLY public\.test1;"),
        "reindex specific table concurrently",
    )
    node.issues_sql_like(
        ["reindexdb", "--concurrently", "--index", "test1x", "postgres"],
        re.compile(r"statement: REINDEX INDEX CONCURRENTLY public\.test1x;"),
        "reindex specific index concurrently",
    )
    node.issues_sql_like(
        ["reindexdb", "--concurrently", "--schema", "public", "postgres"],
        re.compile(r"statement: REINDEX SCHEMA CONCURRENTLY public;"),
        "reindex specific schema concurrently",
    )
    node.command_fails(
        ["reindexdb", "--concurrently", "--system", "postgres"],
        "reindex system tables concurrently",
    )
    node.issues_sql_like(
        ["reindexdb", "--concurrently", "--verbose", "--table", "test1", "postgres"],
        re.compile(
            r"statement: REINDEX \(VERBOSE\) TABLE CONCURRENTLY public\.test1;"
        ),
        "reindex with verbose output concurrently",
    )
    node.issues_sql_like(
        [
            "reindexdb",
            "--concurrently",
            "--verbose",
            "--table",
            "test1",
            "--tablespace",
            tbspace_name,
            "postgres",
        ],
        re.compile(
            rf"statement: REINDEX \(VERBOSE, TABLESPACE {tbspace_name}\) TABLE CONCURRENTLY public\.test1;"
        ),
        "reindex concurrently with verbose output and tablespace",
    )

    # REINDEX TABLESPACE on toast indexes and tables fails.  This is not
    # part of the main regression test suite as these have unpredictable
    # names, and CONCURRENTLY cannot be used in transaction blocks, preventing
    # the use of TRY/CATCH blocks in a custom function to filter error
    # messages.
    node.pg_bin.command_checks_all(
        ["reindexdb", "--table", toast_table, "--tablespace", tbspace_name, "postgres"],
        1,
        [],
        [re.compile(r"cannot move system relation")],
        "reindex toast table with tablespace",
    )
    node.pg_bin.command_checks_all(
        [
            "reindexdb",
            "--concurrently",
            "--table",
            toast_table,
            "--tablespace",
            tbspace_name,
            "postgres",
        ],
        1,
        [],
        [re.compile(r"cannot move system relation")],
        "reindex toast table concurrently with tablespace",
    )
    node.pg_bin.command_checks_all(
        ["reindexdb", "--index", toast_index, "--tablespace", tbspace_name, "postgres"],
        1,
        [],
        [re.compile(r"cannot move system relation")],
        "reindex toast index with tablespace",
    )
    node.pg_bin.command_checks_all(
        [
            "reindexdb",
            "--concurrently",
            "--index",
            toast_index,
            "--tablespace",
            tbspace_name,
            "postgres",
        ],
        1,
        [],
        [re.compile(r"cannot move system relation")],
        "reindex toast index concurrently with tablespace",
    )

    # connection strings
    node.command_ok(
        ["reindexdb", "--echo", "--table=pg_am", "dbname=template1"],
        "reindexdb table with connection string",
    )
    node.command_ok(
        ["reindexdb", "--echo", "dbname=template1"],
        "reindexdb database with connection string",
    )
    node.command_ok(
        ["reindexdb", "--echo", "--system", "dbname=template1"],
        "reindexdb system with connection string",
    )

    # parallel processing
    node.safe_sql(
        """
        CREATE SCHEMA s1;
        CREATE TABLE s1.t1(id integer);
        CREATE INDEX ON s1.t1(id);
        CREATE INDEX i1 ON s1.t1(id);
        CREATE SCHEMA s2;
        CREATE TABLE s2.t2(id integer);
        CREATE INDEX ON s2.t2(id);
        CREATE INDEX i2 ON s2.t2(id);
        -- empty schema
        CREATE SCHEMA s3;
    """
    )

    node.command_fails(
        ["reindexdb", "--jobs", "2", "--system", "postgres"],
        "parallel reindexdb cannot process system catalogs",
    )
    node.command_ok(
        [
            "reindexdb",
            "--jobs",
            "2",
            "--index",
            "s1.i1",
            "--index",
            "s2.i2",
            "--index",
            "s1.t1_id_idx",
            "--index",
            "s2.t2_id_idx",
            "postgres",
        ],
        "parallel reindexdb for indices",
    )
    # Note that the ordering of the commands is not stable, so the second
    # command for s2.t2 is not checked after.
    node.issues_sql_like(
        ["reindexdb", "--jobs", "2", "--schema", "s1", "--schema", "s2", "postgres"],
        re.compile(r"statement: REINDEX TABLE s1.t1;"),
        "parallel reindexdb for schemas does a per-table REINDEX",
    )
    node.command_ok(
        ["reindexdb", "--jobs", "2", "--schema", "s3"],
        "parallel reindexdb with empty schema",
    )
    node.command_ok(
        ["reindexdb", "--jobs", "2", "--concurrently", "--dbname", "postgres"],
        "parallel reindexdb on database, concurrently",
    )

    # combinations of objects
    node.issues_sql_like(
        ["reindexdb", "--system", "--table", "test1", "postgres"],
        re.compile(r"statement: REINDEX SYSTEM postgres;"),
        "specify both --system and --table",
    )
    node.issues_sql_like(
        ["reindexdb", "--system", "--index", "test1x", "postgres"],
        re.compile(r"statement: REINDEX INDEX public.test1x;"),
        "specify both --system and --index",
    )
    node.issues_sql_like(
        ["reindexdb", "--system", "--schema", "pg_catalog", "postgres"],
        re.compile(r"statement: REINDEX SCHEMA pg_catalog;"),
        "specify both --system and --schema",
    )
