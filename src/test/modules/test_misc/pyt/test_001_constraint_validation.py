# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Verify that ALTER TABLE optimizes certain operations as expected."""

import re


def is_table_verified(output):
    """Return True if *output* shows that we did a verify pass.

    Matches the DEBUG message emitted when ALTER TABLE has to scan the table
    to validate a constraint.
    """
    return "DEBUG:  verifying table" in output


def test_001_constraint_validation(create_pg):
    node = create_pg("primary", start=False)
    # Turn message level up to DEBUG1 so that we get the messages we want to
    # see.  The DEBUG messages are delivered to the client as notices and are
    # captured via the libpq notice processor.
    node.append_conf("client_min_messages = DEBUG1")
    node.start()

    # Run a SQL command and return the captured stderr (including DEBUG
    # messages).  Uses a single libpq session for the whole test, clearing the
    # notice buffer before each command so each call returns only that
    # command's output.
    sess = node.connect()

    def run_sql_command(sql):
        sess.clear_notices()
        sess.query_safe(sql)
        return sess.get_notices_str()

    # --- test alter table set not null -------------------------------------

    run_sql_command(
        "create table atacc1 (test_a int, test_b int);\n"
        " insert into atacc1 values (1, 2);"
    )

    output = run_sql_command("alter table atacc1 alter test_a set not null;")
    assert is_table_verified(output), "column test_a without constraint will scan table"

    run_sql_command(
        "alter table atacc1 alter test_a drop not null;\n"
        " alter table atacc1 add constraint atacc1_constr_a_valid\n"
        " check(test_a is not null);"
    )

    # normal run will verify table data
    output = run_sql_command("alter table atacc1 alter test_a set not null;")
    assert not is_table_verified(output), "with constraint will not scan table"
    assert re.search(
        r'existing constraints on column "atacc1.test_a" are sufficient to '
        r"prove that it does not contain nulls",
        output,
    ), "test_a proved by constraints"

    run_sql_command("alter table atacc1 alter test_a drop not null;")

    # we have check only for test_a column, so we need verify table for test_b
    output = run_sql_command(
        "alter table atacc1 alter test_b set not null, alter test_a set not null;"
    )
    assert is_table_verified(output), "table was scanned"
    # we may miss debug message for test_a constraint because we need verify
    # table due test_b
    assert not re.search(
        r'existing constraints on column "atacc1.test_b" are sufficient to '
        r"prove that it does not contain nulls",
        output,
    ), "test_b not proved by wrong constraints"
    run_sql_command(
        "alter table atacc1 alter test_a drop not null, alter test_b drop not null;"
    )

    # test with both columns having check constraints
    run_sql_command(
        "alter table atacc1 add constraint atacc1_constr_b_valid "
        "check(test_b is not null);"
    )
    output = run_sql_command(
        "alter table atacc1 alter test_b set not null, alter test_a set not null;"
    )
    assert not is_table_verified(output), "table was not scanned for both columns"
    assert re.search(
        r'existing constraints on column "atacc1.test_a" are sufficient to '
        r"prove that it does not contain nulls",
        output,
    ), "test_a proved by constraints"
    assert re.search(
        r'existing constraints on column "atacc1.test_b" are sufficient to '
        r"prove that it does not contain nulls",
        output,
    ), "test_b proved by constraints"
    run_sql_command("drop table atacc1;")

    # --- test alter table attach partition ---------------------------------

    run_sql_command(
        "CREATE TABLE list_parted2 (\n"
        " a int,\n"
        " b char\n"
        " ) PARTITION BY LIST (a);\n"
        " CREATE TABLE part_3_4 (\n"
        " LIKE list_parted2,\n"
        " CONSTRAINT check_a CHECK (a IN (3)));"
    )

    # need NOT NULL to skip table scan
    output = run_sql_command(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_3_4 FOR VALUES IN (3, 4);"
    )
    assert is_table_verified(output), "table part_3_4 scanned"

    run_sql_command(
        "ALTER TABLE list_parted2 DETACH PARTITION part_3_4;\n"
        " ALTER TABLE part_3_4 ALTER a SET NOT NULL;"
    )

    output = run_sql_command(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_3_4 FOR VALUES IN (3, 4);"
    )
    assert not is_table_verified(output), "table part_3_4 not scanned"
    assert re.search(
        r'partition constraint for table "part_3_4" is implied by existing '
        r"constraints",
        output,
    ), "part_3_4 verified by existing constraints"

    # test attach default partition
    run_sql_command(
        "CREATE TABLE list_parted2_def (\n"
        " LIKE list_parted2,\n"
        " CONSTRAINT check_a CHECK (a IN (5, 6)));"
    )
    output = run_sql_command(
        "ALTER TABLE list_parted2 ATTACH PARTITION list_parted2_def default;"
    )
    assert not is_table_verified(output), "table list_parted2_def not scanned"
    assert re.search(
        r'partition constraint for table "list_parted2_def" is implied by '
        r"existing constraints",
        output,
    ), "list_parted2_def verified by existing constraints"

    output = run_sql_command(
        "CREATE TABLE part_55_66 PARTITION OF list_parted2 FOR VALUES IN (55, 66);"
    )
    assert not is_table_verified(output), "table list_parted2_def not scanned"
    assert re.search(
        r"updated partition constraint for default partition "
        r'"list_parted2_def" is implied by existing constraints',
        output,
    ), "updated partition constraint for default partition list_parted2_def"

    # test attach another partitioned table
    run_sql_command(
        "CREATE TABLE part_5 (\n"
        " LIKE list_parted2\n"
        " ) PARTITION BY LIST (b);\n"
        " CREATE TABLE part_5_a PARTITION OF part_5 FOR VALUES IN ('a');\n"
        " ALTER TABLE part_5 ADD CONSTRAINT check_a "
        "CHECK (a IS NOT NULL AND a = 5);"
    )
    output = run_sql_command(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_5 FOR VALUES IN (5);"
    )
    assert not re.search(
        r'verifying table "part_5"', output
    ), "table part_5 not scanned"
    assert re.search(
        r'verifying table "list_parted2_def"', output
    ), "list_parted2_def scanned"
    assert re.search(
        r'partition constraint for table "part_5" is implied by existing '
        r"constraints",
        output,
    ), "part_5 verified by existing constraints"

    run_sql_command(
        "ALTER TABLE list_parted2 DETACH PARTITION part_5;\n"
        " ALTER TABLE part_5 DROP CONSTRAINT check_a;"
    )

    # scan should again be skipped, even though NOT NULL is now a column
    # property
    run_sql_command(
        "ALTER TABLE part_5 ADD CONSTRAINT check_a CHECK (a IN (5)),\n"
        " ALTER a SET NOT NULL;"
    )
    output = run_sql_command(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_5 FOR VALUES IN (5);"
    )
    assert not re.search(
        r'verifying table "part_5"', output
    ), "table part_5 not scanned"
    assert re.search(
        r'verifying table "list_parted2_def"', output
    ), "list_parted2_def scanned"
    assert re.search(
        r'partition constraint for table "part_5" is implied by existing '
        r"constraints",
        output,
    ), "part_5 verified by existing constraints"

    # Check the case where attnos of the partitioning columns in the table
    # being attached differs from the parent.  It should not affect the
    # constraint-checking logic that allows to skip the scan.
    run_sql_command(
        "CREATE TABLE part_6 (\n"
        " c int,\n"
        " LIKE list_parted2,\n"
        " CONSTRAINT check_a CHECK (a IS NOT NULL AND a = 6)\n"
        " );\n"
        " ALTER TABLE part_6 DROP c;"
    )
    output = run_sql_command(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_6 FOR VALUES IN (6);"
    )
    assert not re.search(
        r'verifying table "part_6"', output
    ), "table part_6 not scanned"
    assert re.search(
        r'verifying table "list_parted2_def"', output
    ), "list_parted2_def scanned"
    assert re.search(
        r'partition constraint for table "part_6" is implied by existing '
        r"constraints",
        output,
    ), "part_6 verified by existing constraints"

    # Similar to above, but the table being attached is a partitioned table
    # whose partition has still different attnos for the root partitioning
    # columns.
    run_sql_command(
        "CREATE TABLE part_7 (\n"
        " LIKE list_parted2,\n"
        " CONSTRAINT check_a CHECK (a IS NOT NULL AND a = 7)\n"
        " ) PARTITION BY LIST (b);\n"
        " CREATE TABLE part_7_a_null (\n"
        " c int,\n"
        " d int,\n"
        " e int,\n"
        " LIKE list_parted2,  -- a will have attnum = 4\n"
        " CONSTRAINT check_b CHECK (b IS NULL OR b = 'a'),\n"
        " CONSTRAINT check_a CHECK (a IS NOT NULL AND a = 7)\n"
        " );\n"
        " ALTER TABLE part_7_a_null DROP c, DROP d, DROP e;"
    )

    output = run_sql_command(
        "ALTER TABLE part_7 ATTACH PARTITION part_7_a_null "
        "FOR VALUES IN ('a', null);"
    )
    assert not is_table_verified(output), "table not scanned"
    assert re.search(
        r'partition constraint for table "part_7_a_null" is implied by '
        r"existing constraints",
        output,
    ), "part_7_a_null verified by existing constraints"
    output = run_sql_command(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_7 FOR VALUES IN (7);"
    )
    assert not is_table_verified(output), "tables not scanned"
    assert re.search(
        r'partition constraint for table "part_7" is implied by existing '
        r"constraints",
        output,
    ), "part_7 verified by existing constraints"
    assert re.search(
        r"updated partition constraint for default partition "
        r'"list_parted2_def" is implied by existing constraints',
        output,
    ), "updated partition constraint for default partition list_parted2_def"

    run_sql_command(
        "CREATE TABLE range_parted (\n"
        " a int,\n"
        " b int\n"
        " ) PARTITION BY RANGE (a, b);\n"
        " CREATE TABLE range_part1 (\n"
        " a int NOT NULL CHECK (a = 1),\n"
        " b int NOT NULL);"
    )

    output = run_sql_command(
        "ALTER TABLE range_parted ATTACH PARTITION range_part1 "
        "FOR VALUES FROM (1, 1) TO (1, 10);"
    )
    assert is_table_verified(output), "table range_part1 scanned"
    assert not re.search(
        r'partition constraint for table "range_part1" is implied by existing '
        r"constraints",
        output,
    ), "range_part1 not verified by existing constraints"

    run_sql_command(
        "CREATE TABLE range_part2 (\n"
        " a int NOT NULL CHECK (a = 1),\n"
        " b int NOT NULL CHECK (b >= 10 and b < 18)\n"
        ");"
    )
    output = run_sql_command(
        "ALTER TABLE range_parted ATTACH PARTITION range_part2 "
        "FOR VALUES FROM (1, 10) TO (1, 20);"
    )
    assert not is_table_verified(output), "table range_part2 not scanned"
    assert re.search(
        r'partition constraint for table "range_part2" is implied by existing '
        r"constraints",
        output,
    ), "range_part2 verified by existing constraints"

    # If a partitioned table being created or an existing table being attached
    # as a partition does not have a constraint that would allow validation
    # scan to be skipped, but an individual partition does, then the
    # partition's validation scan is skipped.
    run_sql_command(
        "CREATE TABLE quuux (a int, b text) PARTITION BY LIST (a);\n"
        " CREATE TABLE quuux_default PARTITION OF quuux DEFAULT "
        "PARTITION BY LIST (b);\n"
        " CREATE TABLE quuux_default1 PARTITION OF quuux_default (\n"
        " CONSTRAINT check_1 CHECK (a IS NOT NULL AND a = 1)\n"
        " ) FOR VALUES IN ('b');\n"
        " CREATE TABLE quuux1 (a int, b text);"
    )

    output = run_sql_command(
        "ALTER TABLE quuux ATTACH PARTITION quuux1 FOR VALUES IN (1);"
    )
    assert is_table_verified(output), "quuux1 table scanned"
    assert not re.search(
        r'partition constraint for table "quuux1" is implied by existing '
        r"constraints",
        output,
    ), "quuux1 verified by existing constraints"

    run_sql_command("CREATE TABLE quuux2 (a int, b text);")
    output = run_sql_command(
        "ALTER TABLE quuux ATTACH PARTITION quuux2 FOR VALUES IN (2);"
    )
    assert not re.search(
        r'verifying table "quuux_default1"', output
    ), "quuux_default1 not scanned"
    assert re.search(r'verifying table "quuux2"', output), "quuux2 scanned"
    assert re.search(
        r'updated partition constraint for default partition "quuux_default1" '
        r"is implied by existing constraints",
        output,
    ), "updated partition constraint for default partition quuux_default1"
    run_sql_command("DROP TABLE quuux1, quuux2;")

    # should validate for quuux1, but not for quuux2
    output = run_sql_command(
        "CREATE TABLE quuux1 PARTITION OF quuux FOR VALUES IN (1);"
    )
    assert not is_table_verified(output), "tables not scanned"
    assert not re.search(
        r'partition constraint for table "quuux1" is implied by existing '
        r"constraints",
        output,
    ), "quuux1 verified by existing constraints"
    output = run_sql_command(
        "CREATE TABLE quuux2 PARTITION OF quuux FOR VALUES IN (2);"
    )
    assert not is_table_verified(output), "tables not scanned"
    assert re.search(
        r'updated partition constraint for default partition "quuux_default1" '
        r"is implied by existing constraints",
        output,
    ), "updated partition constraint for default partition quuux_default1"
    run_sql_command("DROP TABLE quuux;")

    sess.close()
    node.stop("fast")
