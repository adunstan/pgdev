# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Tests for MultiXact SLRU conversion during upgrade."""

# Version 19 expanded MultiXactOffset from 32 to 64 bits.  Upgrading
# across that requires rewriting the SLRU files to the new format.
# This file contains tests for the conversion.
#
# A pre-v19 installation could be pointed at via the
# 'oldinstall' ENV variable.  This pytest framework always builds a single
# installation, so the old cluster is the same version as the new one.  That
# still performs a very basic test, upgrading a cluster with some multixacts.
# It's not very interesting, however, because there's no conversion involved
# in that case.  The wraparound scenario, which relies on the pre-v19 file
# format, is skipped when the old version is v19 or above (always, here).

import glob
import os
import re

from pypg.util import enable_localhost_tcp


# A workload that consumes multixids.  The purpose of this is to
# generate some multixids in the old cluster, so that we can test
# upgrading them.  The workload is a mix of KEY SHARE locking queries
# and UPDATEs, and commits and aborts, to generate a mix of multixids
# with different statuses.  It consumes around 3000 multixids with
# 60000 members in total.  That's enough to span more than one
# multixids 'offsets' page, and more than one 'members' segment with
# the default block size.
#
# The workload leaves behind a table called 'mxofftest' containing a
# small number of rows referencing some of the generated multixids.
def mxact_workload(node):
    node.start()
    node.safe_sql(
        """
        CREATE TABLE mxofftest (id INT PRIMARY KEY, n_updated INT)
          WITH (AUTOVACUUM_ENABLED=FALSE);
        INSERT INTO mxofftest SELECT G, 0 FROM GENERATE_SERIES(1, 50) G;
        """
    )

    nclients = 20
    update_every = 13
    abort_every = 11
    connections = []

    # Silence the logging of the statements we run to avoid unnecessarily
    # bloating the test logs.  This runs before the upgrade we're testing, so
    # the details should not be very interesting for debugging.  But if needed,
    # you can make it more verbose by setting this.
    verbose = False

    # Open multiple connections to the database.  Start a transaction in each
    # connection.
    for _ in range(0, nclients + 1):
        conn = node.connect("postgres")
        if not verbose:
            conn.do("SET log_statement=none")
        conn.do("SET enable_seqscan=off")
        conn.do("BEGIN")
        connections.append(conn)

    # Run queries using cycling through the connections in a round-robin
    # fashion.  We keep a transaction open in each connection at all times, and
    # lock/update the rows.  With 20 connections, each SELECT FOR KEY SHARE
    # query generates a new multixid, containing the XIDs of all the
    # transactions running at the time, ie. around 20 XIDs.
    for i in range(0, 3000):
        if i % 100 == 0:
            print(f"# generating multixids {i} / 3000")

        conn = connections[i % nclients]

        conn.do("ABORT" if i % abort_every == 0 else "COMMIT")

        conn.do("BEGIN")
        if i % update_every == 0:
            sql = (
                f"UPDATE mxofftest SET n_updated = n_updated + 1 "
                f"WHERE id = {i} % 50;"
            )
        else:
            threshold = int(i / 3000 * 50)
            sql = (
                "select count(*) from ("
                f"  SELECT * FROM mxofftest WHERE id >= {threshold} FOR KEY SHARE"
                ") as x"
            )
        conn.do(sql)

    for conn in connections:
        conn.close()

    node.stop()


# Return contents of the 'mxofftest' table, created by mxact_workload
def get_test_table_contents(node):
    return node.safe_sql("SELECT ctid, xmin, xmax, * FROM mxofftest")


# Return the members of all updating multixids in the given range
def get_updating_multixact_members(node, from_, to):
    contents = ""

    if to >= from_:
        contents += node.safe_sql(
            f"""
            SELECT multi, mode, xid
            FROM generate_series({from_}, {to} - 1) as multi,
                 pg_get_multixact_members(multi::text::xid)
            WHERE mode not in ('keysh', 'sh');
            """
        )
    else:
        # Multixids wrapped around.  Split the query into two parts, before and
        # after the wraparound.
        contents += node.safe_sql(
            f"""
            SELECT multi, mode, xid
            FROM generate_series({from_}, 4294967295) as multi,
                 pg_get_multixact_members(multi::text::xid)
            WHERE mode not in ('keysh', 'sh');
            """
        )
        contents += node.safe_sql(
            f"""
            SELECT multi, mode, xid
            FROM generate_series(1, {to} - 1) as multi,
                 pg_get_multixact_members(multi::text::xid)
            WHERE mode not in ('keysh', 'sh');
            """
        )

    return contents


# Read multixid related fields from the control file
def read_multixid_fields(pg_bin, node):
    res = pg_bin.result(["pg_controldata", node.data_dir])
    stdout = res.stdout

    m = re.search(r"^Latest checkpoint's oldestMultiXid:\s*(.*)$", stdout, re.M)
    assert m, "could not read oldestMultiXid from pg_controldata"
    oldest_multi_xid = m.group(1)
    m = re.search(r"^Latest checkpoint's NextMultiXactId:\s*(.*)$", stdout, re.M)
    assert m, "could not read NextMultiXactId from pg_controldata"
    next_multi_xid = m.group(1)
    m = re.search(r"^Latest checkpoint's NextMultiOffset:\s*(.*)$", stdout, re.M)
    assert m, "could not read NextMultiOffset from pg_controldata"
    next_multi_offset = m.group(1)

    return (oldest_multi_xid, next_multi_xid, next_multi_offset)


# Reset a cluster's next multixid and mxoffset to given values.
#
# Note: This is used on the old installation, so the command arguments and the
# output parsing used here must work with all pre-v19 PostgreSQL versions
# supported by the test.
def reset_mxid_mxoffset_pre_v19(pg_bin, node, mxid, mxoffset):
    # Get block size
    res = pg_bin.result(["pg_resetwal", "--dry-run", node.data_dir])
    out = res.stdout
    assert re.search(r"^Database block size: *(\d+)$", out, re.M)

    # Verify that no multixids are currently in use.  Resetting would destroy
    # them.  (A freshly initialized cluster has no multixids.)
    m = re.search(r"^Latest checkpoint's NextMultiXactId: *(\d+)$", out, re.M)
    assert m
    next_mxid = int(m.group(1))
    m = re.search(r"^Latest checkpoint's oldestMultiXid: *(\d+)$", out, re.M)
    assert m
    oldest_mxid = int(m.group(1))
    assert next_mxid == oldest_mxid, "cluster has some multixids in use"

    # Extract a few other values from pg_resetwal --dry-run output that we need
    # for the calculations below
    m = re.search(r"^Database block size: *(\d+)$", out, re.M)
    assert m
    blcksz = int(m.group(1))
    # SLRU_PAGES_PER_SEGMENT is always 32 on pre-19 versions
    slru_pages_per_segment = 32

    # Do the reset
    pg_bin.command_ok(
        [
            "pg_resetwal",
            "--pgdata",
            node.data_dir,
            "--multixact-offset",
            str(mxoffset),
            "--multixact-ids",
            f"{mxid},{mxid}",
        ],
        "reset multixids and offset",
    )

    # pg_resetwal just updates the control file.  The cluster will refuse to
    # start up, if the SLRU segments corresponding to the next multixid and
    # offset does not exist.  Create a segments that covers the given values,
    # filled with zeros.  But first remove any old segments.
    for path in glob.glob(os.path.join(node.data_dir, "pg_multixact/offsets/*")):
        os.unlink(path)
    for path in glob.glob(os.path.join(node.data_dir, "pg_multixact/members/*")):
        os.unlink(path)

    bytes_per_seg = slru_pages_per_segment * blcksz

    # Initialize the 'offsets' SLRU file containing the new next multixid with
    # zeros
    #
    # sizeof(MultiXactOffset) == 4 in PostgreSQL versions before 19
    multixact_offsets_per_page = blcksz // 4
    segno = int(mxid / multixact_offsets_per_page / slru_pages_per_segment)
    path = os.path.join(node.data_dir, "pg_multixact/offsets", "%04X" % segno)
    with open(path, "wb") as fh:
        fh.write(b"\0" * bytes_per_seg)

    # Same for the 'members' SLRU
    multixact_members_per_page = (blcksz // 20) * 4
    segno = int(mxoffset / multixact_members_per_page / slru_pages_per_segment)
    path = os.path.join(node.data_dir, "pg_multixact/members", "%04X" % segno)
    with open(path, "wb") as fh:
        fh.write(b"\0" * bytes_per_seg)


# Main test workhorse routine.  Dump data on old version, run pg_upgrade,
# compare data after upgrade.
def upgrade_and_compare(pg_bin, bindir, oldnode, newnode):
    pg_bin.command_ok(
        [
            "pg_upgrade",
            "--no-sync",
            "--old-datadir",
            oldnode.data_dir,
            "--new-datadir",
            newnode.data_dir,
            "--old-bindir",
            bindir,
            "--new-bindir",
            bindir,
            "--socketdir",
            newnode.host,
            "--old-port",
            str(oldnode.port),
            "--new-port",
            str(newnode.port),
        ],
        "run of pg_upgrade for new instance",
    )

    # Dump contents of the test table, and the status of all updating multixids
    # from the old cluster.  (Locking-only multixids don't need to be preserved
    # so we ignore those)
    #
    # Note: we do this *after* running pg_upgrade, to ensure that we don't set
    # all the hint bits before upgrade by doing the SELECT on the table.
    multixids_start, multixids_end, _ = read_multixid_fields(pg_bin, oldnode)
    oldnode.start()
    old_table_contents = get_test_table_contents(oldnode)
    old_multixacts = get_updating_multixact_members(
        oldnode, int(multixids_start), int(multixids_end)
    )
    oldnode.stop()

    # Compare them with the upgraded cluster
    newnode.start()
    new_table_contents = get_test_table_contents(newnode)
    new_multixacts = get_updating_multixact_members(
        newnode, int(multixids_start), int(multixids_end)
    )
    newnode.stop()

    assert (
        old_table_contents == new_table_contents
    ), "test table contents from original and upgraded clusters match"
    assert (
        old_multixacts == new_multixacts
    ), "multixact members from original and upgraded clusters match"


def _old_version(pg_bin):
    """Return the major version number of the (old) installation, e.g. 19."""
    res = pg_bin.result(["pg_config", "--version"])
    m = re.search(r"(\d+)", res.stdout)
    assert m, "could not determine PostgreSQL version"
    return int(m.group(1))


def test_007_multixact_conversion(pg_bin, create_pg, bindir, tmp_path):
    # In a VPATH build, we'll be started in the source directory, but we want
    # to run pg_upgrade in the build directory so that any files generated
    # finish in it, like delete_old_cluster.{sh,bat}.  Use the test's tmp_path
    # as a clean cwd since pg_upgrade writes logs there.
    os.chdir(tmp_path)

    old_version = _old_version(pg_bin)

    # Basic scenario: Create a cluster using old installation, run
    # multixid-creating workload on it, then upgrade.
    #
    # This works even if the old and new version is the same, although it's not
    # very interesting as the conversion routines only run when upgrading from a
    # pre-v19 cluster.
    old = create_pg("basic_oldnode", start=False, initdb_extra=["-k"])
    new = create_pg("basic_newnode", start=False)
    # pg_upgrade connects to the clusters over localhost TCP on Windows.
    enable_localhost_tcp(old)
    enable_localhost_tcp(new)

    print(f"# old installation is version {old_version}")

    # Run the workload
    _, start_mxid, start_mxoff = read_multixid_fields(pg_bin, old)
    mxact_workload(old)
    _, finish_mxid, finish_mxoff = read_multixid_fields(pg_bin, old)

    print(
        "# Testing upgrade, basic scenario\n"
        f"#  mxid from {start_mxid} to {finish_mxid}\n"
        f"#  oldnode mxoff from {start_mxoff} to {finish_mxoff}"
    )

    upgrade_and_compare(pg_bin, bindir, old, new)

    # Wraparound scenario: This is the same as the basic scenario, but the old
    # cluster goes through multixid and offset wraparound.
    #
    # This requires the old installation to be version 18 or older, because the
    # hacks we use to reset the old cluster to a state just before the
    # wraparound rely on the pre-v19 file format.  If the old cluster is of v19
    # or above, multixact SLRU conversion is not needed anyway.
    if old_version >= 19:
        # skipping mxoffset conversion tests because upgrading from the old
        # version does not require conversion
        return

    old = create_pg("wraparound_oldnode", start=False, initdb_extra=["-k"])
    new = create_pg("wraparound_newnode", start=False)
    # pg_upgrade connects to the clusters over localhost TCP on Windows.
    enable_localhost_tcp(old)
    enable_localhost_tcp(new)

    # Reset the old cluster to just before multixid and 32-bit offset
    # wraparound.
    reset_mxid_mxoffset_pre_v19(pg_bin, old, 0xFFFFFA00, 0xFFFFEC00)

    # Run the workload.  This crosses multixid and offset wraparound.
    _, start_mxid, start_mxoff = read_multixid_fields(pg_bin, old)
    mxact_workload(old)
    _, finish_mxid, finish_mxoff = read_multixid_fields(pg_bin, old)

    print(
        "# Testing upgrade, wraparound scenario\n"
        f"#  mxid from {start_mxid} to {finish_mxid}\n"
        f"#  oldnode mxoff from {start_mxoff} to {finish_mxoff}"
    )

    # Verify that wraparounds happened.
    assert int(finish_mxid) < int(start_mxid), "multixid wrapped around in old cluster"
    assert int(finish_mxoff) < int(start_mxoff), "mxoff wrapped around in old cluster"

    upgrade_and_compare(pg_bin, bindir, old, new)
