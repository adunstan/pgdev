# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests that unlogged tables are properly reinitialized after a crash.

The behavior should be the same when restoring from a backup, but
that is not tested here.
"""

import os

from pypg.util import append_to_file


def test_014_unlogged_reinit(create_pg, tmp_path):
    node = create_pg("main")
    pgdata = node.data_dir

    # Create an unlogged table and an unlogged sequence to test that forks
    # other than init are not copied.
    node.safe_sql("CREATE UNLOGGED TABLE base_unlogged (id int)")
    node.safe_sql("CREATE UNLOGGED SEQUENCE seq_unlogged")

    base_unlogged_path = node.safe_sql(
        "select pg_relation_filepath('base_unlogged')")
    seq_unlogged_path = node.safe_sql(
        "select pg_relation_filepath('seq_unlogged')")

    # Test that main and init forks exist.
    assert os.path.isfile(f"{pgdata}/{base_unlogged_path}_init"), \
        "table init fork exists"
    assert os.path.isfile(f"{pgdata}/{base_unlogged_path}"), \
        "table main fork exists"
    assert os.path.isfile(f"{pgdata}/{seq_unlogged_path}_init"), \
        "sequence init fork exists"
    assert os.path.isfile(f"{pgdata}/{seq_unlogged_path}"), \
        "sequence main fork exists"

    # Test the sequence
    assert node.safe_sql("SELECT nextval('seq_unlogged')") == "1", \
        "sequence nextval"
    assert node.safe_sql("SELECT nextval('seq_unlogged')") == "2", \
        "sequence nextval again"

    # Create an unlogged table in a tablespace.
    tablespace_dir = str(tmp_path / "ts1")
    os.mkdir(tablespace_dir)

    node.safe_sql(f"CREATE TABLESPACE ts1 LOCATION '{tablespace_dir}'")
    node.safe_sql(
        "CREATE UNLOGGED TABLE ts1_unlogged (id int) TABLESPACE ts1")

    ts1_unlogged_path = node.safe_sql(
        "select pg_relation_filepath('ts1_unlogged')")

    # Test that main and init forks exist.
    assert os.path.isfile(f"{pgdata}/{ts1_unlogged_path}_init"), \
        "init fork in tablespace exists"
    assert os.path.isfile(f"{pgdata}/{ts1_unlogged_path}"), \
        "main fork in tablespace exists"

    # Create more unlogged sequences for testing.
    node.safe_sql("CREATE UNLOGGED SEQUENCE seq_unlogged2")
    # This rewrites the sequence relation in AlterSequence().
    node.safe_sql("ALTER SEQUENCE seq_unlogged2 INCREMENT 2")
    node.safe_sql("SELECT nextval('seq_unlogged2')")

    node.safe_sql(
        "CREATE UNLOGGED TABLE tab_seq_unlogged3 "
        "(a int GENERATED ALWAYS AS IDENTITY)")
    # This rewrites the sequence relation in ResetSequence().
    node.safe_sql("TRUNCATE tab_seq_unlogged3 RESTART IDENTITY")
    node.safe_sql("INSERT INTO tab_seq_unlogged3 DEFAULT VALUES")

    # Crash the postmaster.
    node.stop("immediate")

    # Write fake forks to test that they are removed during recovery.
    append_to_file(f"{pgdata}/{base_unlogged_path}_vm", "TEST_VM")
    append_to_file(f"{pgdata}/{base_unlogged_path}_fsm", "TEST_FSM")

    # Remove main fork to test that it is recopied from init.
    os.unlink(f"{pgdata}/{base_unlogged_path}")
    os.unlink(f"{pgdata}/{seq_unlogged_path}")

    # the same for the tablespace
    append_to_file(f"{pgdata}/{ts1_unlogged_path}_vm", "TEST_VM")
    append_to_file(f"{pgdata}/{ts1_unlogged_path}_fsm", "TEST_FSM")
    os.unlink(f"{pgdata}/{ts1_unlogged_path}")

    node.start()

    # check unlogged table in base
    assert os.path.isfile(f"{pgdata}/{base_unlogged_path}_init"), \
        "table init fork in base still exists"
    assert os.path.isfile(f"{pgdata}/{base_unlogged_path}"), \
        "table main fork in base recreated at startup"
    assert not os.path.isfile(f"{pgdata}/{base_unlogged_path}_vm"), \
        "vm fork in base removed at startup"
    assert not os.path.isfile(f"{pgdata}/{base_unlogged_path}_fsm"), \
        "fsm fork in base removed at startup"

    # check unlogged sequence
    assert os.path.isfile(f"{pgdata}/{seq_unlogged_path}_init"), \
        "sequence init fork still exists"
    assert os.path.isfile(f"{pgdata}/{seq_unlogged_path}"), \
        "sequence main fork recreated at startup"

    # Test the sequence after restart
    assert node.safe_sql("SELECT nextval('seq_unlogged')") == "1", \
        "sequence nextval after restart"
    assert node.safe_sql("SELECT nextval('seq_unlogged')") == "2", \
        "sequence nextval after restart again"

    # check unlogged table in tablespace
    assert os.path.isfile(f"{pgdata}/{ts1_unlogged_path}_init"), \
        "init fork still exists in tablespace"
    assert os.path.isfile(f"{pgdata}/{ts1_unlogged_path}"), \
        "main fork in tablespace recreated at startup"
    assert not os.path.isfile(f"{pgdata}/{ts1_unlogged_path}_vm"), \
        "vm fork in tablespace removed at startup"
    assert not os.path.isfile(f"{pgdata}/{ts1_unlogged_path}_fsm"), \
        "fsm fork in tablespace removed at startup"

    # Test other sequences
    assert node.safe_sql("SELECT nextval('seq_unlogged2')") == "1", \
        "altered sequence nextval after restart"
    assert node.safe_sql("SELECT nextval('seq_unlogged2')") == "3", \
        "altered sequence nextval after restart again"

    node.safe_sql(
        "INSERT INTO tab_seq_unlogged3 VALUES (DEFAULT), (DEFAULT)")
    assert node.safe_sql("SELECT * FROM tab_seq_unlogged3") == "1\n2", \
        "reset sequence nextval after restart"
