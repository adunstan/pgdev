# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test using a standby server as the source.

This sets up three nodes: A, B and C. First, A is the primary,
B follows A, and C follows B:

    A (primary) <--- B (standby) <--- C (standby)

Then we promote C, and insert some divergent rows in A and C:

    A (primary) <--- B (standby)      C (primary)

Finally, we run pg_rewind on C, to re-point it at B again:

    A (primary) <--- B (standby) <--- C (standby)

The test is similar to the basic tests, but since we're dealing with
three nodes, not two, we cannot use most of the RewindTest methods as is.
"""

import os
import shutil

from pypg.command import PgBin


def check_query(node, query, expected, test_name):
    """Run *query* against *node* and assert its text output matches.

    Reproduces psql -At output: each row's columns joined by '|', rows
    joined by newlines, with a trailing newline.
    """
    result = node.sql(query)
    lines = []
    for row in result.rows:
        lines.append("|".join("" if v is None else str(v) for v in row))
    stdout = "".join(line + "\n" for line in lines)
    assert stdout == expected, (
        f"{test_name}: query result matches\n"
        f"got:\n{stdout!r}\nexpected:\n{expected!r}"
    )


def test_007_standby_source(create_pg, bindir):
    pg_bin = PgBin(bindir)

    # Set up node A, as primary
    #
    # A (primary)
    node_a = create_pg("node_a", start=False, allows_streaming=True)
    node_a.append_conf("\nwal_keep_size = 320MB\nallow_in_place_tablespaces = on\n")
    node_a.start()

    # Create a test table and insert a row in primary.
    node_a.safe_sql("CREATE TABLE tbl1 (d text)")
    node_a.safe_sql("INSERT INTO tbl1 VALUES ('in A')")
    node_a.safe_sql("CHECKPOINT")

    # Set up node B and C, as cascaded standbys
    #
    # A (primary) <--- B (standby) <--- C (standby)
    node_a.backup("my_backup")
    node_b = create_pg("node_b", start=False)
    node_b.init_from_backup(node_a, "my_backup", has_streaming=True)
    node_b.set_standby_mode()
    node_b.start()

    node_b.backup("my_backup")
    node_c = create_pg("node_c", start=False)
    node_c.init_from_backup(node_b, "my_backup", has_streaming=True)
    node_c.set_standby_mode()
    node_c.start()

    # Insert additional data on A, and wait for both standbys to catch up.
    node_a.safe_sql("INSERT INTO tbl1 values ('in A, before promotion')")
    node_a.safe_sql("CHECKPOINT")

    lsn = node_a.lsn("write")
    node_a.wait_for_catchup("node_b", "write", lsn)
    node_b.wait_for_catchup("node_c", "write", lsn)

    # Promote C
    #
    # A (primary) <--- B (standby)      C (primary)
    node_c.promote()

    # Insert a row in A. This causes A/B and C to have "diverged", so that
    # it's no longer possible to just apply the standby's logs over primary
    # directory - you need to rewind.
    node_a.safe_sql("INSERT INTO tbl1 VALUES ('in A, after C was promoted')")

    # make sure it's replicated to B before we continue
    node_a.wait_for_catchup("node_b")

    # Also insert a new row in the standby, which won't be present in the
    # old primary.
    node_c.safe_sql("INSERT INTO tbl1 VALUES ('in C, after C was promoted')")

    #
    # All set up. We're ready to run pg_rewind.
    #
    node_c_pgdata = node_c.data_dir

    # Stop the node and be ready to perform the rewind.
    node_c.stop("fast")

    # Keep a temporary postgresql.conf or it would be overwritten during the
    # rewind.
    tmp_folder = os.path.join(node_c.basedir, "rewind_tmp")
    os.makedirs(tmp_folder, exist_ok=True)
    saved_conf = os.path.join(tmp_folder, "node_c-postgresql.conf.tmp")
    shutil.copy(os.path.join(node_c_pgdata, "postgresql.conf"), saved_conf)

    # Temporarily unset PGAPPNAME so that the server doesn't inherit it.
    # Otherwise this could affect libpqwalreceiver connections in confusing
    # ways.
    #
    # Do rewind using a remote connection as source, generating recovery
    # configuration automatically.
    pg_bin.command_ok(
        [
            "pg_rewind",
            "--debug",
            "--source-server",
            node_b.connstr("postgres"),
            "--target-pgdata",
            node_c_pgdata,
            "--no-sync",
            "--write-recovery-conf",
        ],
        "pg_rewind remote",
        extra_env={"PGAPPNAME": None},
    )

    # Now move back postgresql.conf with old settings.
    shutil.move(saved_conf, os.path.join(node_c_pgdata, "postgresql.conf"))

    # Restart the node.
    node_c.start()

    # Run some checks to verify that C has been successfully rewound, and
    # connected back to follow B.
    check_query(
        node_c,
        "SELECT * FROM tbl1",
        "in A\nin A, before promotion\nin A, after C was promoted\n",
        "table content after rewind",
    )

    # Insert another row, and observe that it's cascaded from A to B to C.
    node_a.safe_sql("INSERT INTO tbl1 values ('in A, after rewind')")

    node_b.wait_for_replay_catchup("node_c", node_a)

    check_query(
        node_c,
        "SELECT * FROM tbl1",
        "in A\n"
        "in A, before promotion\n"
        "in A, after C was promoted\n"
        "in A, after rewind\n",
        "table content after rewind and insert",
    )

    # clean up
    node_a.teardown()
    node_b.teardown()
    node_c.teardown()
