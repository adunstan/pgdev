# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Simple tablespace tests.

These can't be replicated on the same host due to the use of absolute paths,
so we keep them out of the regular regression tests.
"""

import os


def test_002_tablespace(pg):
    node = pg

    # Create a couple of directories to use as tablespaces.
    ts1_location = os.path.join(node.basedir, "ts1")
    os.mkdir(ts1_location)
    ts2_location = os.path.join(node.basedir, "ts2")
    os.mkdir(ts2_location)

    # Create a tablespace with an absolute path.  CREATE TABLESPACE cannot run
    # in a transaction block, so each runs as its own safe_sql call.
    node.safe_sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1_location}'")

    # Can't create a tablespace where there is one already.
    res = node.sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1_location}'")
    assert res.error_message is not None, "clobber tablespace with absolute path"

    # Create table in it.
    node.safe_sql("CREATE TABLE t () TABLESPACE regress_ts1")

    # Can't drop a tablespace that still has a table in it.
    res = node.sql("DROP TABLESPACE regress_ts1")
    assert res.error_message is not None, "drop tablespace with absolute path"

    # Drop the table.
    node.safe_sql("DROP TABLE t")

    # Drop the tablespace.
    node.safe_sql("DROP TABLESPACE regress_ts1")

    # Create two absolute tablespaces and two in-place tablespaces, so we can
    # test various kinds of tablespace moves.
    node.safe_sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1_location}'")
    node.safe_sql(f"CREATE TABLESPACE regress_ts2 LOCATION '{ts2_location}'")

    # In-place tablespaces require allow_in_place_tablespaces.  The GUC must be
    # set in the same session as the CREATE TABLESPACE, but CREATE TABLESPACE
    # cannot run in a transaction block (and a multi-statement string runs as
    # one implicit transaction), so issue them as separate statements on one
    # persistent connection.
    with node.connect() as sess:
        sess.query_safe("SET allow_in_place_tablespaces=on")
        sess.query_safe("CREATE TABLESPACE regress_ts3 LOCATION ''")
        sess.query_safe("CREATE TABLESPACE regress_ts4 LOCATION ''")

    # Create a table and test moving between absolute and in-place tablespaces.
    node.safe_sql("CREATE TABLE t () TABLESPACE regress_ts1")
    node.safe_sql("ALTER TABLE t SET tablespace regress_ts2")  # abs->abs
    node.safe_sql("ALTER TABLE t SET tablespace regress_ts3")  # abs->in-place
    node.safe_sql("ALTER TABLE t SET tablespace regress_ts4")  # in-place->in-place
    node.safe_sql("ALTER TABLE t SET tablespace regress_ts1")  # in-place->abs

    # Drop everything.
    node.safe_sql("DROP TABLE t")
    node.safe_sql("DROP TABLESPACE regress_ts1")
    node.safe_sql("DROP TABLESPACE regress_ts2")
    node.safe_sql("DROP TABLESPACE regress_ts3")
    node.safe_sql("DROP TABLESPACE regress_ts4")
