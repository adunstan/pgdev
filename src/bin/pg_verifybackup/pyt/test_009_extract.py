# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test server-side extraction of compressed backups, then verify them."""

import os
import re
import shutil
import subprocess


def _have_pg_config_define(define):
    """Return True if pg_config.h contains the given #define line."""
    try:
        out = subprocess.run(
            ["pg_config", "--includedir"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    header = os.path.join(out, "pg_config.h")
    try:
        with open(header, encoding="utf-8", errors="replace") as fh:
            return define in fh.read()
    except OSError:
        return False


def test_009_extract(create_pg):
    """Verify that the client can decompress and extract a server-side backup."""
    primary = create_pg("primary", allows_streaming=True)

    test_configuration = [
        {
            "compression_method": "none",
            "backup_flags": [],
            "enabled": True,
        },
        {
            "compression_method": "gzip",
            "backup_flags": ["--compress", "server-gzip:5"],
            "enabled": _have_pg_config_define("#define HAVE_LIBZ 1"),
        },
        {
            "compression_method": "lz4",
            "backup_flags": ["--compress", "server-lz4:5"],
            "enabled": _have_pg_config_define("#define USE_LZ4 1"),
        },
        {
            "compression_method": "zstd",
            "backup_flags": ["--compress", "server-zstd:5"],
            "enabled": _have_pg_config_define("#define USE_ZSTD 1"),
        },
        {
            "compression_method": "parallel zstd",
            "backup_flags": ["--compress", "server-zstd:workers=3"],
            "enabled": _have_pg_config_define("#define USE_ZSTD 1"),
            "possibly_unsupported": r"could not set compression worker count to 3: "
            r"Unsupported parameter",
        },
    ]

    for tc in test_configuration:
        backup_path = os.path.join(primary.backup_dir, "extract_backup")
        method = tc["compression_method"]

        if not tc["enabled"]:
            print(f"# skipping: {method} compression not supported by this build")
            continue

        # A backup with a valid compression method should work.
        backup = [
            "pg_basebackup",
            "--pgdata",
            backup_path,
            "--wal-method",
            "fetch",
            "--no-sync",
            "--checkpoint",
            "fast",
            "--format",
            "plain",
            *tc["backup_flags"],
        ]
        result = primary.pg_bin.result(backup)
        if result.stdout:
            print("# standard output was:\n" + result.stdout)
        if result.stderr:
            print("# standard error was:\n" + result.stderr)

        skipped = False
        if (
            result.returncode != 0
            and tc.get("possibly_unsupported")
            and re.search(tc["possibly_unsupported"], result.stderr)
        ):
            print(f"# skipping: compression with {method} not supported by this build")
            skipped = True
        else:
            assert result.returncode == 0, f"backup done, compression {method}"

        if not skipped:
            # Make sure that it verifies OK.
            primary.command_ok(
                ["pg_verifybackup", "--exit-on-error", backup_path],
                f'backup verified, compression method "{method}"',
            )

        # Remove backup immediately to save disk space.
        shutil.rmtree(backup_path, ignore_errors=True)
