# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_verifybackup with each supported checksum algorithm."""

import os
import re
import shutil

from pypg.util import slurp_file


def _test_checksums(primary, fmt, algorithm):
    # Forward slashes: pg_basebackup creates the target directory with
    # pg_mkdir_p, which splits on "/", so a backslash path would not have its
    # intermediate <fmt> directory created.
    backup_path = "/".join(
        [primary.backup_dir.replace("\\", "/"), fmt, algorithm])
    backup = [
        "pg_basebackup",
        "--pgdata", backup_path,
        "--manifest-checksums", algorithm,
        "--no-sync",
        "--checkpoint", "fast",
    ]
    verify = ["pg_verifybackup", "--exit-on-error", backup_path]

    if fmt == "tar":
        # Add switch to get a tar-format backup
        backup += ["--format", "tar"]

    # A backup with a bogus algorithm should fail.
    if algorithm == "bogus":
        primary.command_fails(
            backup,
            f'{fmt} format backup fails with algorithm "{algorithm}"')
        return

    # A backup with a valid algorithm should work.
    primary.command_ok(
        backup,
        f'{fmt} format backup ok with algorithm "{algorithm}"')

    # We expect each real checksum algorithm to be mentioned on every line of
    # the backup manifest file except the first and last; for simplicity, we
    # just check that it shows up lots of times. When the checksum algorithm
    # is none, we just check that the manifest exists.
    if algorithm == "none":
        assert os.path.isfile(os.path.join(backup_path, "backup_manifest")), \
            f"{fmt} format backup manifest exists"
    else:
        manifest = slurp_file(os.path.join(backup_path, "backup_manifest"))
        count_of_algorithm_in_manifest = \
            len(re.findall(algorithm, manifest, re.M | re.I))
        assert count_of_algorithm_in_manifest > 100, \
            f"{algorithm} is mentioned many times in the manifest"

    # Make sure that it verifies OK.
    primary.command_ok(
        verify,
        f'verify {fmt} format backup with algorithm "{algorithm}"')

    # Remove backup immediately to save disk space.
    shutil.rmtree(backup_path)


def test_002_algorithm(create_pg):
    """Verify that we can take and verify backups with various checksum types."""
    primary = create_pg("primary", allows_streaming=True)

    # Do the check
    for fmt in ("plain", "tar"):
        for algorithm in ("bogus", "none", "crc32c",
                          "sha224", "sha256", "sha384", "sha512"):
            _test_checksums(primary, fmt, algorithm)
