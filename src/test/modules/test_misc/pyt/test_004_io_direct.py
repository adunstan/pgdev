# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Very simple exercise of the direct I/O GUC."""

import os
import sys

import pytest


def direct_io_supported(probe_dir):
    """Pre-flight check for direct I/O support.

    We know that macOS has F_NOCACHE, and we know that Windows has
    FILE_FLAG_NO_BUFFERING, and we assume that their typical file systems
    will accept those flags.  For every other system, probe for O_DIRECT
    support on the file system where the test scratch directory lives.

    Returns None if supported, or a skip reason string otherwise.
    """
    if sys.platform == "darwin" or sys.platform == "win32":
        return None

    # Does this Python/platform know about O_DIRECT in <fcntl.h>?
    o_direct = getattr(os, "O_DIRECT", None)
    if o_direct is None:
        return "no O_DIRECT"

    # Can we open a file in O_DIRECT mode in the file system where the test
    # scratch directory lives?
    path = os.path.join(probe_dir, "test_o_direct_file")
    try:
        fd = os.open(path, os.O_RDWR | o_direct | os.O_CREAT, 0o600)
    except OSError as exc:
        return f"pre-flight test if we can open a file with O_DIRECT failed: {exc.strerror}"
    os.close(fd)
    return None


def test_004_io_direct(create_pg, tmp_path):
    skip_reason = direct_io_supported(str(tmp_path))
    if skip_reason is not None:
        pytest.skip(skip_reason)

    node = create_pg("main", start=False)
    node.append_conf(
        "\n".join(
            [
                "debug_io_direct = 'data,wal,wal_init'",
                "shared_buffers = '256kB' # tiny to force I/O",
                "wal_level = replica # minimal runs out of shared_buffers when set so tiny",
                "",
            ]
        )
    )
    node.start()

    # Do some work that is bound to generate shared and local writes and reads
    # as a simple exercise.
    node.safe_sql(
        "create table t1 as select 1 as i from generate_series(1, 10000)"
    )
    node.safe_sql("create table t2count (i int)")
    node.safe_sql(
        "\n".join(
            [
                "begin;",
                "create temporary table t2 as select 1 as i from generate_series(1, 10000);",
                "update t2 set i = i;",
                "insert into t2count select count(*) from t2;",
                "commit;",
            ]
        )
    )
    node.safe_sql("update t1 set i = i")
    assert node.safe_sql("select count(*) from t1") == "10000", \
        "read back from shared"
    assert node.safe_sql("select * from t2count") == "10000", \
        "read back from local"
    node.stop("immediate")

    node.start()
    assert node.safe_sql("select count(*) from t1") == "10000", \
        "read back from shared after crash recovery"
    node.stop()
