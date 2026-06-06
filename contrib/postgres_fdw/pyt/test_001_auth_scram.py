# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test SCRAM authentication when opening a new connection with a foreign server.

The test is executed by testing the SCRAM authentication on a loopback
connection on the same server and with different servers.

All queries run in-process via the libpq Session.  Queries that connect as
the password role ``user01`` build a dedicated Session with the role and
password embedded in the connection string.

The test exercises ``local`` pg_hba.conf entries with scram-sha-256 over
unix sockets, exercising the SCRAM passthrough code paths.
"""

import os
import re

import libpq

USER = "user01"

DB0 = "db0"  # For node1
DB1 = "db1"  # For node1
DB2 = "db2"  # For node2
FDW_SERVER = "db1_fdw"
FDW_SERVER2 = "db2_fdw"
FDW_SERVER3 = "db1_fdw_override"


# -- helper functions --------------------------------------------------------


def user_session(node, db):
    """A libpq Session connected to *db* as the password role USER."""
    connstr = node.connstr(db) + f" user={USER} password=pass"
    return libpq.Session(connstr=connstr, libdir=node.libdir)


def setup_table(node, db, tbl):
    node.safe_sql(
        f"CREATE TABLE {tbl} AS SELECT g, g + 1 FROM generate_series(1,10) g(g)",
        dbname=db,
    )
    node.safe_sql(f"GRANT USAGE ON SCHEMA public TO {USER}", dbname=db)
    node.safe_sql(f"GRANT SELECT ON {tbl} TO {USER}", dbname=db)


def setup_fdw_server(node, db, fdw, fdw_node, dbname):
    host = fdw_node.host
    port = fdw_node.port
    node.safe_sql(
        f"CREATE SERVER {fdw} FOREIGN DATA WRAPPER postgres_fdw options ("
        f"host '{host}', port '{port}', dbname '{dbname}', "
        "use_scram_passthrough 'true') ",
        dbname=db,
    )


def setup_user_mapping(node, db, fdw):
    node.safe_sql(
        f"CREATE USER MAPPING FOR {USER} SERVER {fdw} OPTIONS (user '{USER}');",
        dbname=db,
    )
    node.safe_sql(f"GRANT USAGE ON FOREIGN SERVER {fdw} TO {USER};", dbname=db)
    node.safe_sql(f"GRANT ALL ON SCHEMA public TO {USER}", dbname=db)


def setup_pghba(node, body):
    os.unlink(os.path.join(node.data_dir, "pg_hba.conf"))
    node.append_conf(body, filename="pg_hba.conf")
    node.restart()


def check_auth(node, db, tbl, testname):
    """Connect as USER and assert the foreign table returns its 10 rows."""
    sess = user_session(node, db)
    ret = sess.query_safe(f"SELECT count(1) FROM {tbl}")
    assert ret == "10", testname
    sess.close()


def check_fdw_auth(node, db, tbl, fdw, testname):
    """Import the remote table over the foreign server, then read it."""
    sess = user_session(node, db)
    sess.query_safe(
        f"IMPORT FOREIGN SCHEMA public LIMIT TO ({tbl}) "
        f"FROM SERVER {fdw} INTO public;"
    )
    sess.close()
    check_auth(node, db, tbl, testname)


# -- main test ---------------------------------------------------------------


def test_postgres_fdw_auth_scram(create_pg):
    node1 = create_pg("node1")
    node2 = create_pg("node2")

    # Test setup

    node1.safe_sql(f"CREATE USER {USER} WITH password 'pass'")
    node2.safe_sql(f"CREATE USER {USER} WITH password 'pass'")

    node1.safe_sql(f"CREATE DATABASE {DB0}")
    node1.safe_sql(f"CREATE DATABASE {DB1}")
    node2.safe_sql(f"CREATE DATABASE {DB2}")

    setup_table(node1, DB1, "t")
    setup_table(node2, DB2, "t2")

    node1.safe_sql("CREATE EXTENSION IF NOT EXISTS postgres_fdw", dbname=DB0)
    setup_fdw_server(node1, DB0, FDW_SERVER, node1, DB1)
    setup_fdw_server(node1, DB0, FDW_SERVER2, node2, DB2)
    setup_fdw_server(node1, DB0, FDW_SERVER3, node1, DB1)

    setup_user_mapping(node1, DB0, FDW_SERVER)
    setup_user_mapping(node1, DB0, FDW_SERVER2)
    setup_user_mapping(node1, DB0, FDW_SERVER3)

    # Make the user have the same SCRAM key on both servers.  Forcing to have
    # the same iteration and salt.
    rolpassword = node1.safe_sql(
        f"SELECT rolpassword FROM pg_authid WHERE rolname = '{USER}';"
    )
    node2.safe_sql(f"ALTER ROLE {USER} PASSWORD '{rolpassword}'")

    setup_pghba(
        node1,
        """
local   all             all                                     scram-sha-256
""",
    )
    setup_pghba(
        node2,
        """
local   all             all                                     scram-sha-256
""",
    )

    # End of test setup

    check_fdw_auth(
        node1,
        DB0,
        "t",
        FDW_SERVER,
        "SCRAM auth on the same database cluster must succeed",
    )
    check_fdw_auth(
        node1,
        DB0,
        "t2",
        FDW_SERVER2,
        "SCRAM auth on a different database cluster must succeed",
    )
    check_auth(
        node2,
        DB2,
        "t2",
        "SCRAM auth directly on foreign server should still succeed",
    )

    # Test that use_scram_passthrough=false on user mapping overrides server
    # setting.
    sess = user_session(node1, DB0)
    sess.query_safe(
        f"ALTER USER MAPPING FOR {USER} SERVER {FDW_SERVER3} "
        "OPTIONS(add use_scram_passthrough 'false')"
    )
    sess.query_safe(
        f"CREATE FOREIGN TABLE override_t (g int, col2 int) "
        f"SERVER {FDW_SERVER3} OPTIONS (table_name 't');"
    )
    sess.query_safe(f"GRANT SELECT ON override_t TO {USER};")

    res = sess.query("SELECT count(1) FROM override_t")
    assert (
        res.error_message is not None
    ), "SCRAM passthrough disabled on user mapping should fail"
    assert re.search(r"password", res.error_message, re.IGNORECASE), (
        "expected password-related error when scram passthrough disabled "
        "on user mapping"
    )
    sess.close()

    # Ensure that trust connections fail without superuser opt-in.
    setup_pghba(
        node1,
        """
local   db0             all                                     scram-sha-256
local   db1             all                                     trust
""",
    )
    setup_pghba(
        node2,
        """
local   all             all                                     password
""",
    )

    sess = user_session(node1, DB0)
    res = sess.query("select count(1) from t")
    assert res.error_message is not None, "loopback trust fails on the same cluster"
    assert re.search(
        r'failed: authentication method requirement "scram-sha-256"',
        res.error_message,
    ), "expected error from loopback trust (same cluster)"

    res = sess.query("select count(1) from t2")
    assert (
        res.error_message is not None
    ), "loopback password fails on a different cluster"
    assert re.search(
        r'failed: authentication method requirement "scram-sha-256"',
        res.error_message,
    ), "expected error from loopback password (different cluster)"
    sess.close()
