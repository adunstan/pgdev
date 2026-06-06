# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""This test aims to validate that pg_combinebackup works in the degenerate
case where it is invoked on a single full backup and that it can produce
a new, valid manifest when it does.  Secondarily, it checks that
pg_combinebackup does not produce a manifest when run with --no-manifest.
"""

import os
import re

from pypg.util import slurp_file

# Can be changed to test the other modes.
MODE = os.environ.get("PG_TEST_PG_COMBINEBACKUP_MODE", "--copy")


def _combine_and_test_one_backup(node, original_backup_path, backup_name,
                                 failure_pattern, *extra_options):
    """Process the backup with pg_combinebackup using various manifest options."""
    revised_backup_path = os.path.join(node.backup_dir, backup_name)
    node.command_ok(
        [
            "pg_combinebackup",
            original_backup_path,
            "--output", revised_backup_path,
            "--no-sync",
            *extra_options,
        ],
        f"pg_combinebackup with {' '.join(extra_options)}")
    if failure_pattern is not None:
        node.command_fails_like(
            ["pg_verifybackup", revised_backup_path],
            failure_pattern, f"unable to verify backup {backup_name}")
    else:
        node.command_ok(
            ["pg_verifybackup", revised_backup_path],
            f"verify backup {backup_name}")


def test_004_manifest(create_pg):
    print(f"# testing using mode {MODE}")

    # Set up a new database instance.
    node = create_pg("node", has_archiving=True, allows_streaming=True)

    # Take a full backup.
    original_backup_path = os.path.join(node.backup_dir, "original")
    node.command_ok(
        [
            "pg_basebackup",
            "--pgdata", original_backup_path,
            "--no-sync",
            "--checkpoint", "fast",
        ],
        "full backup")

    # Verify the full backup.
    node.command_ok(["pg_verifybackup", original_backup_path],
                    "verify original backup")

    _combine_and_test_one_backup(
        node, original_backup_path, "nomanifest",
        r"could not open file.*backup_manifest",
        "--no-manifest")
    _combine_and_test_one_backup(
        node, original_backup_path, "csum_none",
        None, "--manifest-checksums=NONE", MODE)
    _combine_and_test_one_backup(
        node, original_backup_path, "csum_sha224",
        None, "--manifest-checksums=SHA224", MODE)

    # Verify that SHA224 is mentioned in the SHA224 manifest lots of times.
    sha224_manifest = slurp_file(
        os.path.join(node.backup_dir, "csum_sha224", "backup_manifest"))
    sha224_count = len(re.findall("SHA224", sha224_manifest, re.M | re.I))
    assert sha224_count > 100, \
        "SHA224 is mentioned many times in SHA224 manifest"

    # Verify that Checksum-Algorithm is not mentioned in the no-checksum
    # manifest.
    nocsum_manifest = slurp_file(
        os.path.join(node.backup_dir, "csum_none", "backup_manifest"))
    nocsum_count = len(re.findall("Checksum-Algorithm", nocsum_manifest,
                                  re.M | re.I))
    assert nocsum_count == 0, \
        "Checksum-Algorithm is not mentioned in no-checksum manifest"
