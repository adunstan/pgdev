# Copyright (c) 2026, PostgreSQL Global Development Group

"""Tests for pg_get_database_ddl(), pg_get_tablespace_ddl(), and pg_get_role_ddl().

These run as a standalone suite rather than as plain regression tests because
they create databases and tablespaces, which are heavyweight operations that
should run only once rather than being repeated with every invocation of the
core regression suite.
"""

import re


def _ddl_filter(text):
    """Strip locale/collation details from DDL output so that results are
    stable across platforms."""
    text = re.sub(
        r"\s*\bLOCALE_PROVIDER\b\s*=\s*(?:'[^']*'|\"[^\"]*\"|\S+)",
        "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s*LC_COLLATE\s*=\s*(['\"])[^'\"]*\1", "", text,
        flags=re.IGNORECASE)
    text = re.sub(
        r"\s*LC_CTYPE\s*=\s*(['\"])[^'\"]*\1", "", text,
        flags=re.IGNORECASE)
    text = re.sub(
        r"\s*\S*LOCALE\S*\s*=?\s*(['\"])[^'\"]*\1", "", text,
        flags=re.IGNORECASE)
    text = re.sub(
        r"\s*\S*COLLATION\S*\s*=?\s*(['\"])[^'\"]*\1", "", text,
        flags=re.IGNORECASE)
    return text


def test_012_ddlutils(create_pg):
    node = create_pg("main", start=False)
    # Force UTC so that timestamptz values (e.g. VALID UNTIL) render the same
    # way regardless of the host's local timezone.
    node.append_conf("timezone = 'UTC'\n")
    node.start()

    ####################################################################
    # pg_get_role_ddl tests
    ####################################################################

    # Basic role
    node.safe_sql("CREATE ROLE regress_role_ddl_test1")
    result = node.safe_sql(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_test1')")
    assert re.search(r"CREATE ROLE regress_role_ddl_test1 .* NOLOGIN", result), \
        "basic role DDL"

    # Role with multiple privileges
    node.safe_sql("""
        CREATE ROLE regress_role_ddl_test2
          LOGIN SUPERUSER CREATEDB CREATEROLE
          CONNECTION LIMIT 5
          VALID UNTIL '2030-12-31 23:59:59+00'""")
    result = node.safe_sql(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_test2')")
    assert "SUPERUSER" in result, "role with SUPERUSER"
    assert "CREATEDB" in result, "role with CREATEDB"
    assert "CONNECTION LIMIT 5" in result, "role with CONNECTION LIMIT"
    assert "VALID UNTIL '2030-12-31" in result, "role with VALID UNTIL"

    # Role with configuration parameters
    node.safe_sql("""
        ALTER ROLE regress_role_ddl_test1 SET work_mem TO '256MB';
        ALTER ROLE regress_role_ddl_test1 SET search_path TO myschema, public""")
    result = node.safe_sql(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_test1')")
    assert "SET work_mem TO '256MB'" in result, "role with work_mem setting"
    assert "SET search_path TO" in result, "role with search_path setting"

    # Role with database-specific configuration (needs a real database).
    # CREATE DATABASE cannot run inside a transaction block, so it must run
    # as its own statement.
    node.safe_sql("""
        CREATE DATABASE regression_ddlutils_test
          TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'""")
    node.safe_sql("""
        ALTER ROLE regress_role_ddl_test2
          IN DATABASE regression_ddlutils_test SET work_mem TO '128MB'""")
    result = node.safe_sql(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_test2')")
    assert "IN DATABASE regression_ddlutils_test SET work_mem TO '128MB'" \
        in result, "role with database-specific setting"

    # Role with special characters (requires quoting)
    node.safe_sql('CREATE ROLE "regress_role-with-dash"')
    result = node.safe_sql(
        "SELECT * FROM pg_get_role_ddl('regress_role-with-dash')")
    assert '"regress_role-with-dash"' in result, "role name requiring quoting"

    # Pretty-printed output
    result = node.safe_sql(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_test2', "
        "'pretty', 'true')")
    assert re.search(r"\n\s+SUPERUSER", result), \
        "role pretty-print indents attributes"

    # Role with memberships
    node.safe_sql("""
        CREATE ROLE regress_role_ddl_grantor CREATEROLE;
        CREATE ROLE regress_role_ddl_group1;
        CREATE ROLE regress_role_ddl_group2;
        CREATE ROLE regress_role_ddl_member;
        GRANT regress_role_ddl_group1 TO regress_role_ddl_grantor WITH ADMIN TRUE;
        GRANT regress_role_ddl_group2 TO regress_role_ddl_grantor WITH ADMIN TRUE;
        SET ROLE regress_role_ddl_grantor;
        GRANT regress_role_ddl_group1 TO regress_role_ddl_member
          WITH INHERIT TRUE, SET FALSE;
        GRANT regress_role_ddl_group2 TO regress_role_ddl_member
          WITH ADMIN TRUE;
        RESET ROLE""")
    result = node.safe_sql(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_member')")
    assert "GRANT regress_role_ddl_group1 TO regress_role_ddl_member" in \
        result, "role with memberships includes GRANT"
    assert "SET FALSE" in result, "membership includes SET FALSE"
    assert "ADMIN TRUE" in result, "membership includes ADMIN TRUE"

    # Memberships suppressed
    result = node.safe_sql(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_member', "
        "'memberships', 'false')")
    assert "GRANT" not in result, "memberships suppressed"

    # Non-existent role (should error)
    res = node.sql("SELECT * FROM pg_get_role_ddl(9999999::oid)")
    assert res.error_message is not None, "non-existent role errors"
    assert "does not exist" in res.error_message, \
        "non-existent role error message"

    # NULL input (should return no rows)
    result = node.safe_sql("SELECT count(*) FROM pg_get_role_ddl(NULL)")
    assert result == "0", "NULL role returns no rows"

    # Permission check: revoke SELECT on pg_authid
    node.safe_sql("""
        CREATE ROLE regress_role_ddl_noaccess;
        REVOKE SELECT ON pg_authid FROM PUBLIC""")
    noaccess = node.connect()
    try:
        res = noaccess.query(
            "SET ROLE regress_role_ddl_noaccess;\n"
            "SELECT * FROM pg_get_role_ddl('regress_role_ddl_test1')")
        assert res.error_message is not None, \
            "role DDL denied without pg_authid access"
    finally:
        noaccess.close()
    node.safe_sql("GRANT SELECT ON pg_authid TO PUBLIC")

    ####################################################################
    # pg_get_database_ddl tests
    ####################################################################

    # Set up: the test database was already created above for role tests.
    node.safe_sql("""
        ALTER DATABASE regression_ddlutils_test OWNER TO regress_role_ddl_test2;
        ALTER DATABASE regression_ddlutils_test CONNECTION LIMIT 123;
        ALTER DATABASE regression_ddlutils_test SET random_page_cost = 2.0;
        ALTER ROLE regress_role_ddl_test2
          IN DATABASE regression_ddlutils_test SET random_page_cost = 1.1""")

    # Non-existent database
    res = node.sql(
        "SELECT * FROM pg_get_database_ddl('regression_no_such_db')")
    assert res.error_message is not None, "non-existent database errors"

    # NULL input
    result = node.safe_sql(
        "SELECT count(*) FROM pg_get_database_ddl(NULL)")
    assert result == "0", "NULL database returns no rows"

    # Invalid option
    res = node.sql(
        "SELECT * FROM pg_get_database_ddl('regression_ddlutils_test', "
        "'owner', 'invalid')")
    assert res.error_message is not None, "invalid boolean option errors"
    assert "invalid value" in res.error_message, \
        "invalid option error message"

    # Duplicate option
    res = node.sql(
        "SELECT * FROM pg_get_database_ddl('regression_ddlutils_test', "
        "'owner', 'false', 'owner', 'true')")
    assert res.error_message is not None, "duplicate option errors"

    # Basic output (without locale details)
    result = _ddl_filter(node.safe_sql(
        "SELECT pg_get_database_ddl "
        "FROM pg_get_database_ddl('regression_ddlutils_test')"))
    assert "CREATE DATABASE regression_ddlutils_test" in result, \
        "database DDL includes CREATE"
    assert "TEMPLATE = template0" in result, "database DDL includes TEMPLATE"
    assert "ENCODING = 'UTF8'" in result, "database DDL includes ENCODING"
    assert "OWNER TO regress_role_ddl_test2" in result, \
        "database DDL includes OWNER"
    assert "CONNECTION LIMIT = 123" in result, \
        "database DDL includes CONNLIMIT"
    assert "SET random_page_cost TO '2.0'" in result, \
        "database DDL includes GUC setting"

    # Pretty-printed output
    result = _ddl_filter(node.safe_sql(
        "SELECT pg_get_database_ddl "
        "FROM pg_get_database_ddl('regression_ddlutils_test', "
        "'pretty', 'true', 'tablespace', 'false')"))
    assert re.search(r"\n\s+WITH TEMPLATE", result), \
        "database DDL pretty-prints WITH"

    # Permission check
    node.safe_sql(
        "REVOKE CONNECT ON DATABASE regression_ddlutils_test FROM PUBLIC")
    noaccess = node.connect()
    try:
        res = noaccess.query(
            "SET ROLE regress_role_ddl_noaccess;\n"
            "SELECT * FROM pg_get_database_ddl('regression_ddlutils_test')")
        assert res.error_message is not None, \
            "database DDL denied without CONNECT"
    finally:
        noaccess.close()
    node.safe_sql(
        "GRANT CONNECT ON DATABASE regression_ddlutils_test TO PUBLIC")

    ####################################################################
    # pg_get_tablespace_ddl tests
    ####################################################################

    # Non-existent tablespace by name
    res = node.sql(
        "SELECT * FROM pg_get_tablespace_ddl('regress_nonexistent_tblsp')")
    assert res.error_message is not None, "non-existent tablespace errors"

    # Non-existent tablespace by OID
    res = node.sql("SELECT * FROM pg_get_tablespace_ddl(0::oid)")
    assert res.error_message is not None, "non-existent tablespace OID errors"

    # NULL input (name and OID variants)
    result = node.safe_sql(
        "SELECT count(*) FROM pg_get_tablespace_ddl(NULL::name)")
    assert result == "0", "NULL tablespace name returns no rows"
    result = node.safe_sql(
        "SELECT count(*) FROM pg_get_tablespace_ddl(NULL::oid)")
    assert result == "0", "NULL tablespace OID returns no rows"

    # Tablespace name requiring quoting.  CREATE TABLESPACE cannot run inside
    # a transaction block, so the GUC and the CREATE run as separate
    # statements on the (persistent) session.
    node.safe_sql("SET allow_in_place_tablespaces = true")
    node.safe_sql("""
        CREATE TABLESPACE "regress_ tblsp" OWNER regress_role_ddl_test1
          LOCATION ''""")
    result = node.safe_sql(
        "SELECT * FROM pg_get_tablespace_ddl('regress_ tblsp')")
    assert '"regress_ tblsp"' in result, "tablespace name is quoted"

    # Rename and add options; reuse this tablespace for the remaining tests
    node.safe_sql("""
        ALTER TABLESPACE "regress_ tblsp" RENAME TO regress_allopt_tblsp;
        ALTER TABLESPACE regress_allopt_tblsp
          SET (seq_page_cost = '1.5', random_page_cost = '1.1234567890',
               effective_io_concurrency = '17', maintenance_io_concurrency = '18')""")

    # Tablespace with multiple options
    result = node.safe_sql(
        "SELECT * FROM pg_get_tablespace_ddl('regress_allopt_tblsp')")
    assert "CREATE TABLESPACE regress_allopt_tblsp" in result, \
        "tablespace DDL includes CREATE"
    assert "OWNER regress_role_ddl_test1" in result, \
        "tablespace DDL includes OWNER"
    assert "seq_page_cost='1.5'" in result, "tablespace DDL includes options"

    # Pretty-printed output
    result = node.safe_sql(
        "SELECT * FROM pg_get_tablespace_ddl('regress_allopt_tblsp', "
        "'pretty', 'true')")
    assert re.search(r"\n\s+OWNER", result), \
        "tablespace DDL pretty-prints OWNER"

    # Owner suppressed
    result = node.safe_sql(
        "SELECT * FROM pg_get_tablespace_ddl('regress_allopt_tblsp', "
        "'owner', 'false')")
    assert "OWNER" not in result, "tablespace DDL owner suppressed"

    # Lookup by OID
    result = node.safe_sql("""
        SELECT pg_get_tablespace_ddl
        FROM pg_get_tablespace_ddl(
          (SELECT oid FROM pg_tablespace
           WHERE spcname = 'regress_allopt_tblsp'))""")
    assert "CREATE TABLESPACE regress_allopt_tblsp" in result, \
        "tablespace DDL by OID"

    # Permission check
    node.safe_sql("REVOKE SELECT ON pg_tablespace FROM PUBLIC")
    noaccess = node.connect()
    try:
        res = noaccess.query(
            "SET ROLE regress_role_ddl_noaccess;\n"
            "SELECT * FROM pg_get_tablespace_ddl('regress_allopt_tblsp')")
        assert res.error_message is not None, \
            "tablespace DDL denied without pg_tablespace access"
    finally:
        noaccess.close()
    node.safe_sql("GRANT SELECT ON pg_tablespace TO PUBLIC")

    node.stop()
