# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests pg_dump / pg_restore in parallel directory format.

Creates a source database with a mix of objects (a table with a unique index,
hash-partitioned tables both "troublesome" and not), then exercises pg_dump in
parallel directory format (-Fd -j N) and pg_restore in parallel (-j N),
verifying the round-trip into two separate destination databases (one plain,
one via --inserts).

pg_dump / pg_restore are the binaries under test and are run as subprocesses
through the node's pg_bin (PGHOST/PGPORT point at the server).  The seed SQL
the test itself runs is executed in-process via safe_sql; the destination
databases are created with CREATE DATABASE (its own statement, not in a txn
block).
"""

DBNAME1 = "regression_src"
DBNAME2 = "regression_dest1"
DBNAME3 = "regression_dest2"

SETUP_SQL = """
create type digit as enum ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9');

-- plain table with index
create table tplain (en digit, data int unique);
insert into tplain select (x%10)::text::digit, x from generate_series(1,1000) x;

-- non-troublesome hashed partitioning
create table ths (mod int, data int, unique(mod, data)) partition by hash(mod);
create table ths_p1 partition of ths for values with (modulus 3, remainder 0);
create table ths_p2 partition of ths for values with (modulus 3, remainder 1);
create table ths_p3 partition of ths for values with (modulus 3, remainder 2);
insert into ths select (x%10), x from generate_series(1,1000) x;

-- dangerous hashed partitioning
create table tht (en digit, data int, unique(en, data)) partition by hash(en);
create table tht_p1 partition of tht for values with (modulus 3, remainder 0);
create table tht_p2 partition of tht for values with (modulus 3, remainder 1);
create table tht_p3 partition of tht for values with (modulus 3, remainder 2);
insert into tht select (x%10)::text::digit, x from generate_series(1,1000) x;
"""


def test_pg_dump_parallel(pg, tmp_path):
    node = pg

    # Create the source and the two destination databases.  CREATE DATABASE
    # cannot run inside a transaction block, so issue each as its own
    # statement.
    node.safe_sql(f"CREATE DATABASE {DBNAME1}")
    node.safe_sql(f"CREATE DATABASE {DBNAME2}")
    node.safe_sql(f"CREATE DATABASE {DBNAME3}")

    node.safe_sql(SETUP_SQL, dbname=DBNAME1)

    dump1 = str(tmp_path / "dump1")
    dump2 = str(tmp_path / "dump2")

    node.command_ok(
        [
            "pg_dump",
            "--format", "directory",
            "--no-sync",
            "--jobs", "2",
            "--file", dump1,
            node.connstr(DBNAME1),
        ],
        "parallel dump",
    )

    node.command_ok(
        [
            "pg_restore", "--verbose",
            "--dbname", node.connstr(DBNAME2),
            "--jobs", "3",
            dump1,
        ],
        "parallel restore",
    )

    node.command_ok(
        [
            "pg_dump",
            "--format", "directory",
            "--no-sync",
            "--jobs", "2",
            "--file", dump2,
            "--inserts",
            node.connstr(DBNAME1),
        ],
        "parallel dump as inserts",
    )

    node.command_ok(
        [
            "pg_restore", "--verbose",
            "--dbname", node.connstr(DBNAME3),
            "--jobs", "3",
            dump2,
        ],
        "parallel restore as inserts",
    )
