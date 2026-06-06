# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Check that pg_check_visible() and pg_check_frozen() report correct TIDs for corruption."""

import os
import shutil


def test_002_corrupt_vm(create_pg):
    node = create_pg("main", start=False)
    # Anything holding a snapshot, including auto-analyze of pg_proc, could stop
    # VACUUM from updating the visibility map.
    node.append_conf("autovacuum=off")
    node.start()

    blck_size = node.safe_sql("SHOW block_size;")

    # Create a sample table with at least 10 pages and then run VACUUM. 10 is
    # selected manually as it is big enough to select 5 random tuples from the
    # relation.
    node.safe_sql(
        f"""
            CREATE EXTENSION pg_visibility;
            CREATE TABLE corruption_test
                WITH (autovacuum_enabled = false) AS
                SELECT
                    i,
                    repeat('a', 10) AS data
                FROM
                    generate_series(1, {blck_size}) i;
    """
    )
    node.safe_sql("VACUUM (FREEZE, DISABLE_PAGE_SKIPPING) corruption_test;")

    # VACUUM is run, it is safe to get the number of pages.
    npages = node.safe_sql(
        "SELECT relpages FROM pg_class\n\t\tWHERE relname = 'corruption_test';"
    )
    assert int(npages) >= 10, "table has at least 10 pages"

    file = node.safe_sql("SELECT pg_relation_filepath('corruption_test');")

    # Delete the first block to make sure that it will be skipped as it is
    # not visible nor frozen.
    node.safe_sql("DELETE FROM corruption_test\n\t\tWHERE (ctid::text::point)[0] = 0;")

    # Copy visibility map.
    node.stop()
    vm_file = os.path.join(node.data_dir, file + "_vm")
    shutil.copy(vm_file, vm_file + "_temp")
    node.start()

    # Select 5 random tuples that are starting from the second block of the
    # relation. The first block is skipped because it is deleted above.
    tuples = node.safe_sql(
        "SELECT ctid FROM (\n"
        "\t\tSELECT ctid FROM corruption_test\n"
        "\t\t\tWHERE (ctid::text::point)[0] != 0\n"
        "\t\t\tORDER BY random() LIMIT 5)\n"
        "\t\tORDER BY ctid ASC;"
    )

    # Do the changes below to use tuples in the query.
    # "\n" -> ","
    # "(" -> "'("
    # ")" -> ")'"
    tuples_query = tuples.replace("\n", ",")
    tuples_query = tuples_query.replace("(", "'(")
    tuples_query = tuples_query.replace(")", ")'")

    node.safe_sql(
        "DELETE FROM corruption_test\n" f"\t\tWHERE ctid in ({tuples_query});"
    )

    # Overwrite visibility map with the old one.
    node.stop()
    shutil.move(vm_file + "_temp", vm_file)
    node.start()

    result = node.safe_sql(
        "SELECT DISTINCT t_ctid\n"
        "\t\tFROM pg_check_visible('corruption_test')\n"
        "\t\tORDER BY t_ctid ASC;"
    )
    assert result == tuples, "pg_check_visible must report tuples as corrupted"

    result = node.safe_sql(
        "SELECT DISTINCT t_ctid\n"
        "\t\tFROM pg_check_frozen('corruption_test')\n"
        "\t\tORDER BY t_ctid ASC;"
    )
    assert result == tuples, "pg_check_frozen must report tuples as corrupted"

    node.stop()
