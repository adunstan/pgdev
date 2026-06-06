# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for pg_archivecleanup, covering which WAL files it removes and retains."""

import os
import re


# WAL file patterns created before each sub-scenario.  "present" tracks
# whether the file should still exist after running pg_archivecleanup.
WALFILES_VERBOSE = [
    {"name": "00000001000000370000000D", "present": False},
    {"name": "00000001000000370000000E", "present": True},
]
WALFILES_WITH_GZ = [
    {"name": "00000001000000370000000C.gz", "present": False},
    {"name": "00000001000000370000000D", "present": False},
    {"name": "00000001000000370000000D.backup", "present": True},
    {"name": "00000001000000370000000E", "present": True},
    {"name": "00000001000000370000000F.partial", "present": True},
    {"name": "unrelated_file", "present": True},
]
WALFILES_FOR_CLEAN_BACKUP_HISTORY = [
    {"name": "00000001000000370000000D", "present": False},
    {"name": "00000001000000370000000D.00000028.backup", "present": False},
    {"name": "00000001000000370000000E", "present": True},
    {"name": "00000001000000370000000F.partial", "present": True},
    {"name": "unrelated_file", "present": True},
]


def _create_files(tempdir, walfiles):
    for entry in walfiles:
        with open(os.path.join(tempdir, entry["name"]), "w", encoding="utf-8") as fh:
            fh.write("CONTENT")


def _remove_files(tempdir, walfiles):
    for entry in walfiles:
        path = os.path.join(tempdir, entry["name"])
        if os.path.exists(path):
            os.unlink(path)


def test_pg_archivecleanup(pg_bin, tmp_path):
    tempdir = str(tmp_path)

    pg_bin.program_help_ok("pg_archivecleanup")
    pg_bin.program_version_ok("pg_archivecleanup")
    pg_bin.program_options_handling_ok("pg_archivecleanup")

    pg_bin.command_fails_like(
        ["pg_archivecleanup"],
        r"must specify archive location",
        "fails if archive location is not specified",
    )
    pg_bin.command_fails_like(
        ["pg_archivecleanup", tempdir],
        r"must specify oldest kept WAL file",
        "fails if oldest kept WAL file name is not specified",
    )
    pg_bin.command_fails_like(
        ["pg_archivecleanup", "notexist", "foo"],
        r"archive location .* does not exist",
        "fails if archive location does not exist",
    )
    pg_bin.command_fails_like(
        ["pg_archivecleanup", tempdir, "foo", "bar"],
        r"too many command-line arguments",
        "fails with too many command-line arguments",
    )
    pg_bin.command_fails_like(
        ["pg_archivecleanup", tempdir, "foo"],
        r"invalid file name argument",
        "fails with invalid restart file name",
    )

    # Dry run: nothing is physically removed, but logs show what would be.
    _create_files(tempdir, WALFILES_VERBOSE)
    res = pg_bin.result(
        ["pg_archivecleanup", "--debug", "--dry-run", tempdir, "00000001000000370000000E"]
    )
    assert res.returncode == 0, "pg_archivecleanup dry run: exit code 0"
    for entry in WALFILES_VERBOSE:
        pat = re.compile(re.escape(entry["name"]) + r".*would be removed")
        if entry["present"]:
            assert not pat.search(res.stderr), f"dry run for {entry['name']}: matches"
        else:
            assert pat.search(res.stderr), f"dry run for {entry['name']}: matches"
    for entry in WALFILES_VERBOSE:
        assert os.path.isfile(os.path.join(tempdir, entry["name"])), (
            f"{entry['name']} not removed"
        )
    _remove_files(tempdir, WALFILES_VERBOSE)

    def run_check(testdata, oldestkeptwalfile, test_name, *options):
        _create_files(tempdir, testdata)
        pg_bin.command_ok(
            ["pg_archivecleanup", *options, tempdir, oldestkeptwalfile],
            f"{test_name}: runs",
        )
        for entry in testdata:
            path = os.path.join(tempdir, entry["name"])
            if entry["present"]:
                assert os.path.isfile(path), f"{test_name}:{entry['name']} was not cleaned up"
            else:
                assert not os.path.isfile(path), f"{test_name}:{entry['name']} was cleaned up"
        _remove_files(tempdir, testdata)

    run_check(WALFILES_WITH_GZ, "00000001000000370000000E", "pg_archivecleanup", "-x.gz")
    run_check(
        WALFILES_WITH_GZ,
        "00000001000000370000000E.partial",
        "pg_archivecleanup with .partial file",
        "-x.gz",
    )
    run_check(
        WALFILES_WITH_GZ,
        "00000001000000370000000E.00000020.backup",
        "pg_archivecleanup with .backup file",
        "-x.gz",
    )
    run_check(
        WALFILES_FOR_CLEAN_BACKUP_HISTORY,
        "00000001000000370000000E",
        "pg_archivecleanup with --clean-backup-history",
        "-b",
    )
