# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic test of a custom WAL resource manager.

Insert a custom WAL record via the test_custom_rmgrs extension and verify,
using pg_walinspect, that the custom resource manager registered and decoded
the record correctly.
"""


def test_001_basic(create_pg):
    node = create_pg("main", start=False)

    node.append_conf(
        "wal_level = 'replica'\n"
        "max_wal_senders = 4\n"
        "shared_preload_libraries = 'test_custom_rmgrs'\n"
    )
    node.start()

    # setup
    node.safe_sql("CREATE EXTENSION test_custom_rmgrs")

    # pg_walinspect is required only for verifying test_custom_rmgrs output.
    # test_custom_rmgrs doesn't use/depend on it internally.
    node.safe_sql("CREATE EXTENSION pg_walinspect")

    # make sure checkpoints don't interfere with the test.
    start_lsn = node.safe_sql(
        "SELECT lsn FROM pg_create_physical_replication_slot("
        "'regress_test_slot1', true, false);"
    )

    # write and save the WAL record's returned end LSN for verifying it later
    record_end_lsn = node.safe_sql(
        "SELECT * FROM test_custom_rmgrs_insert_wal_record('payload123')"
    )

    # ensure the WAL is written and flushed to disk
    node.safe_sql("SELECT pg_switch_wal()")

    end_lsn = node.safe_sql("SELECT pg_current_wal_flush_lsn()")

    # check if our custom WAL resource manager has successfully registered with
    # the server
    row_count = node.safe_sql(
        "SELECT count(*) FROM pg_get_wal_resource_managers() "
        "WHERE rm_name = 'test_custom_rmgrs';"
    )
    assert row_count == "1", (
        "custom WAL resource manager has successfully registered with the server"
    )

    # check if our custom WAL resource manager has successfully written a WAL
    # record
    expected = (
        f"{record_end_lsn}|test_custom_rmgrs|TEST_CUSTOM_RMGRS_MESSAGE|0|"
        "payload (10 bytes): payload123"
    )
    result = node.safe_sql(
        "SELECT end_lsn, resource_manager, record_type, fpi_length, description "
        f"FROM pg_get_wal_records_info('{start_lsn}', '{end_lsn}') "
        "WHERE resource_manager = 'test_custom_rmgrs';"
    )
    assert result == expected, (
        "custom WAL resource manager has successfully written a WAL record"
    )

    node.stop()
