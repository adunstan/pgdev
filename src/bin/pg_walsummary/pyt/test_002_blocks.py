# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests that pg_walsummary reports the blocks recorded in WAL summaries."""

import os
import re


def test_pg_walsummary_blocks(pg_bin, create_pg):
    """Generate WAL summaries and verify pg_walsummary reports modified blocks."""
    # Set up a new database instance.  This test exercises neither archiving
    # nor streaming; it only requires WAL summarization, which needs
    # wal_level >= replica (the default).
    node1 = create_pg("node1", start=False)
    node1.append_conf("wal_level = replica")
    node1.append_conf("summarize_wal = on")
    node1.start()

    # Create a table and insert a few test rows into it. VACUUM FREEZE it so
    # that autovacuum doesn't induce any future modifications unexpectedly.
    # Then trigger a checkpoint.
    #
    # Note: unlike psql, the in-process libpq session runs a multi-statement
    # string inside a single implicit transaction, and VACUUM cannot run in a
    # transaction block.  Issue VACUUM FREEZE as a separate command.
    node1.safe_sql(
        """
        CREATE TABLE mytable (a int, b text);
        INSERT INTO mytable
        SELECT
            g, random()::text||random()::text||random()::text||random()::text
        FROM
            generate_series(1, 400) g;
        """
    )
    node1.safe_sql("VACUUM FREEZE")

    # Record the current WAL insert LSN.
    base_lsn = node1.safe_sql("SELECT pg_current_wal_insert_lsn()")

    # Now perform a CHECKPOINT.
    node1.safe_sql("CHECKPOINT")

    # Wait for a new summary to show up, one that includes the inserts we just
    # did.
    result = node1.poll_query_until(
        f"""
        SELECT EXISTS (
            SELECT * from pg_available_wal_summaries()
            WHERE end_lsn >= '{base_lsn}'
        )
        """
    )
    assert result, "WAL summarization caught up after insert"

    # The WAL summarizer should have generated some IO statistics.
    assert node1.poll_query_until(
        "SELECT sum(reads) > 0 FROM pg_stat_io "
        "WHERE backend_type = 'walsummarizer' AND object = 'wal'"
    ), (
        "Timed out while waiting for WAL summarizer to generate statistics "
        "for WAL reads"
    )

    # Find the highest LSN that is summarized on disk.
    summarized_lsn = node1.safe_sql(
        "SELECT MAX(end_lsn) AS summarized_lsn FROM pg_available_wal_summaries()"
    )

    # Update a row in the first block of the table and trigger a checkpoint.
    node1.safe_sql(
        "UPDATE mytable SET b = 'abcdefghijklmnopqrstuvwxyz' || b "
        "|| '01234567890' WHERE a = 2"
    )
    node1.safe_sql("CHECKPOINT")

    # Wait for a new summary to show up.
    result = node1.poll_query_until(
        f"""
        SELECT EXISTS (
            SELECT * from pg_available_wal_summaries()
            WHERE end_lsn > '{summarized_lsn}'
        )
        """
    )
    assert result, "got new WAL summary after update"

    # Figure out the exact details for the new summary file.
    details = node1.safe_sql(
        f"""
        SELECT tli, start_lsn, end_lsn from pg_available_wal_summaries()
            WHERE end_lsn > '{summarized_lsn}'
        """
    )
    lines = details.split("\n")
    assert len(lines) == 1, "got exactly one new WAL summary"
    tli, start_lsn, end_lsn = lines[0].split("|")

    # Reconstruct the full pathname for the WAL summary file.  The backend
    # names these files "%08X%08X%08X%08X%08X.summary" from the TLI and the
    # high/low halves of the start and end LSNs (see OpenWalSummaryFile).
    start_hi, start_lo = start_lsn.split("/")
    end_hi, end_lo = end_lsn.split("/")
    basename = "{:08X}{:08X}{:08X}{:08X}{:08X}.summary".format(
        int(tli),
        int(start_hi, 16),
        int(start_lo, 16),
        int(end_hi, 16),
        int(end_lo, 16),
    )
    filename = os.path.join(node1.data_dir, "pg_wal", "summaries", basename)
    assert os.path.isfile(filename), "WAL summary file exists"

    # Run pg_walsummary on it. We expect exactly two blocks to be modified,
    # block 0 and one other.
    res = pg_bin.result(["pg_walsummary", "-i", filename])
    stdout = res.stdout
    stderr = res.stderr
    lines = stdout.rstrip("\n").split("\n")
    assert re.search(r"FORK main: block 0$", stdout, re.MULTILINE), (
        "stdout shows block 0 modified"
    )
    assert stderr == "", "stderr is empty"
    assert len(lines) == 2, "UPDATE modified 2 blocks"
