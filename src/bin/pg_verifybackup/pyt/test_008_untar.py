# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_verifybackup against server-side (tar-format) backups."""

import os
import shutil
import subprocess
import tempfile


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


def _slurp_dir(path):
    """Return directory entries, including '.' and '..'."""
    return [".", ".."] + os.listdir(path)


# This test case aims to verify that server-side backups and server-side
# backup compression work properly, and it also aims to verify that
# pg_verifybackup can verify a base backup that didn't start out in plain
# format.
def test_008_untar(create_pg):
    primary = create_pg("primary", allows_streaming=True)

    # Create file with some random data and an arbitrary size, useful to check
    # the solidity of the compression and decompression logic.  The size of the
    # file is chosen to be around 640kB.  This has proven to be large enough to
    # detect some issues related to LZ4, and low enough to not impact the
    # runtime of the test significantly.
    junk_data = primary.safe_sql(
        "SELECT string_agg(encode(sha256(i::bytea), 'hex'), '') "
        "FROM generate_series(1, 10240) s(i);")
    data_dir = primary.data_dir
    junk_file = os.path.join(data_dir, "junk")
    with open(junk_file, "w", encoding="utf-8") as jf:
        jf.write(junk_data)

    # Create a tablespace directory.
    source_ts_path = tempfile.mkdtemp(prefix="pgt")

    # Create a tablespace with table in it.  CREATE TABLESPACE cannot run
    # inside a transaction block, so issue each statement separately on the
    # cached session rather than as one multi-statement implicit transaction.
    primary.safe_sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{source_ts_path}';")
    primary.safe_sql("SELECT oid FROM pg_tablespace WHERE spcname = 'regress_ts1';")
    primary.safe_sql(
        "CREATE TABLE regress_tbl1(i int) TABLESPACE regress_ts1;")
    primary.safe_sql("INSERT INTO regress_tbl1 VALUES(generate_series(1,5));")
    tsoid = primary.safe_sql(
        "SELECT oid FROM pg_tablespace WHERE spcname = 'regress_ts1'")

    backup_path = os.path.join(primary.backup_dir, "server-backup")

    test_configuration = [
        {
            "compression_method": "none",
            "backup_flags": [],
            "backup_archive": ["base.tar", f"{tsoid}.tar"],
            "enabled": True,
        },
        {
            "compression_method": "gzip",
            "backup_flags": ["--compress", "server-gzip"],
            "backup_archive": ["base.tar.gz", f"{tsoid}.tar.gz"],
            "enabled": _have_pg_config_define("#define HAVE_LIBZ 1"),
        },
        {
            "compression_method": "lz4",
            "backup_flags": ["--compress", "server-lz4"],
            "backup_archive": ["base.tar.lz4", f"{tsoid}.tar.lz4"],
            "enabled": _have_pg_config_define("#define USE_LZ4 1"),
        },
        {
            "compression_method": "lz4",
            "backup_flags": ["--compress", "server-lz4:5"],
            "backup_archive": ["base.tar.lz4", f"{tsoid}.tar.lz4"],
            "enabled": _have_pg_config_define("#define USE_LZ4 1"),
        },
        {
            "compression_method": "zstd",
            "backup_flags": ["--compress", "server-zstd"],
            "backup_archive": ["base.tar.zst", f"{tsoid}.tar.zst"],
            "enabled": _have_pg_config_define("#define USE_ZSTD 1"),
        },
        {
            "compression_method": "zstd",
            "backup_flags": ["--compress", "server-zstd:level=1,long"],
            "backup_archive": ["base.tar.zst", f"{tsoid}.tar.zst"],
            "enabled": _have_pg_config_define("#define USE_ZSTD 1"),
        },
    ]

    for tc in test_configuration:
        method = tc["compression_method"]

        if not tc["enabled"]:
            print(f"# skipping: {method} compression not supported by this build")
            continue
        # A configuration could also be skipped when its decompress_program is
        # unavailable, but none of the configurations above set one, so that
        # case never fires.

        # Take a server-side backup.
        primary.command_ok(
            [
                "pg_basebackup", "--no-sync",
                "--checkpoint", "fast",
                "--target", f"server:{backup_path}",
                "--wal-method", "fetch",
                *tc["backup_flags"],
            ],
            f"server side backup, compression {method}")

        # Verify that the we got the files we expected.
        backup_files = ",".join(sorted(
            e for e in _slurp_dir(backup_path) if e not in (".", "..")))
        expected_backup_files = ",".join(sorted(
            ["backup_manifest", *tc["backup_archive"]]))
        assert backup_files == expected_backup_files, \
            f"found expected backup files, compression {method}"

        # Verify tar backup.
        primary.command_ok(
            ["pg_verifybackup", "--exit-on-error", backup_path],
            f"verify backup, compression {method}")

        # Cleanup.
        shutil.rmtree(backup_path)
