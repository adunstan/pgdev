# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests pg_dump of foreign data against a running server.

Starts a server and runs pg_dump against it, checking that dumping foreign
data includes only foreign tables of matching servers.

pg_dump is the binary under test and is run as a subprocess through the
node's pg_bin (PGHOST/PGPORT point at the server).  The seed SQL the test
itself runs is executed in-process via safe_sql.
"""


def test_pg_dump_with_server(pg):
    node = pg
    port = node.port

    #########################################
    # Verify that dumping foreign data includes only foreign tables of
    # matching servers

    node.safe_sql("CREATE FOREIGN DATA WRAPPER dummy")
    node.safe_sql("CREATE SERVER s0 FOREIGN DATA WRAPPER dummy")
    node.safe_sql("CREATE SERVER s1 FOREIGN DATA WRAPPER dummy")
    node.safe_sql("CREATE SERVER s2 FOREIGN DATA WRAPPER dummy")
    node.safe_sql("CREATE FOREIGN TABLE t0 (a int) SERVER s0")
    node.safe_sql("CREATE FOREIGN TABLE t1 (a int) SERVER s1")

    node.command_fails_like(
        [
            "pg_dump",
            "--port", str(port),
            "--include-foreign-data", "s0",
            "postgres",
        ],
        r'foreign-data wrapper "dummy" has no handler\r?\n'
        r"pg_dump: detail: Query was: .*t0",
        "correctly fails to dump a foreign table from a dummy FDW",
    )

    node.command_ok(
        [
            "pg_dump",
            "--port", str(port),
            "--data-only",
            "--include-foreign-data", "s2",
            "postgres",
        ],
        "dump foreign server with no tables",
    )
