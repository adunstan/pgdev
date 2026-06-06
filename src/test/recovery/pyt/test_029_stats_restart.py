# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests statistics handling around restarts, including handling of crashes and
invalid stats files, as well as restoring stats after "normal" restarts.
"""

import os
import shutil

CONNECT_DB = "postgres"
DB_UNDER_TEST = "test"


def _trigger_funcrel_stat(node):
    node.safe_sql(
        """
        SELECT * FROM tab_stats_crash_discard_test1;
        SELECT func_stats_crash_discard1();
        SELECT pg_stat_force_next_flush();""",
        DB_UNDER_TEST,
    )


def _have_stats(node, kind, dboid, objid):
    return node.safe_sql(
        f"SELECT pg_stat_have_stats('{kind}', {dboid}, {objid})", CONNECT_DB
    )


def _overwrite_file(filename, text):
    with open(filename, "w", encoding="utf-8") as fh:
        fh.write(text)


def _append_file(filename, text):
    with open(filename, "a", encoding="utf-8") as fh:
        fh.write(text)


def _checkpoint_stats(node):
    results = {}
    results["count"] = node.safe_sql(
        "SELECT num_timed + num_requested FROM pg_stat_checkpointer", CONNECT_DB
    )
    results["reset"] = node.safe_sql(
        "SELECT stats_reset FROM pg_stat_checkpointer", CONNECT_DB
    )
    return results


def _wal_stats(node):
    results = {}
    results["records"] = node.safe_sql(
        "SELECT wal_records FROM pg_stat_wal", CONNECT_DB
    )
    results["bytes"] = node.safe_sql("SELECT wal_bytes FROM pg_stat_wal", CONNECT_DB)
    results["reset"] = node.safe_sql(
        "SELECT stats_reset FROM pg_stat_wal", CONNECT_DB
    )
    return results


def _io_stats(node, context, obj, backend_type):
    results = {}
    results["writes"] = node.safe_sql(
        f"""SELECT writes FROM pg_stat_io
  WHERE context = '{context}' AND object = '{obj}' AND
    backend_type = '{backend_type}'""",
        CONNECT_DB,
    )
    results["reads"] = node.safe_sql(
        f"""SELECT reads FROM pg_stat_io
  WHERE context = '{context}' AND object = '{obj}' AND
    backend_type = '{backend_type}'""",
        CONNECT_DB,
    )
    return results


def test_029_stats_restart(create_pg, tmp_path):
    node = create_pg("primary", start=False, allows_streaming=True)
    node.append_conf("track_functions = 'all'")
    node.start()

    sect = "startup"

    # Check some WAL statistics after a fresh startup.  The startup process
    # should have done WAL reads, and initialization some WAL writes.
    standalone_io_stats = _io_stats(node, "init", "wal", "standalone backend")
    startup_io_stats = _io_stats(node, "normal", "wal", "startup")
    assert int("0") < int(standalone_io_stats["writes"]), \
        f"{sect}: increased standalone backend IO writes"
    assert int("0") < int(startup_io_stats["reads"]), \
        f"{sect}: increased startup IO reads"

    # create test objects
    node.safe_sql(f"CREATE DATABASE {DB_UNDER_TEST}", CONNECT_DB)
    node.safe_sql(
        "CREATE TABLE tab_stats_crash_discard_test1 AS "
        "SELECT generate_series(1,100) AS a",
        DB_UNDER_TEST,
    )
    node.safe_sql(
        "CREATE FUNCTION func_stats_crash_discard1() RETURNS VOID AS "
        "'select 2;' LANGUAGE SQL IMMUTABLE",
        DB_UNDER_TEST,
    )

    # collect object oids
    dboid = node.safe_sql(
        f"SELECT oid FROM pg_database WHERE datname = '{DB_UNDER_TEST}'",
        DB_UNDER_TEST,
    )
    funcoid = node.safe_sql(
        "SELECT 'func_stats_crash_discard1()'::regprocedure::oid", DB_UNDER_TEST
    )
    tableoid = node.safe_sql(
        "SELECT 'tab_stats_crash_discard_test1'::regclass::oid", DB_UNDER_TEST
    )

    # generate stats and flush them
    _trigger_funcrel_stat(node)

    # verify stats objects exist
    sect = "initial"
    assert _have_stats(node, "database", dboid, 0) == "t", \
        f"{sect}: db stats do exist"
    assert _have_stats(node, "function", dboid, funcoid) == "t", \
        f"{sect}: function stats do exist"
    assert _have_stats(node, "relation", dboid, tableoid) == "t", \
        f"{sect}: relation stats do exist"

    # regular shutdown
    node.stop()

    # backup stats files
    statsfile = os.path.join(str(tmp_path), "discard_stats1")
    assert not os.path.isfile(statsfile), "backup statsfile cannot already exist"

    datadir = node.data_dir
    og_stats = os.path.join(datadir, "pg_stat", "pgstat.stat")
    assert os.path.isfile(og_stats), "origin stats file must exist"
    shutil.copy(og_stats, statsfile)

    # test discarding of stats file after crash etc

    node.start()

    sect = "copy"
    assert _have_stats(node, "database", dboid, 0) == "t", \
        f"{sect}: db stats do exist"
    assert _have_stats(node, "function", dboid, funcoid) == "t", \
        f"{sect}: function stats do exist"
    assert _have_stats(node, "relation", dboid, tableoid) == "t", \
        f"{sect}: relation stats do exist"

    node.stop("immediate")

    assert not os.path.isfile(og_stats), \
        "no stats file should exist after immediate shutdown"

    # copy the old stats back to test we discard stats after crash restart
    shutil.copy(statsfile, og_stats)

    node.start()

    # stats should have been discarded
    sect = "post immediate"
    assert _have_stats(node, "database", dboid, 0) == "f", \
        f"{sect}: db stats do not exist"
    assert _have_stats(node, "function", dboid, funcoid) == "f", \
        f"{sect}: function stats do exist"
    assert _have_stats(node, "relation", dboid, tableoid) == "f", \
        f"{sect}: relation stats do not exist"

    # get rid of backup statsfile
    os.unlink(statsfile)

    # generate new stats and flush them
    _trigger_funcrel_stat(node)

    sect = "post immediate, new"
    assert _have_stats(node, "database", dboid, 0) == "t", \
        f"{sect}: db stats do exist"
    assert _have_stats(node, "function", dboid, funcoid) == "t", \
        f"{sect}: function stats do exist"
    assert _have_stats(node, "relation", dboid, tableoid) == "t", \
        f"{sect}: relation stats do exist"

    # regular shutdown
    node.stop()

    # check an invalid stats file is handled

    _overwrite_file(og_stats, "ZZZZZZZZZZZZZ")

    # normal startup and no issues despite invalid stats file
    node.start()

    # no stats present due to invalid stats file
    sect = "invalid_overwrite"
    assert _have_stats(node, "database", dboid, 0) == "f", \
        f"{sect}: db stats do not exist"
    assert _have_stats(node, "function", dboid, funcoid) == "f", \
        f"{sect}: function stats do not exist"
    assert _have_stats(node, "relation", dboid, tableoid) == "f", \
        f"{sect}: relation stats do not exist"

    # check invalid stats file starting with valid contents, but followed by
    # invalid content is handled.

    _trigger_funcrel_stat(node)
    node.stop()
    _append_file(og_stats, "XYZ")
    node.start()

    sect = "invalid_append"
    assert _have_stats(node, "database", dboid, 0) == "f", \
        f"{sect}: db stats do not exist"
    assert _have_stats(node, "function", dboid, funcoid) == "f", \
        f"{sect}: function stats do not exist"
    assert _have_stats(node, "relation", dboid, tableoid) == "f", \
        f"{sect}: relation stats do not exist"

    # checks related to stats persistency around restarts and resets

    # Ensure enough checkpoints to protect against races for test after reset,
    # even on very slow machines.
    node.safe_sql("CHECKPOINT; CHECKPOINT;", CONNECT_DB)

    # check checkpoint and wal stats are incremented due to restart

    ckpt_start = _checkpoint_stats(node)
    wal_start = _wal_stats(node)
    node.restart()

    sect = "post restart"
    ckpt_restart = _checkpoint_stats(node)
    wal_restart = _wal_stats(node)

    assert int(ckpt_start["count"]) < int(ckpt_restart["count"]), \
        f"{sect}: increased checkpoint count"
    assert int(wal_start["records"]) < int(wal_restart["records"]), \
        f"{sect}: increased wal record count"
    assert int(wal_start["bytes"]) < int(wal_restart["bytes"]), \
        f"{sect}: increased wal bytes"
    assert ckpt_start["reset"] == ckpt_restart["reset"], \
        f"{sect}: checkpoint stats_reset equal"
    assert wal_start["reset"] == wal_restart["reset"], \
        f"{sect}: wal stats_reset equal"

    # Check that checkpoint stats are reset, WAL stats aren't affected

    node.safe_sql("SELECT pg_stat_reset_shared('checkpointer')", CONNECT_DB)

    sect = "post ckpt reset"
    ckpt_reset = _checkpoint_stats(node)
    wal_ckpt_reset = _wal_stats(node)

    assert int(ckpt_restart["count"]) > int(ckpt_reset["count"]), \
        f"{sect}: checkpoint count smaller"
    assert ckpt_start["reset"] < ckpt_reset["reset"], \
        f"{sect}: stats_reset newer"

    assert int(wal_restart["records"]) <= int(wal_ckpt_reset["records"]), \
        f"{sect}: wal record count not affected by reset"
    assert wal_start["reset"] == wal_ckpt_reset["reset"], \
        f"{sect}: wal stats_reset equal"

    # check that checkpoint stats stay reset after restart

    node.restart()

    sect = "post ckpt reset & restart"
    ckpt_restart_reset = _checkpoint_stats(node)
    wal_restart2 = _wal_stats(node)

    # made sure above there's enough checkpoints that this will be stable even
    # on slow machines
    assert int(ckpt_restart_reset["count"]) < int(ckpt_restart["count"]), \
        f"{sect}: checkpoint still reset"
    assert ckpt_restart_reset["reset"] == ckpt_reset["reset"], \
        f"{sect}: stats_reset same"

    assert int(wal_ckpt_reset["records"]) < int(wal_restart2["records"]), \
        f"{sect}: increased wal record count"
    assert int(wal_ckpt_reset["bytes"]) < int(wal_restart2["bytes"]), \
        f"{sect}: increased wal bytes"
    assert wal_start["reset"] == wal_restart2["reset"], \
        f"{sect}: wal stats_reset equal"

    # check WAL stats stay reset

    node.safe_sql("SELECT pg_stat_reset_shared('wal')", CONNECT_DB)

    sect = "post wal reset"
    wal_reset = _wal_stats(node)

    assert int(wal_reset["records"]) < int(wal_restart2["records"]), \
        f"{sect}: smaller record count"
    assert int(wal_reset["bytes"]) < int(wal_restart2["bytes"]), \
        f"{sect}: smaller bytes"
    assert wal_reset["reset"] > wal_restart2["reset"], \
        f"{sect}: newer stats_reset"

    node.restart()

    sect = "post wal reset & restart"
    wal_reset_restart = _wal_stats(node)

    # enough WAL generated during prior tests and initdb to make this not racy
    assert int(wal_reset_restart["records"]) < int(wal_restart2["records"]), \
        f"{sect}: smaller record count"
    assert int(wal_reset["bytes"]) < int(wal_restart2["bytes"]), \
        f"{sect}: smaller bytes"
    assert wal_reset["reset"] > wal_restart2["reset"], \
        f"{sect}: newer stats_reset"

    node.stop("immediate")
    node.start()

    sect = "post immediate restart"
    wal_restart_immediate = _wal_stats(node)

    assert wal_reset_restart["reset"] < wal_restart_immediate["reset"], \
        f"{sect}: reset timestamp is new"

    node.stop()
