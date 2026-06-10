# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Tests for pg_waldump --save-fullpage, verifying saved full-page images."""

import glob
import os
import re
import struct


# This regexp will match filenames formatted as:
# TLI-LSNh-LSNl.TBLSPCOID.DBOID.NODEOID.dd_fork with the components being:
# - Timeline ID in hex format.
# - WAL LSN in hex format, as two 8-character numbers.
# - Tablespace OID (0 for global).
# - Database OID.
# - Relfilenode.
# - Block number.
# - Fork this block came from (vm, init, fsm, or main).
_FILE_RE = re.compile(
    r"^[0-9A-F]{8}-([0-9A-F]{8})-([0-9A-F]{8})"
    r"[.][0-9]+[.][0-9]+[.][0-9]+[.][0-9]+(?:_vm|_init|_fsm|_main)?$"
)


def _get_block_lsn(path, blocksize):
    """Extract the LSN from the given block structure."""
    with open(path, "rb") as fh:
        block = fh.read(blocksize)
        assert len(block) == blocksize, "could not read block"
        # unpack('LL', ...): two native unsigned longs.  The page LSN is stored
        # as two 32-bit halves (high, low).
        lsn_hi, lsn_lo = struct.unpack("=LL", block[:8])

    return ("%08X" % lsn_hi, "%08X" % lsn_lo)


def test_pg_waldump_save_fullpage(create_pg):
    """Verify pg_waldump --save-fullpage emits well-formed block files."""
    node = create_pg("main", start=False)
    node.append_conf(
        """
wal_level = 'replica'
max_wal_senders = 4
"""
    )
    node.start()

    # Generate data/WAL to examine that will have full pages in them.
    #
    # Note: the in-process libpq session runs a multi-statement string as a
    # single implicit transaction, and CHECKPOINT cannot run in a transaction
    # block.  Issue the statements separately.
    node.safe_sql(
        "SELECT 'init' FROM pg_create_physical_replication_slot("
        "'regress_pg_waldump_slot', true, false)"
    )
    node.safe_sql("CREATE TABLE test_table AS SELECT generate_series(1,100) a")
    # Force FPWs on the next writes.
    node.safe_sql("CHECKPOINT")
    node.safe_sql("UPDATE test_table SET a = a + 1")

    walfile_name, blocksize = node.safe_sql(
        "SELECT pg_walfile_name(pg_switch_wal()), current_setting('block_size')"
    ).split("|")
    blocksize = int(blocksize)

    # Get the relation node, etc for the new table
    relation = node.safe_sql(
        """SELECT format(
        '%s/%s/%s',
        CASE WHEN reltablespace = 0 THEN dattablespace ELSE reltablespace END,
        pg_database.oid,
        pg_relation_filenode(pg_class.oid))
    FROM pg_class, pg_database
    WHERE relname = 'test_table' AND
        datname = current_database()"""
    )

    # Forward slashes: pg_waldump splits a WAL path on "/" to find the
    # directory, so a Windows backslash path would not be located.
    walfile = "/".join([node.data_dir.replace("\\", "/"), "pg_wal", walfile_name])
    tmp_folder = str(node.basedir)
    raw_dir = os.path.join(tmp_folder, "raw")

    assert os.path.isfile(walfile), "Got a WAL file"

    node.command_ok(
        [
            "pg_waldump",
            "--quiet",
            "--save-fullpage", raw_dir,
            "--relation", relation,
            walfile,
        ],
        "pg_waldump with --save-fullpage runs",
    )

    file_count = 0

    # Verify filename format matches --save-fullpage.
    for fullpath in glob.glob(os.path.join(raw_dir, "*")):
        filename = os.path.basename(fullpath)

        m = _FILE_RE.match(filename)
        assert m is not None, f"verify filename format for file {filename}"
        file_count += 1

        hi_lsn_fn, lo_lsn_fn = m.group(1), m.group(2)
        hi_lsn_bk, lo_lsn_bk = _get_block_lsn(fullpath, blocksize)

        # The LSN on the block comes before the file's LSN.
        assert (hi_lsn_fn + lo_lsn_fn) >= (hi_lsn_bk + lo_lsn_bk), (
            f"LSN stored in the file {hi_lsn_fn}/{lo_lsn_fn} precedes the one "
            f"stored in the block {hi_lsn_bk}/{lo_lsn_bk}"
        )

    assert file_count > 0, "verify that at least one block has been saved"
