# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_receivewal, including compression, replication slots, and
permission handling.
"""

import glob
import os
import stat

from pypg.util import WINDOWS_OS
import subprocess

import pytest

from libpq import Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pg_config_value(pg_config, option):
    return subprocess.run(
        [pg_config, option], stdout=subprocess.PIPE, text=True, check=True
    ).stdout.strip()


def check_pg_config(pg_config, define):
    """Return True if *define* appears in the installed pg_config.h."""
    include_server = _pg_config_value(pg_config, "--includedir-server")
    header = os.path.join(include_server, "pg_config.h")
    with open(header, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if define in line:
                return True
    return False


def _slot(node, slot_name):
    """Return a dict of pg_replication_slots columns for *slot_name*.

    Missing values come back as empty strings.
    """
    fields = [
        "plugin",
        "slot_type",
        "datoid",
        "database",
        "active",
        "active_pid",
        "xmin",
        "catalog_xmin",
        "restart_lsn",
    ]
    row = node.safe_sql(
        "SELECT " + ", ".join(fields) + " FROM pg_catalog.pg_replication_slots "
        f"WHERE slot_name = '{slot_name}'"
    )
    values = row.split("|") if row != "" else [""] * len(fields)
    return dict(zip(fields, values))


def _check_mode_recursive(directory, expected_dir_mode, expected_file_mode):
    """Recursively verify file/dir permission bits under *directory*."""
    for root, dirs, files in os.walk(directory):
        for name in [*dirs, *files]:
            path = os.path.join(root, name)
            expected = expected_dir_mode if os.path.isdir(path) else expected_file_mode
            actual = stat.S_IMODE(os.lstat(path).st_mode)
            if actual != expected:
                print(f"mode of {path} is {actual:#o} but expected {expected:#o}")
                return False
    return True


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_020_pg_receivewal(pg_bin, pg_config, create_pg):
    # Set umask so test directories and files are created with default
    # permissions.
    os.umask(0o077)

    pg_bin.program_help_ok("pg_receivewal")
    pg_bin.program_version_ok("pg_receivewal")
    pg_bin.program_options_handling_ok("pg_receivewal")

    primary = create_pg(
        "primary",
        start=False,
        allows_streaming=True,
        initdb_extra=["--wal-segsize=1"],
    )
    primary.start()

    stream_dir = os.path.join(primary.basedir, "archive_wal")
    os.mkdir(stream_dir)

    # Sanity checks for command line options.
    primary.command_fails(
        ["pg_receivewal"], "pg_receivewal needs target directory specified"
    )
    primary.command_fails(
        [
            "pg_receivewal",
            "--directory",
            stream_dir,
            "--create-slot",
            "--drop-slot",
        ],
        "failure if both --create-slot and --drop-slot specified",
    )
    primary.command_fails(
        ["pg_receivewal", "--directory", stream_dir, "--create-slot"],
        "failure if --create-slot specified without --slot",
    )
    primary.command_fails(
        [
            "pg_receivewal",
            "--directory",
            stream_dir,
            "--synchronous",
            "--no-sync",
        ],
        "failure if --synchronous specified with --no-sync",
    )
    primary.command_fails_like(
        [
            "pg_receivewal",
            "--directory",
            stream_dir,
            "--compress",
            "none:1",
        ],
        r"pg_receivewal: error: invalid compression specification: "
        r'compression algorithm "none" does not accept a compression level',
        "failure if --compress none:N (where N > 0)",
    )

    # Slot creation and drop
    slot_name = "test"
    primary.command_ok(
        ["pg_receivewal", "--slot", slot_name, "--create-slot"],
        "creating a replication slot",
    )
    slot = _slot(primary, slot_name)
    assert slot["slot_type"] == "physical", "physical replication slot was created"
    assert slot["restart_lsn"] == "", "restart LSN of new slot is null"
    primary.command_ok(
        ["pg_receivewal", "--slot", slot_name, "--drop-slot"],
        "dropping a replication slot",
    )
    assert _slot(primary, slot_name)["slot_type"] == "", "replication slot was removed"

    # Generate some WAL.  Use --synchronous at the same time to add more
    # code coverage.  Switch to the next segment first so that subsequent
    # restarts of pg_receivewal will see this segment as full..
    primary.safe_sql("CREATE TABLE test_table(x integer PRIMARY KEY);")
    primary.safe_sql("SELECT pg_switch_wal();")
    nextlsn = primary.safe_sql("SELECT pg_current_wal_insert_lsn();")
    primary.safe_sql("INSERT INTO test_table VALUES (1);")

    # Stream up to the given position.  This is necessary to have a fixed
    # started point for the next commands done in this test, with or without
    # compression involved.
    primary.command_ok(
        [
            "pg_receivewal",
            "--directory",
            stream_dir,
            "--verbose",
            "--endpos",
            nextlsn,
            "--synchronous",
            "--no-loop",
        ],
        "streaming some WAL with --synchronous",
    )

    # Verify that one partial file was generated and keep track of it
    partial_wals = glob.glob(os.path.join(stream_dir, "*.partial"))
    assert len(partial_wals) == 1, "one partial WAL segment was created"

    print("# Testing pg_receivewal with compression methods")

    # Check ZLIB compression if available.
    if not check_pg_config(pg_config, "#define HAVE_LIBZ 1"):
        pytest.skip("postgres was not built with ZLIB support")
    else:
        # Generate more WAL worth one completed, compressed, segment.
        primary.safe_sql("SELECT pg_switch_wal();")
        nextlsn = primary.safe_sql("SELECT pg_current_wal_insert_lsn();")
        primary.safe_sql("INSERT INTO test_table VALUES (2);")

        primary.command_ok(
            [
                "pg_receivewal",
                "--directory",
                stream_dir,
                "--verbose",
                "--endpos",
                nextlsn,
                "--compress",
                "gzip:1",
                "--no-loop",
            ],
            "streaming some WAL using ZLIB compression",
        )

        # Verify that the stored files are generated with their expected
        # names.
        zlib_wals = glob.glob(os.path.join(stream_dir, "*.gz"))
        assert len(zlib_wals) == 1, "one WAL segment compressed with ZLIB was created"
        zlib_partial_wals = glob.glob(os.path.join(stream_dir, "*.gz.partial"))
        assert (
            len(zlib_partial_wals) == 1
        ), "one partial WAL segment compressed with ZLIB was created"

        # Verify that the start streaming position is computed correctly by
        # comparing it with the partial file generated previously.  The name
        # of the previous partial, now-completed WAL segment is updated,
        # keeping its base number.
        completed = partial_wals[0][: -len(".partial")] + ".gz"
        assert zlib_wals[0] == completed, "one partial WAL segment is now completed"
        # Update the list of partial wals with the current one.
        partial_wals = zlib_partial_wals

        # Check the integrity of the completed segment, if gzip is a command
        # available.
        gzip = os.environ.get("GZIP_PROGRAM")
        if not gzip:
            print("# program gzip is not found in your system")
        else:
            gzip_is_valid = subprocess.run(
                [gzip, "--test", *zlib_wals], check=False
            ).returncode
            assert (
                gzip_is_valid == 0
            ), "gzip verified the integrity of compressed WAL segments"

    # Check LZ4 compression if available
    if not check_pg_config(pg_config, "#define USE_LZ4 1"):
        pytest.skip("postgres was not built with LZ4 support")
    else:
        # Generate more WAL including one completed, compressed segment.
        primary.safe_sql("SELECT pg_switch_wal();")
        nextlsn = primary.safe_sql("SELECT pg_current_wal_insert_lsn();")
        primary.safe_sql("INSERT INTO test_table VALUES (3);")

        # Stream up to the given position.
        primary.command_ok(
            [
                "pg_receivewal",
                "--directory",
                stream_dir,
                "--verbose",
                "--endpos",
                nextlsn,
                "--no-loop",
                "--compress",
                "lz4",
            ],
            "streaming some WAL using --compress=lz4",
        )

        # Verify that the stored files are generated with their expected
        # names.
        lz4_wals = glob.glob(os.path.join(stream_dir, "*.lz4"))
        assert len(lz4_wals) == 1, "one WAL segment compressed with LZ4 was created"
        lz4_partial_wals = glob.glob(os.path.join(stream_dir, "*.lz4.partial"))
        assert (
            len(lz4_partial_wals) == 1
        ), "one partial WAL segment compressed with LZ4 was created"

        # Verify that the start streaming position is computed correctly by
        # comparing it with the partial file generated previously.  The name
        # of the previous partial, now-completed WAL segment is updated,
        # keeping its base number.
        base = partial_wals[0]
        if base.endswith(".gz.partial"):
            base = base[: -len(".gz.partial")]
        elif base.endswith(".partial"):
            base = base[: -len(".partial")]
        completed = base + ".lz4"
        assert lz4_wals[0] == completed, "one partial WAL segment is now completed"
        # Update the list of partial wals with the current one.
        partial_wals = lz4_partial_wals

        # Check the integrity of the completed segment, if LZ4 is an available
        # command.
        lz4 = os.environ.get("LZ4")
        if not lz4:
            print("# program lz4 is not found in your system")
        else:
            lz4_is_valid = subprocess.run(
                [lz4, "-t", *lz4_wals], check=False
            ).returncode
            assert (
                lz4_is_valid == 0
            ), "lz4 verified the integrity of compressed WAL segments"

    # Verify that the start streaming position is computed and that the value
    # is correct regardless of whether any compression is available.
    primary.safe_sql("SELECT pg_switch_wal();")
    nextlsn = primary.safe_sql("SELECT pg_current_wal_insert_lsn();")
    primary.safe_sql("INSERT INTO test_table VALUES (4);")
    primary.command_ok(
        [
            "pg_receivewal",
            "--directory",
            stream_dir,
            "--verbose",
            "--endpos",
            nextlsn,
            "--no-loop",
        ],
        "streaming some WAL",
    )

    # Strip an optional (.gz|.lz4) plus .partial suffix to get the name of the
    # completed segment.
    completed = partial_wals[0]
    for suffix in (".gz.partial", ".lz4.partial", ".partial"):
        if completed.endswith(suffix):
            completed = completed[: -len(suffix)]
            break
    assert os.path.exists(
        completed
    ), "check that previously partial WAL is now complete"

    # Permissions on WAL files should be default (unix-only; skipped on Windows).
    if not WINDOWS_OS:
        assert _check_mode_recursive(
            stream_dir, 0o700, 0o600
        ), "check stream dir permissions"

    print("# Testing pg_receivewal with slot as starting streaming point")

    # When using a replication slot, archiving should be resumed from the
    # slot's restart LSN.  Use a new archive location and new slot for this
    # test.
    slot_dir = os.path.join(primary.basedir, "slot_wal")
    os.mkdir(slot_dir)
    slot_name = "archive_slot"

    # Setup the slot, reserving WAL at creation (corresponding to the
    # last redo LSN here, actually, so use a checkpoint to reduce the
    # number of segments archived).
    primary.safe_sql("checkpoint;")
    primary.safe_sql(
        f"SELECT pg_create_physical_replication_slot('{slot_name}', true);"
    )

    # Get the segment name associated with the slot's restart LSN, that should
    # be archived.
    walfile_streamed = primary.safe_sql(
        "SELECT pg_walfile_name(restart_lsn) "
        "FROM pg_replication_slots "
        f"WHERE slot_name = '{slot_name}';"
    )

    # Switch to a new segment, to make sure that the segment retained by the
    # slot is still streamed.  This may not be necessary, but play it safe.
    primary.safe_sql("INSERT INTO test_table VALUES (5);")
    primary.safe_sql("SELECT pg_switch_wal();")
    nextlsn = primary.safe_sql("SELECT pg_current_wal_insert_lsn();")

    # Add a bit more data to accelerate the end of the next pg_receivewal
    # commands.
    primary.safe_sql("INSERT INTO test_table VALUES (6);")

    # Check case where the slot does not exist.
    primary.command_fails_like(
        [
            "pg_receivewal",
            "--directory",
            slot_dir,
            "--slot",
            "nonexistentslot",
            "--no-loop",
            "--no-sync",
            "--verbose",
            "--endpos",
            nextlsn,
        ],
        'pg_receivewal: error: replication slot "nonexistentslot" does not exist',
        "pg_receivewal fails with non-existing slot",
    )
    primary.command_ok(
        [
            "pg_receivewal",
            "--directory",
            slot_dir,
            "--slot",
            slot_name,
            "--no-loop",
            "--no-sync",
            "--verbose",
            "--endpos",
            nextlsn,
        ],
        "WAL streamed from the slot's restart_lsn",
    )
    assert os.path.exists(
        os.path.join(slot_dir, walfile_streamed)
    ), "WAL from the slot's restart_lsn has been archived"

    # Test timeline switch using a replication slot, requiring a promoted
    # standby.
    backup_name = "basebackup"
    primary.backup(backup_name)
    standby = create_pg("standby", start=False)
    standby.init_from_backup(primary, backup_name, has_streaming=True)
    standby.start()

    # Create a replication slot on this new standby
    archive_slot = "archive_slot"
    with Session(
        connstr=standby.connstr() + " replication=database",
        libdir=standby.libdir,
    ) as sess:
        res = sess.query(
            f"CREATE_REPLICATION_SLOT {archive_slot} PHYSICAL (RESERVE_WAL)"
        )
        assert res.error_message is None, res.error_message
    # Wait for standby catchup
    primary.wait_for_catchup(standby)
    # Get a walfilename from before the promotion to make sure it is archived
    # after promotion
    standby_slot = _slot(standby, archive_slot)
    replication_slot_lsn = standby_slot["restart_lsn"]

    # pg_walfile_name() is not supported while in recovery, so use the primary
    # to build the segment name.  Both nodes are on the same timeline, so this
    # produces a segment name with the timeline we are switching from.
    walfile_before_promotion = primary.safe_sql(
        f"SELECT pg_walfile_name('{replication_slot_lsn}');"
    )
    # Everything is setup, promote the standby to trigger a timeline switch.
    standby.promote()

    # Force a segment switch to make sure at least one full WAL is archived
    # on the new timeline.
    walfile_after_promotion = standby.safe_sql(
        "SELECT pg_walfile_name(pg_current_wal_insert_lsn());"
    )
    standby.safe_sql("INSERT INTO test_table VALUES (7);")
    standby.safe_sql("SELECT pg_switch_wal();")
    nextlsn = standby.safe_sql("SELECT pg_current_wal_insert_lsn();")
    # This speeds up the operation.
    standby.safe_sql("INSERT INTO test_table VALUES (8);")

    # Now try to resume from the slot after the promotion.
    timeline_dir = os.path.join(primary.basedir, "timeline_wal")
    os.mkdir(timeline_dir)

    standby.command_ok(
        [
            "pg_receivewal",
            "--directory",
            timeline_dir,
            "--verbose",
            "--endpos",
            nextlsn,
            "--slot",
            archive_slot,
            "--no-sync",
            "--no-loop",
        ],
        "Stream some wal after promoting, resuming from the slot's position",
    )
    assert os.path.exists(
        os.path.join(timeline_dir, walfile_before_promotion)
    ), f"WAL segment {walfile_before_promotion} archived after timeline jump"
    assert os.path.exists(
        os.path.join(timeline_dir, walfile_after_promotion)
    ), f"WAL segment {walfile_after_promotion} archived after timeline jump"
    assert os.path.exists(
        os.path.join(timeline_dir, "00000002.history")
    ), "timeline history file archived after timeline jump"
