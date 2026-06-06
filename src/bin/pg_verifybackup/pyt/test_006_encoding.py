# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Verify that pg_verifybackup handles hex-encoded filenames correctly."""

import os
import re

from pypg.util import slurp_file


def test_006_encoding(create_pg, tmp_path):
    primary = create_pg("primary", allows_streaming=True)

    backup_path = str(tmp_path / "test_encoding")
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup_path,
            "--no-sync",
            "--checkpoint", "fast",
            "--manifest-force-encode",
        ],
        "backup ok with forced hex encoding")

    manifest = slurp_file(os.path.join(backup_path, "backup_manifest"))
    count_of_encoded_path_in_manifest = len(
        re.findall(r"Encoded-Path", manifest, re.I | re.M))
    assert count_of_encoded_path_in_manifest > 100, \
        "many paths are encoded in the manifest"

    primary.command_like(
        ["pg_verifybackup", "--skip-checksums", backup_path],
        r"backup successfully verified",
        "backup with forced encoding verified")
