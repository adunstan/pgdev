# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test SCRAM authentication when opening a new connection with a foreign server.

The test is executed by testing the SCRAM authentication on a loopback
connection on the same server and with different servers.

All queries run in-process via the libpq Session.  Queries that connect as
the password role ``user01`` build a dedicated Session with the role and
password embedded in the connection string.
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
FDW_INVALID_SERVER = "db2_fdw_invalid"  # For invalid fdw options
FDW_INVALID_SERVER2 = "db2_fdw_invalid2"  # For invalid scram keys fdw options


# -- helper functions --------------------------------------------------------


def user_session(node, db):
    """A libpq Session connected to *db* as the password role USER."""
    connstr = node.connstr(db) + f" user={USER} password=pass"
    return libpq.Session(connstr=connstr, libdir=node.libdir)


def setup_table(node, db, tbl):
    node.safe_sql(
        f"CREATE TABLE {tbl} AS SELECT g as a, g + 1 as b "
        "FROM generate_series(1,10) g(g)",
        dbname=db,
    )
    node.safe_sql(f"GRANT USAGE ON SCHEMA public TO {USER}", dbname=db)
    node.safe_sql(f"GRANT SELECT ON {tbl} TO {USER}", dbname=db)


def setup_fdw_server(node, db, fdw, fdw_node, dbname):
    host = fdw_node.host
    port = fdw_node.port
    node.safe_sql(
        f"CREATE SERVER {fdw} FOREIGN DATA WRAPPER dblink_fdw options ("
        f"host '{host}', port '{port}', dbname '{dbname}', "
        "use_scram_passthrough 'true') ",
        dbname=db,
    )
    node.safe_sql(f"GRANT USAGE ON FOREIGN SERVER {fdw} TO {USER};", dbname=db)
    node.safe_sql(f"GRANT ALL ON SCHEMA public TO {USER}", dbname=db)


def setup_invalid_fdw_server(node, db, fdw, fdw_node, dbname):
    host = fdw_node.host
    port = fdw_node.port
    node.safe_sql(
        f"CREATE SERVER {fdw} FOREIGN DATA WRAPPER dblink_fdw options ("
        f"host '{host}', port '{port}', dbname '{dbname}', "
        "use_scram_passthrough 'true', require_auth 'none') ",
        dbname=db,
    )
    node.safe_sql(f"GRANT USAGE ON FOREIGN SERVER {fdw} TO {USER};", dbname=db)
    node.safe_sql(f"GRANT ALL ON SCHEMA public TO {USER}", dbname=db)


def setup_user_mapping(node, db, fdw):
    node.safe_sql(
        f"CREATE USER MAPPING FOR {USER} SERVER {fdw} OPTIONS (user '{USER}');",
        dbname=db,
    )


def check_fdw_auth(node, db, tbl, fdw, testname):
    sess = user_session(node, db)
    ret = sess.query_safe(
        f"SELECT count(1) FROM dblink('{fdw}', 'SELECT * FROM {tbl}') "
        f"AS {tbl}(a int, b int)"
    )
    assert ret == "10", testname
    sess.close()


def check_fdw_auth_with_invalid_overwritten_require_auth(node, fdw):
    sess = user_session(node, DB0)
    res = sess.query(
        f"select * from dblink('{fdw}', 'select * from t') as t(a int, b int)"
    )
    assert (
        res.error_message is not None
    ), "loopback trust fails when overwriting require_auth"
    assert re.search(
        r"password or GSSAPI delegated credentials required",
        res.error_message,
    ), "expected error when connecting to a fdw overwriting the require_auth"
    sess.close()


def check_scram_keys_is_not_overwritten(node, db, fdw):
    sess = user_session(node, db)

    res = sess.query(
        f"CREATE USER MAPPING FOR {USER} SERVER {fdw} "
        f"OPTIONS (user '{USER}', scram_client_key 'key');"
    )
    assert (
        res.error_message is not None
    ), "user mapping creation fails when using scram_client_key"
    assert re.search(
        r'ERROR:  invalid option "scram_client_key"', res.error_message
    ), "user mapping creation fails when using scram_client_key"

    res = sess.query(
        f"CREATE USER MAPPING FOR {USER} SERVER {fdw} "
        f"OPTIONS (user '{USER}', scram_server_key 'key');"
    )
    assert (
        res.error_message is not None
    ), "user mapping creation fails when using scram_server_key"
    assert re.search(
        r'ERROR:  invalid option "scram_server_key"', res.error_message
    ), "user mapping creation fails when using scram_server_key"

    sess.close()


# -- main test ---------------------------------------------------------------


def test_dblink_auth_scram(create_pg):
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

    node1.safe_sql("CREATE EXTENSION IF NOT EXISTS dblink", dbname=DB0)
    setup_fdw_server(node1, DB0, FDW_SERVER, node1, DB1)
    setup_fdw_server(node1, DB0, FDW_SERVER2, node2, DB2)
    setup_invalid_fdw_server(node1, DB0, FDW_INVALID_SERVER, node2, DB2)
    setup_fdw_server(node1, DB0, FDW_INVALID_SERVER2, node2, DB2)
    setup_fdw_server(node1, DB0, FDW_SERVER3, node1, DB1)

    setup_user_mapping(node1, DB0, FDW_SERVER)
    setup_user_mapping(node1, DB0, FDW_SERVER2)
    setup_user_mapping(node1, DB0, FDW_INVALID_SERVER)
    setup_user_mapping(node1, DB0, FDW_SERVER3)

    # Make the user have the same SCRAM key on both servers.  Forcing to have
    # the same iteration and salt.
    rolpassword = node1.safe_sql(
        f"SELECT rolpassword FROM pg_authid WHERE rolname = '{USER}';"
    )
    node2.safe_sql(f"ALTER ROLE {USER} PASSWORD '{rolpassword}'")

    os.unlink(os.path.join(node1.data_dir, "pg_hba.conf"))
    os.unlink(os.path.join(node2.data_dir, "pg_hba.conf"))

    node1.append_conf(
        """
local   db0             all                                     scram-sha-256
local   db1             all                                     scram-sha-256
""",
        filename="pg_hba.conf",
    )
    node2.append_conf(
        """
local   db2             all                                     scram-sha-256
""",
        filename="pg_hba.conf",
    )

    node1.restart()
    node2.restart()

    # End of test setup

    check_scram_keys_is_not_overwritten(node1, DB0, FDW_INVALID_SERVER2)

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

    check_fdw_auth_with_invalid_overwritten_require_auth(node1, FDW_INVALID_SERVER)

    # Test that use_scram_passthrough=false on user mapping overrides server
    # setting.
    sess = user_session(node1, DB0)
    sess.query_safe(
        f"ALTER USER MAPPING FOR {USER} SERVER {FDW_SERVER3} "
        "OPTIONS(add use_scram_passthrough 'false')"
    )

    res = sess.query(
        f"select * from dblink('{FDW_SERVER3}', 'select * from t') "
        "as t(a int, b int)"
    )
    assert (
        res.error_message is not None
    ), "SCRAM passthrough disabled on user mapping should fail"
    assert re.search(r"password", res.error_message, re.IGNORECASE), (
        "expected password-related error when scram passthrough disabled "
        "on user mapping"
    )
    sess.close()

    # Ensure that trust connections fail without superuser opt-in.
    os.unlink(os.path.join(node1.data_dir, "pg_hba.conf"))
    os.unlink(os.path.join(node2.data_dir, "pg_hba.conf"))

    node1.append_conf(
        """
local   db0             all                                     scram-sha-256
local   db1             all                                     trust
""",
        filename="pg_hba.conf",
    )
    node2.append_conf(
        """
local   all             all                                     password
""",
        filename="pg_hba.conf",
    )

    node1.restart()
    node2.restart()

    sess = user_session(node1, DB0)
    res = sess.query(
        f"SELECT * FROM dblink('{FDW_SERVER}', 'SELECT * FROM t') " "AS t(a int, b int)"
    )
    assert res.error_message is not None, "loopback trust fails on the same cluster"
    assert re.search(
        r'failed: authentication method requirement "scram-sha-256" failed: '
        r"server did not complete authentication",
        res.error_message,
    ), "expected error from loopback trust (same cluster)"

    res = sess.query(
        f"SELECT * FROM dblink('{FDW_SERVER2}', 'SELECT * FROM t2') "
        "AS t2(a int, b int)"
    )
    assert (
        res.error_message is not None
    ), "loopback password fails on a different cluster"
    assert re.search(
        r'authentication method requirement "scram-sha-256" failed: '
        r"server requested a cleartext password",
        res.error_message,
    ), "expected error from loopback password (different cluster)"
    sess.close()
