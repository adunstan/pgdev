# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that pg_verifybackup rejects malformed or invalid backup manifests."""

import os
import re


def _test_bad_manifest(pg_bin, tempdir, test_name, regexp, manifest_contents):
    """Write *manifest_contents* and run pg_verifybackup expecting *regexp*.

    Writes the manifest text to ``<tempdir>/backup_manifest`` and runs
    ``pg_verifybackup <tempdir>``, asserting that it fails with stderr matching
    *regexp*.
    """
    with open(os.path.join(tempdir, "backup_manifest"), "w", encoding="utf-8") as fh:
        fh.write(manifest_contents)

    pg_bin.command_fails_like(["pg_verifybackup", tempdir], regexp, test_name)


def _test_parse_error(pg_bin, tempdir, test_name, manifest_contents):
    """Assert pg_verifybackup reports a manifest parse error for *test_name*."""
    _test_bad_manifest(
        pg_bin,
        tempdir,
        test_name,
        r"could not parse backup manifest: " + re.escape(test_name),
        manifest_contents,
    )


def _test_fatal_error(pg_bin, tempdir, test_name, manifest_contents):
    """Assert pg_verifybackup reports a fatal error for *test_name*."""
    _test_bad_manifest(
        pg_bin, tempdir, test_name, r"error: " + re.escape(test_name), manifest_contents
    )


def test_005_bad_manifest(pg_bin, tmp_path):
    tempdir = str(tmp_path)

    _test_bad_manifest(
        pg_bin,
        tempdir,
        "input string ended unexpectedly",
        r"could not parse backup manifest: The input string ended unexpectedly",
        "{\n",
    )

    _test_parse_error(pg_bin, tempdir, "unexpected object end", "{}\n")

    _test_parse_error(pg_bin, tempdir, "unexpected array start", "[]\n")

    _test_parse_error(
        pg_bin, tempdir, "expected version indicator", '{"not-expected": 1}\n'
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "manifest version not an integer",
        '{"PostgreSQL-Backup-Manifest-Version": "phooey"}\n',
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "unexpected manifest version",
        '{"PostgreSQL-Backup-Manifest-Version": 9876599}\n',
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "unexpected scalar",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": true}\n',
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "unrecognized top-level field",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Oops": 1}\n',
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "unexpected object start",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": {}}\n',
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "missing path name",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [{}]}\n',
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "both path name and encoded path name",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Path": "x", "Encoded-Path": "1234"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "unexpected file field",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Oops": 1}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "missing size",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Path": "x"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "file size is not an integer",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Path": "x", "Size": "Oops"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "could not decode file name",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Encoded-Path": "123", "Size": 0}\n'
        "]}\n",
    )

    _test_fatal_error(
        pg_bin,
        tempdir,
        "duplicate path name in backup manifest",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Path": "x", "Size": 0},\n'
        '    {"Path": "x", "Size": 0}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "checksum without algorithm",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Path": "x", "Size": 100, "Checksum": "Oops"}\n'
        "]}\n",
    )

    _test_fatal_error(
        pg_bin,
        tempdir,
        "unrecognized checksum algorithm",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Path": "x", "Size": 100, "Checksum-Algorithm": "Oops", "Checksum": "00"}\n'
        "]}\n",
    )

    _test_fatal_error(
        pg_bin,
        tempdir,
        "invalid checksum for file",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [\n'
        '    {"Path": "x", "Size": 100, "Checksum-Algorithm": "CRC32C", "Checksum": "0"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "missing start LSN",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "WAL-Ranges": [\n'
        '    {"Timeline": 1}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "missing end LSN",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "WAL-Ranges": [\n'
        '    {"Timeline": 1, "Start-LSN": "0/0"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "unexpected WAL range field",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "WAL-Ranges": [\n'
        '    {"Oops": 1}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "missing timeline",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "WAL-Ranges": [\n    {}\n]}\n',
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "unexpected object end",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "WAL-Ranges": [\n'
        '    {"Timeline": 1, "Start-LSN": "0/0", "End-LSN": "0/0"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "timeline is not an integer",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "WAL-Ranges": [\n'
        '    {"Timeline": true, "Start-LSN": "0/0", "End-LSN": "0/0"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "could not parse start LSN",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "WAL-Ranges": [\n'
        '    {"Timeline": 1, "Start-LSN": "oops", "End-LSN": "0/0"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "could not parse end LSN",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "WAL-Ranges": [\n'
        '    {"Timeline": 1, "Start-LSN": "0/0", "End-LSN": "oops"}\n'
        "]}\n",
    )

    _test_parse_error(
        pg_bin,
        tempdir,
        "expected at least 2 lines",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [], "Manifest-Checksum": null}\n',
    )

    manifest_without_newline = (
        '{"PostgreSQL-Backup-Manifest-Version": 1,\n'
        ' "Files": [],\n'
        ' "Manifest-Checksum": null}'
    )
    _test_parse_error(
        pg_bin, tempdir, "last line not newline-terminated", manifest_without_newline
    )

    _test_fatal_error(
        pg_bin,
        tempdir,
        "invalid manifest checksum",
        '{"PostgreSQL-Backup-Manifest-Version": 1, "Files": [],\n'
        ' "Manifest-Checksum": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz01234567890-"}\n',
    )
