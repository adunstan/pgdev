# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test the worker_spi module."""


def test_001_worker_spi(create_pg):
    node = create_pg("mynode", start=False)
    node.start()

    # testing dynamic bgworkers

    node.safe_sql("CREATE EXTENSION worker_spi;")

    # Launch one dynamic worker, then wait for its initialization to complete.
    # This consists in making sure that a table name "counted" is created
    # on a new schema whose name includes the index defined in input argument
    # of worker_spi_launch().
    # By default, dynamic bgworkers connect to the "postgres" database with
    # an undefined role, falling back to the GUC defaults (or InvalidOid for
    # worker_spi_launch).
    result = node.safe_sql("SELECT worker_spi_launch(4) IS NOT NULL;")
    assert result == "t", "dynamic bgworker launched"
    assert node.poll_query_until(
        "SELECT count(*) > 0 FROM information_schema.tables\n"
        "    WHERE table_schema = 'schema4' AND table_name = 'counted';")
    node.safe_sql(
        "INSERT INTO schema4.counted VALUES ('total', 0), ('delta', 1);")
    # Issue a SIGHUP on the node to force the worker to loop once, accelerating
    # this test.
    node.reload()
    # Wait until the worker has processed the tuple that has just been inserted.
    assert node.poll_query_until(
        "SELECT count(*) FROM schema4.counted WHERE type = 'delta';", "0")
    result = node.safe_sql("SELECT * FROM schema4.counted;")
    assert result == "total|1", "dynamic bgworker correctly consumed tuple data"

    # Check the wait event used by the dynamic bgworker.
    assert node.poll_query_until(
        "SELECT wait_event FROM pg_stat_activity "
        "WHERE backend_type ~ 'worker_spi';",
        "WorkerSpiMain"), \
        'dynamic bgworker has reported "WorkerSpiMain" as wait event'

    # Check the wait event used by the dynamic bgworker appears in
    # pg_wait_events
    result = node.safe_sql(
        "SELECT count(*) > 0 from pg_wait_events where type = 'Extension' "
        "and name = 'WorkerSpiMain';")
    assert result == "t", '"WorkerSpiMain" is reported in pg_wait_events'

    # testing bgworkers loaded with shared_preload_libraries

    # Create the database first so as the workers can connect to it when
    # the library is loaded.
    node.safe_sql("CREATE DATABASE mydb;")
    node.safe_sql("CREATE ROLE myrole SUPERUSER LOGIN;")
    node.safe_sql("CREATE EXTENSION worker_spi;", dbname="mydb")

    # Now load the module as a shared library.
    # Update max_worker_processes to make room for enough bgworkers, including
    # parallel workers these may spawn.
    node.append_conf("""
shared_preload_libraries = 'worker_spi'
worker_spi.database = 'mydb'
worker_spi.total_workers = 3
max_worker_processes = 32
""")
    node.restart()

    # Check that bgworkers have been registered and launched.
    assert node.poll_query_until(
        "SELECT datname, count(datname), wait_event FROM pg_stat_activity\n"
        "            WHERE backend_type = 'worker_spi' "
        "GROUP BY datname, wait_event;",
        "mydb|3|WorkerSpiMain", dbname="mydb"), \
        "Timed out while waiting for bgworkers to be launched"

    # Ask worker_spi to launch dynamic bgworkers with the library loaded, then
    # check their existence.  Use IDs that do not overlap with the schemas
    # created by the previous workers.  These ones use a new role, on different
    # databases.
    myrole_id = node.safe_sql(
        "SELECT oid FROM pg_roles where rolname = 'myrole';", dbname="mydb")
    mydb_id = node.safe_sql(
        "SELECT oid FROM pg_database where datname = 'mydb';", dbname="mydb")
    postgresdb_id = node.safe_sql(
        "SELECT oid FROM pg_database where datname = 'postgres';",
        dbname="mydb")
    worker1_pid = node.safe_sql(
        f"SELECT worker_spi_launch(10, {mydb_id}, {myrole_id});",
        dbname="mydb")
    worker2_pid = node.safe_sql(
        f"SELECT worker_spi_launch(11, {postgresdb_id}, {myrole_id});",
        dbname="mydb")

    assert node.poll_query_until(
        "SELECT datname, usename, wait_event FROM pg_stat_activity\n"
        "            WHERE backend_type = 'worker_spi dynamic' AND\n"
        f"            pid IN ({worker1_pid}, {worker2_pid}) ORDER BY datname;",
        "mydb|myrole|WorkerSpiMain\npostgres|myrole|WorkerSpiMain",
        dbname="mydb"), \
        "Timed out while waiting for dynamic bgworkers to be launched"

    # Check BGWORKER_BYPASS_ALLOWCONN.
    node.safe_sql("CREATE DATABASE noconndb ALLOW_CONNECTIONS false;")
    noconndb_id = node.safe_sql(
        "SELECT oid FROM pg_database where datname = 'noconndb';",
        dbname="mydb")
    log_offset = node.log_position()

    # worker_spi_launch() may be able to detect that the worker has been
    # stopped, so do not rely on safe_sql().
    sess = node.connect()
    try:
        sess.query(
            f"SELECT worker_spi_launch(12, {noconndb_id}, {myrole_id});")
    finally:
        sess.close()
    node.wait_for_log(
        r'database "noconndb" is not currently accepting connections',
        log_offset)

    # bgworker bypasses the connection check, and can be launched.
    worker4_pid = node.safe_sql(
        f"SELECT worker_spi_launch(12, {noconndb_id}, {myrole_id}, "
        "'{\"ALLOWCONN\"}');")
    assert node.poll_query_until(
        "SELECT datname, usename, wait_event FROM pg_stat_activity\n"
        "            WHERE backend_type = 'worker_spi dynamic' AND\n"
        f"            pid IN ({worker4_pid}) ORDER BY datname;",
        "noconndb|myrole|WorkerSpiMain"), \
        "dynamic bgworker with BYPASS_ALLOWCONN started"

    # Check BGWORKER_BYPASS_ROLELOGINCHECK.
    # First create a role without login access.
    node.safe_sql("""
  CREATE ROLE nologrole WITH NOLOGIN;
  GRANT CREATE ON DATABASE mydb TO nologrole;
""")
    nologrole_id = node.safe_sql(
        "SELECT oid FROM pg_roles where rolname = 'nologrole';", dbname="mydb")
    log_offset = node.log_position()

    # bgworker cannot be launched with login restriction.
    sess = node.connect()
    try:
        sess.query(
            f"SELECT worker_spi_launch(13, {mydb_id}, {nologrole_id});")
    finally:
        sess.close()
    node.wait_for_log(
        r'role "nologrole" is not permitted to log in', log_offset)

    # bgworker bypasses the login restriction, and can be launched.
    log_offset = node.log_position()
    worker5_pid = node.safe_sql(
        f"SELECT worker_spi_launch(13, {mydb_id}, {nologrole_id}, "
        "'{\"ROLELOGINCHECK\"}');", dbname="mydb")
    assert node.poll_query_until(
        "SELECT datname, usename, wait_event FROM pg_stat_activity\n"
        "            WHERE backend_type = 'worker_spi dynamic' AND\n"
        f"            pid = {worker5_pid};",
        "mydb|nologrole|WorkerSpiMain", dbname="mydb"), \
        "dynamic bgworker with BYPASS_ROLELOGINCHECK launched"
