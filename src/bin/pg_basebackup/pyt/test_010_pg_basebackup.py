# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test pg_basebackup across its many options and output formats."""

import glob
import os
import re
import stat
import subprocess
import tempfile

import pytest

from pypg.util import TIMEOUT_DEFAULT, append_to_file, slurp_file

# pg_basebackup invocation defaults: keep test times reasonable.  Used as the
# leading elements of the argument list passed to the node command_* helpers.
PG_BASEBACKUP_DEFS = ["pg_basebackup", "--no-sync", "-cfast"]

# Some tests depend on optional build features (e.g. compression libraries).
# We probe the installed pg_config.h at runtime for the corresponding HAVE_*
# defines and skip those tests when the feature is absent.


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


def _slurp_dir(path):
    """Return directory entries, including '.' and '..'."""
    return [".", ".."] + os.listdir(path)


def check_mode_recursive(path, dir_mode, file_mode):
    """Recursively verify directory and file permission bits.

    Symlinks are ignored.  Returns True if all modes match.
    """
    ok = True
    for root, dirs, files in os.walk(path):
        st = os.lstat(root)
        if stat.S_IMODE(st.st_mode) != dir_mode:
            print(f"mode of directory {root} is {oct(stat.S_IMODE(st.st_mode))}, "
                  f"expected {oct(dir_mode)}")
            ok = False
        for name in files:
            fp = os.path.join(root, name)
            if os.path.islink(fp):
                continue
            st = os.lstat(fp)
            if stat.S_IMODE(st.st_mode) != file_mode:
                print(f"mode of file {fp} is {oct(stat.S_IMODE(st.st_mode))}, "
                      f"expected {oct(file_mode)}")
                ok = False
    return ok


def chmod_recursive(path, dir_mode, file_mode):
    """Recursively chmod *path* applying *dir_mode* and *file_mode*."""
    os.chmod(path, dir_mode)
    for root, dirs, files in os.walk(path):
        for name in dirs:
            os.chmod(os.path.join(root, name), dir_mode)
        for name in files:
            fp = os.path.join(root, name)
            if not os.path.islink(fp):
                os.chmod(fp, file_mode)


def test_010_pg_basebackup(create_pg, pg_bin, tmp_path):
    pg_bin.program_help_ok("pg_basebackup")
    pg_bin.program_version_ok("pg_basebackup")
    pg_bin.program_options_handling_ok("pg_basebackup")

    tempdir = str(tmp_path / "tempdir")
    os.mkdir(tempdir)

    # Set umask so test directories and files are created with default
    # permissions (pg_basebackup creates the target dir honoring the umask).
    old_umask = os.umask(0o077)
    try:
        _run_body(create_pg, tempdir)
    finally:
        os.umask(old_umask)


def _run_body(create_pg, tempdir):
    # Initialize node without replication settings.  The framework's
    # init() (without allows_streaming) leaves the compiled-in defaults
    # (wal_level=replica, max_wal_senders=10), so explicitly write
    # wal_level=minimal / max_wal_senders=0 here to make the "fails because of
    # WAL configuration" check below meaningful.  (The default trust pg_hba
    # already permits the backupuser role, so no extra role setup is needed.)
    node = create_pg("main", start=False, initdb_extra=["--data-checksums"])
    node.append_conf("\n".join([
        "wal_level = minimal",
        "max_wal_senders = 0",
        "",
    ]))
    node.start()
    pgdata = node.data_dir

    node.command_fails(
        ["pg_basebackup"],
        "pg_basebackup needs target directory specified")

    # Sanity checks for options.
    node.command_fails_like(
        ["pg_basebackup", "--pgdata", f"{tempdir}/backup", "--compress", "none:1"],
        r'compression algorithm "none" does not accept a compression level',
        'failure if method "none" specified with compression level')
    node.command_fails_like(
        ["pg_basebackup", "--pgdata", f"{tempdir}/backup", "--compress", "none+"],
        r'unrecognized compression algorithm: "none\+"',
        "failure on incorrect separator to define compression level")

    # Write a file with a non-UTF8 name to test backup of such files.  Some
    # Windows ANSI code pages may reject this filename; on POSIX it is fine.
    os.makedirs(f"{tempdir}/pgdata", exist_ok=True)
    with open(os.path.join(tempdir.encode(), b"pgdata",
                           b"FOO\xe0\xe0\xe0BAR"), "ab") as fh:
        fh.write(b"test backup of file with non-UTF8 name\n")

    # set_replication_conf / reload: the default trust pg_hba already permits
    # local replication, so no pg_hba change is needed for unix sockets.
    node.reload()

    node.command_fails(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup"],
        "pg_basebackup fails because of WAL configuration")

    assert not os.path.isdir(f"{tempdir}/backup"), \
        "backup directory was cleaned up"

    # Create a backup directory that is not empty so the next command will
    # fail but leave the data directory behind.
    os.mkdir(f"{tempdir}/backup")
    append_to_file(f"{tempdir}/backup/dir-not-empty.txt", "Some data")

    node.command_fails(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup", "-n"],
        "failing run with no-clean option")

    assert os.path.isdir(f"{tempdir}/backup"), \
        "backup directory was created and left behind"
    _rmtree(f"{tempdir}/backup")

    node.append_conf("\n".join([
        "max_replication_slots = 10",
        "max_wal_senders = 10",
        "wal_level = replica",
    ]))
    node.restart()

    # Now that we have a server that supports replication commands, test
    # whether certain invalid compression commands fail on the client side
    # with client-side compression and on the server side with server-side
    # compression.
    if not HAVE_LIBZ:
        pytest.skip("postgres was not built with ZLIB support")
    else:
        client_fails = "pg_basebackup: error: "
        server_fails = \
            "pg_basebackup: error: could not initiate base backup: ERROR:  "
        compression_failure_tests = [
            ["extrasquishy",
             'unrecognized compression algorithm: "extrasquishy"',
             "failure on invalid compression algorithm"],
            ["gzip:",
             "invalid compression specification: found empty string where a "
             "compression option was expected",
             "failure on empty compression options list"],
            ["gzip:thunk",
             "invalid compression specification: unrecognized compression "
             'option: "thunk"',
             "failure on unknown compression option"],
            ["gzip:level",
             'invalid compression specification: compression option "level" '
             "requires a value",
             "failure on missing compression level"],
            ["gzip:level=",
             'invalid compression specification: value for compression option '
             '"level" must be an integer',
             "failure on empty compression level"],
            ["gzip:level=high",
             'invalid compression specification: value for compression option '
             '"level" must be an integer',
             "failure on non-numeric compression level"],
            ["gzip:level=236",
             'invalid compression specification: compression algorithm "gzip" '
             "expects a compression level between 1 and 9",
             "failure on out-of-range compression level"],
            ["gzip:level=9,",
             "invalid compression specification: found empty string where a "
             "compression option was expected",
             "failure on extra, empty compression option"],
            ["gzip:workers=3",
             'invalid compression specification: compression algorithm "gzip" '
             "does not accept a worker count",
             "failure on worker count for gzip"],
            ["gzip:long",
             'invalid compression specification: compression algorithm "gzip" '
             "does not support long-distance mode",
             "failure on long mode for gzip"],
        ]
        for spec, errmsg, label in compression_failure_tests:
            cfail = re.escape(client_fails + errmsg)
            sfail = re.escape(server_fails + errmsg)
            node.command_fails_like(
                ["pg_basebackup", "--pgdata", f"{tempdir}/backup",
                 "--compress", spec],
                cfail, "client " + label)
            node.command_fails_like(
                ["pg_basebackup", "--pgdata", f"{tempdir}/backup",
                 "--compress", "server-" + spec],
                sfail, "server " + label)

    # Write some files to test that they are not copied.
    for filename in ("backup_label", "tablespace_map",
                     "postgresql.auto.conf.tmp", "current_logfiles.tmp",
                     "global/pg_internal.init.123"):
        append_to_file(os.path.join(pgdata, filename), "DONOTCOPY")

    # Test that macOS system files are skipped.  Only test on non-macOS
    # systems since creating incorrect .DS_Store files on macOS may have
    # unintended side effects.
    import sys
    if sys.platform != "darwin":
        append_to_file(os.path.join(pgdata, ".DS_Store"), "DONOTCOPY")

    # Connect to a database to create global/pg_internal.init.  If this is
    # removed the test below would return a false positive.
    node.safe_sql("SELECT 1;")

    # Create an unlogged table to test that forks other than init are not
    # copied.
    node.safe_sql("CREATE UNLOGGED TABLE base_unlogged (id int)")

    base_unlogged_path = node.safe_sql(
        "select pg_relation_filepath('base_unlogged')")

    # Make sure main and init forks exist.
    assert os.path.isfile(os.path.join(pgdata, base_unlogged_path + "_init")), \
        "unlogged init fork in base"
    assert os.path.isfile(os.path.join(pgdata, base_unlogged_path)), \
        "unlogged main fork in base"

    # Create files that look like temporary relations to ensure they are
    # ignored.
    postgres_oid = node.safe_sql(
        "select oid from pg_database where datname = 'postgres'")

    temp_relation_files = ["t999_999", "t9999_999.1", "t999_9999_vm",
                           "t99999_99999_vm.1"]
    for filename in temp_relation_files:
        append_to_file(
            os.path.join(pgdata, "base", postgres_oid, filename),
            "TEMP_RELATION")

    # Run base backup.
    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup",
                              "--wal-method", "none"],
        "pg_basebackup runs")
    assert os.path.isfile(f"{tempdir}/backup/PG_VERSION"), "backup was created"
    assert os.path.isfile(f"{tempdir}/backup/backup_manifest"), \
        "backup manifest included"

    # Permissions on backup should be default (unix-only; skipped on Windows).
    assert check_mode_recursive(f"{tempdir}/backup", 0o700, 0o600), \
        "check backup dir permissions"

    # Only archive_status and summaries directories should be copied in
    # pg_wal/.
    assert sorted(_slurp_dir(f"{tempdir}/backup/pg_wal/")) == \
        sorted([".", "..", "archive_status", "summaries"]), \
        "no WAL files copied"

    # Contents of these directories should not be copied.
    for dirname in ("pg_dynshmem", "pg_notify", "pg_replslot", "pg_serial",
                    "pg_snapshots", "pg_stat_tmp", "pg_subtrans"):
        assert sorted(_slurp_dir(f"{tempdir}/backup/{dirname}/")) == \
            sorted([".", ".."]), f"contents of {dirname}/ not copied"

    # These files should not be copied.
    for filename in ("postgresql.auto.conf.tmp", "postmaster.opts",
                     "postmaster.pid", "tablespace_map", "current_logfiles.tmp",
                     "global/pg_internal.init", "global/pg_internal.init.123"):
        assert not os.path.isfile(f"{tempdir}/backup/{filename}"), \
            f"{filename} not copied"

    # We only test .DS_Store files being skipped on non-macOS systems.
    if sys.platform != "darwin":
        assert not os.path.isfile(f"{tempdir}/backup/.DS_Store"), \
            ".DS_Store not copied"

    # Unlogged relation forks other than init should not be copied.
    assert os.path.isfile(f"{tempdir}/backup/{base_unlogged_path}_init"), \
        "unlogged init fork in backup"
    assert not os.path.isfile(f"{tempdir}/backup/{base_unlogged_path}"), \
        "unlogged main fork not in backup"

    # Temp relations should not be copied.
    for filename in temp_relation_files:
        assert not os.path.isfile(
            f"{tempdir}/backup/base/{postgres_oid}/{filename}"), \
            f"base/{postgres_oid}/{filename} not copied"

    # Make sure existing backup_label was ignored.
    assert slurp_file(f"{tempdir}/backup/backup_label") != "DONOTCOPY", \
        "existing backup_label not copied"
    _rmtree(f"{tempdir}/backup")

    # Now delete the bogus backup_label file since it will interfere with
    # startup.
    os.unlink(os.path.join(pgdata, "backup_label"))

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup2",
                              "--no-manifest", "--waldir", f"{tempdir}/xlog2"],
        "separate xlog directory")
    assert os.path.isfile(f"{tempdir}/backup2/PG_VERSION"), "backup was created"
    assert not os.path.isfile(f"{tempdir}/backup2/backup_manifest"), \
        "manifest was suppressed"
    assert os.path.isdir(f"{tempdir}/xlog2/"), "xlog directory was created"
    _rmtree(f"{tempdir}/backup2")
    _rmtree(f"{tempdir}/xlog2")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/tarbackup",
                              "--format", "tar"],
        "tar format")
    assert os.path.isfile(f"{tempdir}/tarbackup/base.tar"), \
        "backup tar was created"
    _rmtree(f"{tempdir}/tarbackup")

    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_foo",
                              "--format", "plain",
                              "--tablespace-mapping", "=/foo"],
        r"invalid tablespace mapping format",
        "--tablespace-mapping with empty old directory fails")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_foo",
                              "--format", "plain",
                              "--tablespace-mapping", "/foo="],
        r"invalid tablespace mapping format",
        "--tablespace-mapping with empty new directory fails")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_foo",
                              "--format", "plain",
                              "--tablespace-mapping", "/foo=/bar=/baz"],
        r'multiple "=" signs in tablespace mapping',
        "--tablespace-mapping with multiple = fails")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_foo",
                              "--format", "plain",
                              "--tablespace-mapping", "foo=/bar"],
        r"old directory is not an absolute path in tablespace mapping",
        "--tablespace-mapping with old directory not absolute fails")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_foo",
                              "--format", "plain",
                              "--tablespace-mapping", "/foo=bar"],
        r"new directory is not an absolute path in tablespace mapping",
        "--tablespace-mapping with new directory not absolute fails")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_foo",
                              "--format", "plain",
                              "--tablespace-mapping", "foo"],
        r"invalid tablespace mapping format",
        "--tablespace-mapping with invalid format fails")

    superlongname = "superlongname_" + ("x" * 100)
    # Tar format doesn't support filenames longer than 100 bytes.
    superlongpath = os.path.join(pgdata, superlongname)
    with open(superlongpath, "w"):
        pass
    node.command_fails(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/tarbackup_l1",
                              "--format", "tar"],
        "pg_basebackup tar with long name fails")
    os.unlink(superlongpath)

    # The following tests are for symlinks.

    # Move pg_replslot out of $pgdata and create a symlink to it.
    node.stop()

    # Set umask so test directories and files are created with group
    # permissions.
    os.umask(0o027)

    # Enable group permissions on PGDATA.
    chmod_recursive(pgdata, 0o750, 0o640)

    # Create a temporary directory in a short system location (for short
    # tablespace path names, to stay under the tar 99-char limit).
    sys_tempdir = tempfile.mkdtemp(prefix="pgt")

    # pg_replslot should be empty.  Remove and recreate it under sys_tempdir
    # before symlinking, to avoid moving across drives.
    os.rmdir(os.path.join(pgdata, "pg_replslot"))
    os.mkdir(os.path.join(sys_tempdir, "pg_replslot"))
    os.symlink(os.path.join(sys_tempdir, "pg_replslot"),
               os.path.join(pgdata, "pg_replslot"))

    node.start()

    # Test backup of a tablespace using tar format.  Symlink the system
    # located tempdir to our physical temp location so we can use shorter
    # names for the tablespace directories.
    real_sys_tempdir = os.path.join(sys_tempdir, "tempdir")
    os.symlink(tempdir, real_sys_tempdir)

    os.mkdir(f"{tempdir}/tblspc1")
    real_ts_dir = f"{real_sys_tempdir}/tblspc1"
    node.safe_sql(f"CREATE TABLESPACE tblspc1 LOCATION '{real_ts_dir}';")
    node.safe_sql("CREATE TABLE test1 (a int) TABLESPACE tblspc1;"
                   "INSERT INTO test1 VALUES (1234);")
    node.backup("tarbackup2", backup_options=["--format", "tar"])
    # empty test1, just so that it's different from the to-be-restored data
    node.safe_sql("TRUNCATE TABLE test1;")

    # basic checks on the output
    backupdir = os.path.join(node.backup_dir, "tarbackup2")
    assert os.path.isfile(f"{backupdir}/base.tar"), "backup tar was created"
    assert os.path.isfile(f"{backupdir}/pg_wal.tar"), "WAL tar was created"
    tblspc_tars = glob.glob(f"{backupdir}/[0-9]*.tar")
    assert len(tblspc_tars) == 1, "one tablespace tar was created"

    # Try to verify the tar-format backup by restoring it.
    #
    # FRAMEWORK GAP: PostgresServer.init_from_backup only supports plain-format
    # backups (tar_program / tablespace_map variants are explicitly
    # unsupported in this unix-socket-only framework), so the restore-and-query
    # sub-check is skipped here.

    # Create an unlogged table to test that forks other than init are not
    # copied.
    node.safe_sql(
        "CREATE UNLOGGED TABLE tblspc1_unlogged (id int) TABLESPACE tblspc1;")

    tblspc1_unlogged_path = node.safe_sql(
        "select pg_relation_filepath('tblspc1_unlogged')")

    # Make sure main and init forks exist.
    assert os.path.isfile(
        os.path.join(pgdata, tblspc1_unlogged_path + "_init")), \
        "unlogged init fork in tablespace"
    assert os.path.isfile(os.path.join(pgdata, tblspc1_unlogged_path)), \
        "unlogged main fork in tablespace"

    # Create files that look like temporary relations to ensure they are
    # ignored in a tablespace.
    temp_relation_files = ["t888_888", "t888888_888888_vm.1"]
    test1_path = node.safe_sql("select pg_relation_filepath('test1')")
    tblspc1_id = os.path.basename(
        os.path.dirname(os.path.dirname(test1_path)))

    for filename in temp_relation_files:
        append_to_file(
            f"{real_sys_tempdir}/tblspc1/{tblspc1_id}/{postgres_oid}/{filename}",
            "TEMP_RELATION")

    node.command_fails(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup1",
                              "--format", "plain"],
        "plain format with tablespaces fails without tablespace mapping")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup1",
                              "--format", "plain",
                              "--tablespace-mapping",
                              f"{real_ts_dir}={tempdir}/tbackup/tblspc1"],
        "plain format with tablespaces succeeds with tablespace mapping")
    assert os.path.isdir(f"{tempdir}/tbackup/tblspc1"), \
        "tablespace was relocated"

    # Check the tablespace symlink was updated (unix only; junctions on
    # Windows don't support -l).
    found = False
    for entry in os.listdir(os.path.join(pgdata, "pg_tblspc")):
        link = f"{tempdir}/backup1/pg_tblspc/{entry}"
        if os.path.islink(link) and \
                os.readlink(link) == f"{tempdir}/tbackup/tblspc1":
            found = True
            break
    assert found, "tablespace symlink was updated"

    # Group access should be enabled on all backup files (unix only).
    assert check_mode_recursive(f"{tempdir}/backup1", 0o750, 0o640), \
        "check backup dir permissions"

    # Unlogged relation forks other than init should not be copied.
    m = re.search(r"[^/]*/[^/]*/[^/]*$", tblspc1_unlogged_path)
    tblspc1_unlogged_backup_path = m.group(0)

    assert os.path.isfile(
        f"{tempdir}/tbackup/tblspc1/{tblspc1_unlogged_backup_path}_init"), \
        "unlogged init fork in tablespace backup"
    assert not os.path.isfile(
        f"{tempdir}/tbackup/tblspc1/{tblspc1_unlogged_backup_path}"), \
        "unlogged main fork not in tablespace backup"

    # Temp relations should not be copied.
    for filename in temp_relation_files:
        assert not os.path.isfile(
            f"{tempdir}/tbackup/tblspc1/{tblspc1_id}/{postgres_oid}/{filename}"), \
            f"[tblspc1]/{postgres_oid}/{filename} not copied"

        # Also remove temp relation files or tablespace drop will fail.
        filepath = \
            f"{real_sys_tempdir}/tblspc1/{tblspc1_id}/{postgres_oid}/{filename}"
        os.unlink(filepath)

    assert os.path.isdir(f"{tempdir}/backup1/pg_replslot"), \
        "pg_replslot symlink copied as directory"
    _rmtree(f"{tempdir}/backup1")

    os.mkdir(f"{tempdir}/tbl=spc2")
    real_ts_dir = f"{real_sys_tempdir}/tbl=spc2"
    node.safe_sql("DROP TABLE test1;")
    node.safe_sql("DROP TABLE tblspc1_unlogged;")
    node.safe_sql("DROP TABLESPACE tblspc1;")
    node.safe_sql(f"CREATE TABLESPACE tblspc2 LOCATION '{real_ts_dir}';")
    # Escape '=' in the old directory for the --tablespace-mapping argument.
    real_ts_dir_escaped = real_ts_dir.replace("=", "\\=")
    node.command_ok(
        PG_BASEBACKUP_DEFS + [
            "--pgdata", f"{tempdir}/backup3", "--format", "plain",
            "--tablespace-mapping",
            f"{real_ts_dir_escaped}={tempdir}/tbackup/tbl\\=spc2"],
        "mapping tablespace with = sign in path")
    assert os.path.isdir(f"{tempdir}/tbackup/tbl=spc2"), \
        "tablespace with = sign was relocated"
    node.safe_sql("DROP TABLESPACE tblspc2;")
    _rmtree(f"{tempdir}/backup3")

    os.mkdir(f"{tempdir}/{superlongname}")
    real_ts_dir = f"{real_sys_tempdir}/{superlongname}"
    node.safe_sql(f"CREATE TABLESPACE tblspc3 LOCATION '{real_ts_dir}';")
    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/tarbackup_l3",
                              "--format", "tar"],
        "pg_basebackup tar with long symlink target")
    node.safe_sql("DROP TABLESPACE tblspc3;")
    _rmtree(f"{tempdir}/tarbackup_l3")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupR",
                              "--write-recovery-conf"],
        "pg_basebackup --write-recovery-conf runs")
    assert os.path.isfile(f"{tempdir}/backupR/postgresql.auto.conf"), \
        "postgresql.auto.conf exists"
    assert os.path.isfile(f"{tempdir}/backupR/standby.signal"), \
        "standby.signal was created"
    recovery_conf = slurp_file(f"{tempdir}/backupR/postgresql.auto.conf")
    _rmtree(f"{tempdir}/backupR")

    port = node.port
    assert re.search(rf"^primary_conninfo = '.*port={port}.*'\n",
                     recovery_conf, re.M), \
        "postgresql.auto.conf sets primary_conninfo"

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxd"],
        "pg_basebackup runs in default xlog mode")
    assert any(re.match(r"^[0-9A-F]{24}$", f)
               for f in _slurp_dir(f"{tempdir}/backupxd/pg_wal")), \
        "WAL files copied"
    _rmtree(f"{tempdir}/backupxd")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxf",
                              "--wal-method", "fetch"],
        "pg_basebackup --wal-method fetch runs")
    assert any(re.match(r"^[0-9A-F]{24}$", f)
               for f in _slurp_dir(f"{tempdir}/backupxf/pg_wal")), \
        "WAL files copied"
    _rmtree(f"{tempdir}/backupxf")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxs",
                              "--wal-method", "stream"],
        "pg_basebackup --wal-method stream runs")
    assert any(re.match(r"^[0-9A-F]{24}$", f)
               for f in _slurp_dir(f"{tempdir}/backupxs/pg_wal")), \
        "WAL files copied"
    _rmtree(f"{tempdir}/backupxs")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxst",
                              "--wal-method", "stream", "--format", "tar"],
        "pg_basebackup --wal-method stream runs in tar mode")
    assert os.path.isfile(f"{tempdir}/backupxst/pg_wal.tar"), \
        "tar file was created"
    _rmtree(f"{tempdir}/backupxst")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupnoslot",
                              "--wal-method", "stream", "--no-slot"],
        "pg_basebackup --wal-method stream runs with --no-slot")
    _rmtree(f"{tempdir}/backupnoslot")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxf",
                              "--wal-method", "fetch"],
        "pg_basebackup --wal-method fetch runs")

    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--target", "blackhole"],
        r"WAL cannot be streamed when a backup target is specified",
        "backup target requires --wal-method")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--target", "blackhole", "--wal-method", "stream"],
        r"WAL cannot be streamed when a backup target is specified",
        "backup target requires --wal-method other than --wal-method stream")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--target", "bogus", "--wal-method", "none"],
        r"unrecognized target",
        "backup target unrecognized")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--target", "blackhole", "--wal-method", "none",
                              "--pgdata", f"{tempdir}/blackhole"],
        r"cannot specify both output directory and backup target",
        "backup target and output directory")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--target", "blackhole", "--wal-method", "none",
                              "--format", "tar"],
        r"cannot specify both format and backup target",
        "backup target and format")
    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--target", "blackhole", "--wal-method", "none"],
        "backup target blackhole")
    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--target", f"server:{tempdir}/backuponserver",
                              "--wal-method", "none"],
        "backup target server")
    assert os.path.isfile(f"{tempdir}/backuponserver/base.tar"), \
        "backup tar was created"
    _rmtree(f"{tempdir}/backuponserver")

    node.command_ok(
        ["createuser", "--replication", "--role=pg_write_server_files",
         "backupuser"],
        "create backup user")
    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--username", "backupuser",
                              "--target", f"server:{tempdir}/backuponserver",
                              "--wal-method", "none"],
        "backup target server")
    assert os.path.isfile(f"{tempdir}/backuponserver/base.tar"), \
        "backup tar was created as non-superuser"
    _rmtree(f"{tempdir}/backuponserver")

    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxs_sl_fail",
                              "--wal-method", "stream", "--slot", "slot0"],
        r'replication slot "slot0" does not exist',
        "pg_basebackup fails with nonexistent replication slot")

    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxs_slot",
                              "--create-slot"],
        r"--create-slot needs a slot to be specified using --slot",
        "pg_basebackup --create-slot fails without slot name")

    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxs_slot",
                              "--create-slot", "--slot", "slot0", "--no-slot"],
        r"--no-slot cannot be used with slot name",
        "pg_basebackup fails with --create-slot --slot --no-slot")
    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--target", "blackhole",
                              "--pgdata", f"{tempdir}/blackhole"],
        r"cannot specify both output directory and backup target",
        "backup target and output directory")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backuptr/co",
                              "--wal-method", "none"],
        "pg_basebackup --wal-method fetch runs")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxs_slot",
                              "--create-slot", "--slot", "slot0"],
        "pg_basebackup --create-slot runs")
    _rmtree(f"{tempdir}/backupxs_slot")

    assert node.safe_sql(
        "SELECT slot_name FROM pg_replication_slots "
        "WHERE slot_name = 'slot0'") == "slot0", \
        "replication slot was created"
    assert node.safe_sql(
        "SELECT restart_lsn FROM pg_replication_slots "
        "WHERE slot_name = 'slot0'") != "", \
        "restart LSN of new slot is not null"

    node.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxs_slot1",
                              "--create-slot", "--slot", "slot0"],
        r'replication slot "slot0" already exists',
        "pg_basebackup fails with --create-slot --slot and a previously "
        "existing slot")

    node.safe_sql(
        "SELECT * FROM pg_create_physical_replication_slot('slot1')")
    lsn = node.safe_sql(
        "SELECT restart_lsn FROM pg_replication_slots "
        "WHERE slot_name = 'slot1'")
    assert lsn == "", "restart LSN of new slot is null"
    node.command_fails(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/fail",
                              "--slot", "slot1", "--wal-method", "none"],
        "pg_basebackup with replication slot fails without WAL streaming")
    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxs_sl",
                              "--wal-method", "stream", "--slot", "slot1"],
        "pg_basebackup --wal-method stream with replication slot runs")
    lsn = node.safe_sql(
        "SELECT restart_lsn FROM pg_replication_slots "
        "WHERE slot_name = 'slot1'")
    assert re.match(r"^0/[0-9A-Z]{7,8}$", lsn), \
        "restart LSN of slot has advanced"
    _rmtree(f"{tempdir}/backupxs_sl")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backupxs_sl_R",
                              "--wal-method", "stream", "--slot", "slot1",
                              "--write-recovery-conf"],
        "pg_basebackup with replication slot and --write-recovery-conf runs")
    assert re.search(r"^primary_slot_name = 'slot1'\n",
                     slurp_file(f"{tempdir}/backupxs_sl_R/postgresql.auto.conf"),
                     re.M), \
        "recovery conf file sets primary_slot_name"

    checksum = node.safe_sql("SHOW data_checksums;")
    assert checksum == "on", "checksums are enabled"
    _rmtree(f"{tempdir}/backupxs_sl_R")

    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_dbname_R",
                              "--wal-method", "stream",
                              "--dbname", "dbname=db1",
                              "--write-recovery-conf"],
        "pg_basebackup with dbname and --write-recovery-conf runs")
    assert re.search(
        r"dbname=db1",
        slurp_file(f"{tempdir}/backup_dbname_R/postgresql.auto.conf"), re.M), \
        "recovery conf file sets dbname"
    _rmtree(f"{tempdir}/backup_dbname_R")

    # create tables to corrupt and get their relfilenodes
    file_corrupt1 = node.safe_sql(
        "CREATE TABLE corrupt1 AS SELECT a FROM generate_series(1,10000) AS a; "
        "ALTER TABLE corrupt1 SET (autovacuum_enabled=false); "
        "SELECT pg_relation_filepath('corrupt1')")
    file_corrupt2 = node.safe_sql(
        "CREATE TABLE corrupt2 AS SELECT b FROM generate_series(1,2) AS b; "
        "ALTER TABLE corrupt2 SET (autovacuum_enabled=false); "
        "SELECT pg_relation_filepath('corrupt2')")

    # get block size for corruption steps
    block_size = int(node.safe_sql("SHOW block_size;"))

    # induce corruption
    node.stop()
    node.corrupt_page_checksum(file_corrupt1, 0)
    node.start()

    node.command_checks_all(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_corrupt"],
        1,
        [r"^$"],
        [r"(?s)^WARNING.*checksum verification failed"],
        "pg_basebackup reports checksum mismatch")
    _rmtree(f"{tempdir}/backup_corrupt")

    # induce further corruption in 5 more blocks
    node.stop()
    for i in range(1, 6):
        node.corrupt_page_checksum(file_corrupt1, i * block_size)
    node.start()

    node.command_checks_all(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_corrupt2"],
        1,
        [r"^$"],
        [r"(?s)^WARNING.*further.*failures.*will.not.be.reported"],
        "pg_basebackup does not report more than 5 checksum mismatches")
    _rmtree(f"{tempdir}/backup_corrupt2")

    # induce corruption in a second file
    node.stop()
    node.corrupt_page_checksum(file_corrupt2, 0)
    node.start()

    node.command_checks_all(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_corrupt3"],
        1,
        [r"^$"],
        [r"(?s)^WARNING.*7 total checksum verification failures"],
        "pg_basebackup correctly report the total number of checksum mismatches")
    _rmtree(f"{tempdir}/backup_corrupt3")

    # do not verify checksums, should return ok
    node.command_ok(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_corrupt4",
                              "--no-verify-checksums"],
        "pg_basebackup with -k does not report checksum mismatch")
    _rmtree(f"{tempdir}/backup_corrupt4")

    node.safe_sql("DROP TABLE corrupt1;")
    node.safe_sql("DROP TABLE corrupt2;")

    print("# Testing pg_basebackup with compression methods")

    # Check ZLIB compression if available.
    if not HAVE_LIBZ:
        print("# postgres was not built with ZLIB support; "
              "skipping compression tests")
    else:
        node.command_ok(
            PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_gzip",
                                  "--compress", "1", "--format", "t"],
            "pg_basebackup with --compress")
        node.command_ok(
            PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_gzip2",
                                  "--gzip", "--format", "t"],
            "pg_basebackup with --gzip")
        node.command_ok(
            PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/backup_gzip3",
                                  "--compress", "gzip:1", "--format", "t"],
            "pg_basebackup with --compress=gzip:1")

        # Verify that the stored files are generated with their expected names.
        zlib_files = glob.glob(f"{tempdir}/backup_gzip/*.tar.gz")
        assert len(zlib_files) == 2, \
            "two files created with --compress=NUM (base.tar.gz and pg_wal.tar.gz)"
        zlib_files2 = glob.glob(f"{tempdir}/backup_gzip2/*.tar.gz")
        assert len(zlib_files2) == 2, \
            "two files created with --gzip (base.tar.gz and pg_wal.tar.gz)"
        zlib_files3 = glob.glob(f"{tempdir}/backup_gzip3/*.tar.gz")
        assert len(zlib_files3) == 2, \
            "two files created with --compress=gzip:NUM (base.tar.gz and pg_wal.tar.gz)"

        # Check the integrity of the files generated using Python's gzip
        # module (equivalent to a "gzip --test" check).
        import gzip as gzipmod
        for gzpath in zlib_files + zlib_files2 + zlib_files3:
            with gzipmod.open(gzpath, "rb") as gf:
                while gf.read(1024 * 1024):
                    pass
        _rmtree(f"{tempdir}/backup_gzip")
        _rmtree(f"{tempdir}/backup_gzip2")
        _rmtree(f"{tempdir}/backup_gzip3")

    # Test background stream process terminating before the basebackup has
    # finished; the main process should exit gracefully with an error message
    # on stderr.  To reduce timing risk we throttle the base backup.
    node.safe_sql(
        "CREATE TABLE t AS SELECT a FROM generate_series(1,10000) AS a;")

    # Set a distinguishing application_name so we can find the walsender.
    app_name = "test_010_pg_basebackup"
    connstr = node.connstr("postgres") + f" application_name={app_name}"
    sigchld_bb = subprocess.Popen(
        [os.path.join(node.bindir, "pg_basebackup"), "--no-sync", "-cfast",
         "--wal-method=stream", "--pgdata", f"{tempdir}/sigchld",
         "--max-rate", "32", "--dbname", connstr],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)

    assert node.poll_query_until(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE "
        f"application_name = '{app_name}' AND wait_event = 'WalSenderMain' "
        "AND backend_type = 'walsender' AND query ~ 'START_REPLICATION'"), \
        "Walsender killed"

    try:
        _, stderr = sigchld_bb.communicate(timeout=TIMEOUT_DEFAULT)
    except subprocess.TimeoutExpired:
        sigchld_bb.kill()
        _, stderr = sigchld_bb.communicate()
    assert re.search(r"background process terminated unexpectedly", stderr), \
        "background process exit message"

    # Test that we can back up an in-place tablespace.  CREATE TABLESPACE
    # cannot run inside a transaction block, so issue the GUC SET as a
    # separate statement on the same (cached) session rather than as one
    # multi-statement implicit transaction.
    node.safe_sql("SET allow_in_place_tablespaces = on;")
    node.safe_sql("CREATE TABLESPACE tblspc2 LOCATION '';")
    node.safe_sql("CREATE TABLE test2 (a int) TABLESPACE tblspc2;"
                   "INSERT INTO test2 VALUES (1234);")
    tblspc_oid = node.safe_sql(
        "SELECT oid FROM pg_tablespace WHERE spcname = 'tblspc2';")
    node.backup("backup3")
    node.safe_sql("DROP TABLE test2;")
    node.safe_sql("DROP TABLESPACE tblspc2;")

    # check that the in-place tablespace exists in the backup
    backupdir = os.path.join(node.backup_dir, "backup3")
    dst_tblspc = glob.glob(f"{backupdir}/pg_tblspc/{tblspc_oid}/PG_*")
    assert len(dst_tblspc) == 1, "tblspc directory copied"

    # Can't take backup with referring manifest of different cluster.
    #
    # Set up another new database instance.  Since create_pg always runs a
    # fresh initdb, it gets a different system ID.
    node2 = create_pg("node2", start=False, has_archiving=True,
                      allows_streaming=True)
    node2.append_conf("summarize_wal = on")
    node2.start()

    node2.command_fails_like(
        PG_BASEBACKUP_DEFS + ["--pgdata", f"{tempdir}/diff_sysid",
                              "--incremental", f"{backupdir}/backup_manifest"],
        r"system identifier in backup manifest is .*, but database system "
        r"identifier is",
        "pg_basebackup fails with different database system manifest")


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)
