# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_rewind when the target contains WAL beyond minRecoveryPoint."""

#
# Test situation where a target data directory contains
# WAL records beyond both the last checkpoint and the divergence
# point:
#
# Target WAL (TLI 2):
#
# backup ... Checkpoint A ... INSERT 'rewind this'
#            (TLI 1 -> 2)
#
#            ^ last common                        ^ minRecoveryPoint
#              checkpoint
#
# Source WAL (TLI 3):
#
# backup ... Checkpoint A ... Checkpoint B ... INSERT 'keep this'
#            (TLI 1 -> 2)     (TLI 2 -> 3)
#
#
# The last common checkpoint is Checkpoint A. But there is WAL on TLI 2
# after the last common checkpoint that needs to be rewound. We used to
# have a bug where minRecoveryPoint was ignored, and pg_rewind concluded
# that the target doesn't need rewinding in this scenario, because the
# last checkpoint on the target TLI was an ancestor of the source TLI.
#
#
# This test does not make use of RewindTest as it requires three
# nodes.

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


def test_008_min_recovery_point(create_pg, bindir):
    pg_bin = PgBin(bindir)

    node_1 = create_pg("node_1", start=False, allows_streaming=True)
    node_1.append_conf("\nwal_keep_size='100 MB'\n")
    node_1.start()

    # Create a couple of test tables
    node_1.safe_sql("CREATE TABLE public.foo (t TEXT)")
    node_1.safe_sql("CREATE TABLE public.bar (t TEXT)")
    node_1.safe_sql("INSERT INTO public.bar VALUES ('in both')")

    #
    # Create node_2 and node_3 as standbys following node_1
    #
    backup_name = "my_backup"
    node_1.backup(backup_name)

    node_2 = create_pg("node_2", start=False)
    node_2.init_from_backup(node_1, backup_name, has_streaming=True)
    node_2.start()

    node_3 = create_pg("node_3", start=False)
    node_3.init_from_backup(node_1, backup_name, has_streaming=True)
    node_3.start()

    # Wait until node 3 has connected and caught up
    node_1.wait_for_catchup("node_3")

    #
    # Swap the roles of node_1 and node_3, so that node_1 follows node_3.
    #
    node_1.stop("fast")
    node_3.promote()

    # reconfigure node_1 as a standby following node_3
    #
    # This framework's wait_for_catchup only polls pg_stat_replication and
    # disambiguates the
    # streaming connections by application_name, so set a distinct
    # application_name for each standby's connection to node_3.  (Otherwise
    # both node_1 and node_2 would appear as 'walreceiver' and the polling
    # query would match two rows.)
    node_3_connstr = f"host={node_3.host} port={node_3.port}"
    node_1.append_conf(
        f"\nprimary_conninfo='{node_3_connstr} application_name=node_1'\n"
    )
    node_1.set_standby_mode()
    node_1.start()

    # also reconfigure node_2 to follow node_3
    node_2.append_conf(
        f"\nprimary_conninfo='{node_3_connstr} application_name=node_2'\n"
    )
    node_2.restart()

    #
    # Promote node_1, to create a split-brain scenario.
    #

    # make sure node_1 is full caught up with node_3 first
    node_3.wait_for_catchup("node_1")

    node_1.promote()

    #
    # We now have a split-brain with two primaries. Insert a row on both to
    # demonstratively create a split brain. After the rewind, we should only
    # see the insert on 1, as the insert on node 3 is rewound away.
    #
    node_1.safe_sql("INSERT INTO public.foo (t) VALUES ('keep this')")
    # 'bar' is unmodified in node 1, so it won't be overwritten by replaying
    # the WAL from node 1.
    node_3.safe_sql("INSERT INTO public.bar (t) VALUES ('rewind this')")

    # Insert more rows in node 1, to bump up the XID counter. Otherwise, if
    # rewind doesn't correctly rewind the changes made on the other node,
    # we might fail to notice if the inserts are invisible because the XIDs
    # are not marked as committed.
    node_1.safe_sql("INSERT INTO public.foo (t) VALUES ('and this')")
    node_1.safe_sql("INSERT INTO public.foo (t) VALUES ('and this too')")

    # Wait for node 2 to catch up
    node_2.poll_query_until("SELECT COUNT(*) > 1 FROM public.bar", "t")

    # At this point node_2 will shut down without a shutdown checkpoint,
    # but with WAL entries beyond the preceding shutdown checkpoint.
    node_2.stop("fast")
    node_3.stop("fast")

    node_2_pgdata = node_2.data_dir
    node_1_connstr = node_1.connstr("postgres")

    # Keep a temporary postgresql.conf or it would be overwritten during the
    # rewind.
    tmp_folder = os.path.join(node_2.basedir, "rewind_tmp")
    os.makedirs(tmp_folder, exist_ok=True)
    saved_conf = os.path.join(tmp_folder, "node_2-postgresql.conf.tmp")
    shutil.copy(os.path.join(node_2_pgdata, "postgresql.conf"), saved_conf)

    pg_bin.command_ok(
        [
            "pg_rewind",
            "--source-server", node_1_connstr,
            "--target-pgdata", node_2_pgdata,
            "--debug",
        ],
        "run pg_rewind",
    )

    # Now move back postgresql.conf with old settings
    shutil.move(saved_conf, os.path.join(node_2_pgdata, "postgresql.conf"))

    node_2.start()

    # Check contents of the test tables after rewind. The rows inserted in
    # node 3 before rewind should've been overwritten with the data from
    # node 1.
    check_query(
        node_2,
        "SELECT * FROM public.foo",
        "keep this\n"
        "and this\n"
        "and this too\n",
        "table foo after rewind",
    )

    check_query(
        node_2,
        "SELECT * FROM public.bar",
        "in both\n",
        "table bar after rewind",
    )

    # clean up
    node_1.teardown()
    node_2.teardown()
    node_3.teardown()
