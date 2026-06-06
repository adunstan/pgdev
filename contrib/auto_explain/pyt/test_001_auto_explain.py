# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that auto_explain logs plans for executed statements."""

import re

import libpq
import pypg


def query_log(node, sql, params=None, user=None):
    """Run *sql* and return the server log emitted while it ran.

    *params* is an optional dict mapping GUC names to values; any such
    settings are transmitted to the backend via the connection "options"
    keyword.  *user*, if given, connects as that role.
    """
    params = params or {}

    connstr = node.connstr("postgres")
    if params:
        opts = " ".join(f"-c {name}={value}" for name, value in params.items())
        connstr += f" options='{opts}'"
    if user is not None:
        connstr += f" user='{user}'"

    offset = node.log_position()

    # A fresh session each time, so the connection-time options take effect.
    sess = libpq.Session(connstr=connstr, libdir=node.libdir)
    try:
        # Send each ';'-terminated statement as a separate simple query, so
        # auto_explain logs each statement's own query text (sending the whole
        # buffer as one query would log the concatenated text).
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                # Keep the ';' so the logged query text includes the statement
                # terminator.
                sess.query_safe(stmt + ";")
    finally:
        sess.close()

    return pypg.util.slurp_file(node.logfile, offset)


def test_auto_explain(create_pg):
    # create_pg(start=False) runs initdb but does not start the server.  The
    # cluster is initialized with trust auth, so regress_user1 can connect
    # without any extra setup.
    node = create_pg("main", start=False)
    node.append_conf(
        "session_preload_libraries = 'pg_overexplain,auto_explain'"
    )
    node.append_conf("auto_explain.log_min_duration = 0")
    node.append_conf("auto_explain.log_analyze = on")
    node.start()

    # Simple query.
    log_contents = query_log(node, "SELECT * FROM pg_class;")

    assert re.search(
        r"Query Text: SELECT \* FROM pg_class;", log_contents
    ), "query text logged, text mode"

    assert not re.search(
        r"Query Parameters:", log_contents
    ), "no query parameters logged when none, text mode"

    assert re.search(
        r"Seq Scan on pg_class", log_contents
    ), "sequential scan logged, text mode"

    # Prepared query.
    log_contents = query_log(
        node,
        "PREPARE get_proc(name) AS SELECT * FROM pg_proc WHERE proname = $1;"
        " EXECUTE get_proc('int4pl');",
    )

    assert re.search(
        r"Query Text: PREPARE get_proc\(name\) AS SELECT \* FROM pg_proc"
        r" WHERE proname = \$1;",
        log_contents,
    ), "prepared query text logged, text mode"

    assert re.search(
        r"Query Parameters: \$1 = 'int4pl'", log_contents
    ), "query parameters logged, text mode"

    assert re.search(
        r"Index Scan using pg_proc_proname_args_nsp_index on pg_proc",
        log_contents,
    ), "index scan logged, text mode"

    # Prepared query with truncated parameters.
    log_contents = query_log(
        node,
        "PREPARE get_type(name) AS SELECT * FROM pg_type WHERE typname = $1;"
        " EXECUTE get_type('float8');",
        {"auto_explain.log_parameter_max_length": 3},
    )

    assert re.search(
        r"Query Text: PREPARE get_type\(name\) AS SELECT \* FROM pg_type"
        r" WHERE typname = \$1;",
        log_contents,
    ), "prepared query text logged, text mode"

    assert re.search(
        r"Query Parameters: \$1 = 'flo\.\.\.'", log_contents
    ), "query parameters truncated, text mode"

    # Prepared query with parameter logging disabled.
    log_contents = query_log(
        node,
        "PREPARE get_type(name) AS SELECT * FROM pg_type WHERE typname = $1;"
        " EXECUTE get_type('float8');",
        {"auto_explain.log_parameter_max_length": 0},
    )

    assert re.search(
        r"Query Text: PREPARE get_type\(name\) AS SELECT \* FROM pg_type"
        r" WHERE typname = \$1;",
        log_contents,
    ), "prepared query text logged, text mode"

    assert not re.search(
        r"Query Parameters:", log_contents
    ), "query parameters not logged when disabled, text mode"

    # Query Identifier.
    # Logging enabled.
    log_contents = query_log(
        node,
        "SELECT * FROM pg_class;",
        {
            "auto_explain.log_verbose": "on",
            "compute_query_id": "on",
        },
    )

    assert re.search(
        r"Query Identifier:", log_contents
    ), "query identifier logged with compute_query_id=on, text mode"

    # Logging disabled.
    log_contents = query_log(
        node,
        "SELECT * FROM pg_class;",
        {
            "auto_explain.log_verbose": "on",
            "compute_query_id": "regress",
        },
    )

    assert not re.search(
        r"Query Identifier:", log_contents
    ), "query identifier not logged with compute_query_id=regress, text mode"

    # JSON format.
    log_contents = query_log(
        node,
        "SELECT * FROM pg_class;",
        {"auto_explain.log_format": "json"},
    )

    assert re.search(
        r'"Query Text": "SELECT \* FROM pg_class;"', log_contents
    ), "query text logged, json mode"

    assert not re.search(
        r'"Query Parameters":', log_contents
    ), "query parameters not logged when none, json mode"

    assert re.search(
        r'"Node Type": "Seq Scan"[^}]*"Relation Name": "pg_class"',
        log_contents,
        re.DOTALL,
    ), "sequential scan logged, json mode"

    # Prepared query in JSON format.
    log_contents = query_log(
        node,
        "PREPARE get_class(name) AS SELECT * FROM pg_class WHERE relname = $1;"
        " EXECUTE get_class('pg_class');",
        {"auto_explain.log_format": "json"},
    )

    assert re.search(
        r'"Query Text": "PREPARE get_class\(name\) AS SELECT \* FROM pg_class'
        r' WHERE relname = \$1;"',
        log_contents,
    ), "prepared query text logged, json mode"

    assert re.search(
        r'"Node Type": "Index Scan"[^}]*"Index Name": "pg_class_relname_nsp_index"',
        log_contents,
        re.DOTALL,
    ), "index scan logged, json mode"

    # Extension options.
    log_contents = query_log(
        node,
        "SELECT 1;",
        {"auto_explain.log_extension_options": "debug"},
    )

    assert re.search(
        r"Parallel Safe:", log_contents
    ), "extension option produces per-node output"

    assert re.search(
        r"Command Type: select", log_contents
    ), "extension option produces per-plan output"

    # Check that PGC_SUSET parameters can be set by non-superuser if granted,
    # otherwise not
    node.safe_sql(
        "CREATE USER regress_user1;"
        " GRANT SET ON PARAMETER auto_explain.log_format TO regress_user1;"
    )

    # queries run as regress_user1
    log_contents = query_log(
        node,
        "SELECT * FROM pg_database;",
        {"auto_explain.log_format": "json"},
        user="regress_user1",
    )

    assert re.search(
        r'"Query Text": "SELECT \* FROM pg_database;"', log_contents
    ), "query text logged, json mode selected by non-superuser"

    log_contents = query_log(
        node,
        "SELECT * FROM pg_database;",
        {"auto_explain.log_level": "log"},
        user="regress_user1",
    )

    assert re.search(
        r'WARNING: ( 42501:)? permission denied to set parameter'
        r' "auto_explain\.log_level"',
        log_contents,
    ), "permission failure logged"
    # end queries run as regress_user1

    node.safe_sql(
        "REVOKE SET ON PARAMETER auto_explain.log_format FROM regress_user1;"
        " DROP USER regress_user1;"
    )

    # Test pg_get_loaded_modules() function.  This function is particularly
    # useful for modules with no SQL presence, such as auto_explain.
    res = node.safe_sql(
        "SELECT module_name,"
        " version = current_setting('server_version') as version_ok,"
        " regexp_replace(file_name, '\\..*', '') as file_name_stripped"
        " FROM pg_get_loaded_modules()"
        " WHERE module_name = 'auto_explain';"
    )
    assert re.search(
        r"^auto_explain\|t\|auto_explain$", res
    ), "pg_get_loaded_modules() ok"

    node.stop()
