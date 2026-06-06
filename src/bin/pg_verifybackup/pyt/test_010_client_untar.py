# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_verifybackup against client-side compressed (tar-format) backups."""

import os
import re
import shutil
import subprocess


# Compression-method tests gate on the HAVE_LIBZ / USE_LZ4 / USE_ZSTD defines.
# We probe the installed pg_config.h at runtime and skip those cases when a
# compression method is not supported by this build.


def _have_pg_config_define(define):
    """Return True if pg_config.h contains the given #define line."""
    try:
        out = subprocess.run(
            ["pg_config", "--includedir"],
            stdout=subprocess.PIPE, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return False
    header = os.path.join(out, "pg_config.h")
    try:
        with open(header, encoding="utf-8", errors="replace") as fh:
            return define in fh.read()
    except OSError:
        return False


HAVE_LIBZ = _have_pg_config_define("#define HAVE_LIBZ 1")
USE_LZ4 = _have_pg_config_define("#define USE_LZ4 1")
USE_ZSTD = _have_pg_config_define("#define USE_ZSTD 1")


def test_010_client_untar(create_pg):
    """Verify client-side backup compression and tar-format verification.

    This test case aims to verify that client-side backup compression works
    properly, and it also aims to verify that pg_verifybackup can verify a base
    backup that didn't start out in plain format.
    """
    primary = create_pg("primary", allows_streaming=True)

    # Create file with some random data and an arbitrary size, useful to check
    # the solidity of the compression and decompression logic.  The size of the
    # file is chosen to be around 640kB.  This has proven to be large enough to
    # detect some issues related to LZ4, and low enough to not impact the
    # runtime of the test significantly.
    junk_data = primary.safe_sql(
        """
            SELECT string_agg(encode(sha256(i::bytea), 'hex'), '')
            FROM generate_series(1, 10240) s(i);""")
    data_dir = primary.data_dir
    junk_file = os.path.join(data_dir, "junk")
    with open(junk_file, "w", encoding="utf-8") as jf:
        jf.write(junk_data)

    backup_path = os.path.join(primary.backup_dir, "client-backup")

    test_configuration = [
        {
            "compression_method": "none",
            "backup_flags": [],
            "backup_archive": "base.tar",
            "enabled": True,
        },
        {
            "compression_method": "gzip",
            "backup_flags": ["--compress", "client-gzip:5"],
            "backup_archive": "base.tar.gz",
            "enabled": HAVE_LIBZ,
        },
        {
            "compression_method": "lz4",
            "backup_flags": ["--compress", "client-lz4:5"],
            "backup_archive": "base.tar.lz4",
            "enabled": USE_LZ4,
        },
        {
            "compression_method": "lz4",
            "backup_flags": ["--compress", "client-lz4:1"],
            "backup_archive": "base.tar.lz4",
            "enabled": USE_LZ4,
        },
        {
            "compression_method": "zstd",
            "backup_flags": ["--compress", "client-zstd:5"],
            "backup_archive": "base.tar.zst",
            "enabled": USE_ZSTD,
        },
        {
            "compression_method": "zstd",
            "backup_flags": ["--compress", "client-zstd:level=1,long"],
            "backup_archive": "base.tar.zst",
            "enabled": USE_ZSTD,
        },
        {
            "compression_method": "parallel zstd",
            "backup_flags": ["--compress", "client-zstd:workers=3"],
            "backup_archive": "base.tar.zst",
            "enabled": USE_ZSTD,
            "possibly_unsupported":
                r"could not set compression worker count to 3: "
                r"Unsupported parameter",
        },
    ]

    for tc in test_configuration:
        method = tc["compression_method"]

        if not tc["enabled"]:
            # skip "$method compression not supported by this build"
            continue

        # Take a client-side backup.
        backup = primary.pg_bin.result([
            "pg_basebackup", "--no-sync",
            "--pgdata", backup_path,
            "--wal-method", "fetch",
            "--checkpoint", "fast",
            "--format", "tar",
            *tc["backup_flags"],
        ])
        if backup.stdout != "":
            print("# standard output was:\n" + backup.stdout)
        if backup.stderr != "":
            print("# standard error was:\n" + backup.stderr)

        if (backup.returncode != 0
                and tc.get("possibly_unsupported")
                and re.search(tc["possibly_unsupported"], backup.stderr)):
            # skip "compression with $method not supported by this build"
            continue
        else:
            assert backup.returncode == 0, \
                f"client side backup, compression {method}"

        # Verify that the we got the files we expected.
        backup_files = ",".join(sorted(os.listdir(backup_path)))
        expected_backup_files = ",".join(
            sorted(["backup_manifest", tc["backup_archive"]]))
        assert backup_files == expected_backup_files, \
            f"found expected backup files, compression {method}"

        # Verify tar backup.
        primary.command_ok(
            ["pg_verifybackup", "--exit-on-error", backup_path],
            f"verify backup, compression {method}")

        # Cleanup.
        shutil.rmtree(backup_path)
