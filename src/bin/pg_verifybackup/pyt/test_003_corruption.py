# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that pg_verifybackup detects various forms of backup corruption."""

import os
import shutil
import subprocess

import pytest

from pypg.util import short_tempdir, WINDOWS_OS


def _tar_portability_options(tar):
    """Return the options needed so that the tar program produces a tarfile
    pg_verifybackup can decode (i.e. no pax extensions).
    """
    if not tar:
        return []
    devnull = os.devnull
    if subprocess.call(
        f"{tar} --format=ustar --owner=0 --group=0 -cf {devnull} {devnull}",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) == 0:
        # GNU tar (Linux), BSD tar (FreeBSD, NetBSD, macOS, Windows)
        return ["--format=ustar", "--owner=0", "--group=0"]
    if subprocess.call(
        f"{tar} -F ustar -cf {devnull} {devnull}",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) == 0:
        # OpenBSD tar
        return ["-F", "ustar"]
    return []


def _slurp_dir_entries(path):
    """Directory entries excluding '.' and '..'."""
    return os.listdir(path)


# -- mutilate / cleanup helpers -----------------------------------------------


def _create_extra_file(backup_path, relative_path):
    pathname = os.path.join(backup_path, relative_path)
    with open(pathname, "w", encoding="utf-8") as fh:
        fh.write("This is an extra file.\n")


def mutilate_extra_file(backup_path):
    # Add a file into the root directory of the backup.
    _create_extra_file(backup_path, "extra_file")


def mutilate_extra_tablespace_file(backup_path):
    # Add a file inside the user-defined tablespace.
    (tsoid,) = _slurp_dir_entries(os.path.join(backup_path, "pg_tblspc"))
    (catvdir,) = _slurp_dir_entries(
        os.path.join(backup_path, "pg_tblspc", tsoid))
    (tsdboid,) = _slurp_dir_entries(
        os.path.join(backup_path, "pg_tblspc", tsoid, catvdir))
    _create_extra_file(
        backup_path,
        os.path.join("pg_tblspc", tsoid, catvdir, tsdboid, "extra_ts_file"))


def mutilate_missing_file(backup_path):
    # Remove a file.
    os.unlink(os.path.join(backup_path, "pg_xact", "0000"))


def mutilate_missing_tablespace(backup_path):
    # Remove the symlink to the user-defined tablespace.
    (tsoid,) = _slurp_dir_entries(os.path.join(backup_path, "pg_tblspc"))
    os.unlink(os.path.join(backup_path, "pg_tblspc", tsoid))


def mutilate_append_to_file(backup_path):
    # Append an additional byte to a file.
    with open(os.path.join(backup_path, "global", "pg_control"), "ab") as fh:
        fh.write(b"x")


def mutilate_truncate_file(backup_path):
    # Truncate a file to zero length.
    pathname = os.path.join(backup_path, "pg_hba.conf")
    with open(pathname, "w", encoding="utf-8"):
        pass


def mutilate_replace_file(backup_path):
    # Replace a file's contents without changing the length of the file.  This
    # is not a particularly efficient way to do this, so we pick a file that's
    # expected to be short.
    pathname = os.path.join(backup_path, "PG_VERSION")
    with open(pathname, encoding="utf-8") as fh:
        contents = fh.read()
    with open(pathname, "w", encoding="utf-8") as fh:
        fh.write("q" * len(contents))


def mutilate_bad_manifest(backup_path):
    # Corrupt the backup manifest.
    with open(os.path.join(backup_path, "backup_manifest"), "ab") as fh:
        fh.write(b"\n")


def mutilate_open_file_fails(backup_path):
    # Create a file that can't be opened. (This is skipped on Windows.)
    os.chmod(os.path.join(backup_path, "PG_VERSION"), 0)


def mutilate_open_directory_fails(backup_path):
    # Create a directory that can't be opened. (This is skipped on Windows.)
    os.chmod(os.path.join(backup_path, "pg_subtrans"), 0)


def cleanup_open_directory_fails(backup_path):
    # restore permissions on the unreadable directory we created.
    os.chmod(os.path.join(backup_path, "pg_subtrans"), 0o700)


def mutilate_search_directory_fails(backup_path):
    # Create a directory that can't be searched. (This is skipped on Windows.)
    os.chmod(os.path.join(backup_path, "base"), 0o400)


def cleanup_search_directory_fails(backup_path):
    # rmtree can't cope with a mode 400 directory, so change back to 700.
    os.chmod(os.path.join(backup_path, "base"), 0o700)


# The mutilate for the system_identifier scenario needs to spin up a second
# server, so it is handled specially in the test body rather than as a plain
# closure over the backup dir.
SYSTEM_IDENTIFIER = "system_identifier"

SCENARIOS = [
    {
        "name": "extra_file",
        "mutilate": mutilate_extra_file,
        "fails_like":
            r'extra_file.*present (on disk|in archive "[^"]+") '
            r"but not in the manifest",
    },
    {
        "name": "extra_tablespace_file",
        "mutilate": mutilate_extra_tablespace_file,
        "fails_like":
            r'extra_ts_file.*present (on disk|in archive "[^"]+") '
            r"but not in the manifest",
    },
    {
        "name": "missing_file",
        "mutilate": mutilate_missing_file,
        "fails_like":
            r'pg_xact\/0000.*present in the manifest but not '
            r'(on disk|in archive "[^"]+")',
    },
    {
        "name": "missing_tablespace",
        "mutilate": mutilate_missing_tablespace,
        "fails_like":
            r'pg_tblspc.*present in the manifest but not '
            r'(on disk|in archive "[^"]+")',
    },
    {
        "name": "append_to_file",
        "mutilate": mutilate_append_to_file,
        "fails_like":
            r'has size \d+ (on disk|in archive "[^"]+") '
            r"but size \d+ in the manifest",
    },
    {
        "name": "truncate_file",
        "mutilate": mutilate_truncate_file,
        "fails_like":
            r'has size 0 (on disk|in archive "[^"]+") '
            r"but size \d+ in the manifest",
    },
    {
        "name": "replace_file",
        "mutilate": mutilate_replace_file,
        "fails_like": r"checksum mismatch for file",
    },
    {
        "name": SYSTEM_IDENTIFIER,
        "mutilate": None,
        "fails_like":
            r"manifest system identifier is .*, but control file has",
    },
    {
        "name": "bad_manifest",
        "mutilate": mutilate_bad_manifest,
        "fails_like": r"manifest checksum mismatch",
    },
    {
        "name": "open_file_fails",
        "mutilate": mutilate_open_file_fails,
        "fails_like": r"could not open file",
        "needs_unix_permissions": True,
    },
    {
        "name": "open_directory_fails",
        "mutilate": mutilate_open_directory_fails,
        "cleanup": cleanup_open_directory_fails,
        "fails_like": r"could not open directory",
        "needs_unix_permissions": True,
    },
    {
        "name": "search_directory_fails",
        "mutilate": mutilate_search_directory_fails,
        "cleanup": cleanup_search_directory_fails,
        "fails_like": r"could not stat file or directory",
        "needs_unix_permissions": True,
    },
]


@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_003_corruption(scenario, create_pg, tmp_path):
    # Each corruption scenario is independent (it takes its own backup and
    # verifies it), so build a fresh primary per parametrized case;
    # create_pg tears it down at the end of the test.
    primary = create_pg("primary", allows_streaming=True)

    # Include a user-defined tablespace in the hopes of detecting problems in
    # that area.
    source_ts_path = short_tempdir()

    # CREATE TABLESPACE cannot run inside a transaction block, so issue each
    # statement separately rather than as one multi-statement implicit
    # transaction.
    primary.safe_sql("CREATE TABLE x1 (a int);")
    primary.safe_sql("INSERT INTO x1 VALUES (111);")
    primary.safe_sql(f"CREATE TABLESPACE ts1 LOCATION '{source_ts_path}';")
    primary.safe_sql("CREATE TABLE x2 (a int) TABLESPACE ts1;")
    primary.safe_sql("INSERT INTO x1 VALUES (222);")

    name = scenario["name"]

    # The *_fails scenarios make a file or directory unreadable with chmod(0)
    # and expect pg_verifybackup to report it.  Windows ignores those mode bits,
    # so the backup still verifies and the scenario cannot be exercised there.
    if scenario.get("needs_unix_permissions") and WINDOWS_OS:
        pytest.skip("scenario requires UNIX file permissions")

    # Take a backup and check that it verifies OK.
    backup_path = str(tmp_path / name)
    backup_ts_path = short_tempdir()
    # tablespace gets remapped into a short tempdir so paths stay short.
    primary.command_ok(
        [
            "pg_basebackup",
            "--pgdata", backup_path,
            "--no-sync",
            "--checkpoint", "fast",
            "--tablespace-mapping", f"{source_ts_path}={backup_ts_path}",
        ],
        "base backup ok")
    primary.pg_bin.command_ok(
        ["pg_verifybackup", backup_path], "intact backup verified")

    # Mutilate the backup in some way.
    if name == SYSTEM_IDENTIFIER:
        # Set up another new database instance with different system identifier
        # and make a backup; copy its manifest over to demonstrate the case
        # where the wrong manifest is referred to.
        node = create_pg("node", allows_streaming=True)
        node.backup("backup2")
        shutil.move(
            os.path.join(node.backup_dir, "backup2", "backup_manifest"),
            os.path.join(backup_path, "backup_manifest"))
        node.teardown()
    else:
        scenario["mutilate"](backup_path)

    # Now check that the backup no longer verifies.
    primary.command_fails_like(
        ["pg_verifybackup", backup_path],
        scenario["fails_like"],
        f"corrupt backup fails verification: {name}")

    # Run cleanup hook, if provided.
    if "cleanup" in scenario:
        scenario["cleanup"](backup_path)

    # Turn it into a tar-format backup and see if we can still detect the same
    # problem, unless the scenario needs UNIX permissions or we don't have a
    # TAR program available.  Note that this destructively modifies the backup
    # directory.
    tar = os.environ.get("TAR")
    tar_p_flags = _tar_portability_options(tar)
    if not scenario.get("needs_unix_permissions") and tar:
        tar_backup_path = str(tmp_path / ("tar_" + name))
        os.mkdir(tar_backup_path)

        # tar and then remove each tablespace.  We remove the original files so
        # that they don't also end up in base.tar.
        tsoids = _slurp_dir_entries(os.path.join(backup_path, "pg_tblspc"))
        for tsoid in tsoids:
            tspath = os.path.join(backup_path, "pg_tblspc", tsoid)
            _tar_in(tar, tar_p_flags, tspath,
                    os.path.join(tar_backup_path, f"{tsoid}.tar"))
            _rmtree_like_perl(tspath)

        # tar and remove pg_wal
        _tar_in(tar, tar_p_flags, os.path.join(backup_path, "pg_wal"),
                os.path.join(tar_backup_path, "pg_wal.tar"))
        shutil.rmtree(os.path.join(backup_path, "pg_wal"))

        # move the backup manifest
        shutil.move(os.path.join(backup_path, "backup_manifest"),
                    os.path.join(tar_backup_path, "backup_manifest"))

        # Construct base.tar with what's left.
        _tar_in(tar, tar_p_flags, backup_path,
                os.path.join(tar_backup_path, "base.tar"))

        primary.command_fails_like(
            ["pg_verifybackup", tar_backup_path],
            scenario["fails_like"],
            f"corrupt backup fails verification: {name}")

        shutil.rmtree(tar_backup_path)

    _rmtree_like_perl(backup_path)


def _rmtree_like_perl(path):
    """Remove *path*, handling symlinks and unreadable subdirectories.

    In particular, a symlink (e.g. a pg_tblspc/<oid> tablespace link) is just
    unlinked, not followed; shutil.rmtree refuses to operate on a symlink.
    Directories are removed recursively, restoring permissions on any
    unreadable subdirectory first so the removal can proceed.
    """
    if os.path.islink(path):
        os.unlink(path)
        return
    if not os.path.exists(path):
        return

    def _onerror(func, p, exc):
        # Make the entry accessible and retry (e.g. mode-0 dirs/files left
        # behind by the *_fails scenarios when cleanup did not run).
        try:
            os.chmod(p, 0o700)
        except OSError:
            return
        func(p)

    shutil.rmtree(path, onerror=_onerror)


def _tar_in(tar, tar_p_flags, cwd, outfile):
    """Run tar with *cwd* as working directory, archiving '.'; assert success."""
    proc = subprocess.run(
        [tar, *tar_p_flags, "-cf", outfile, "."],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.returncode == 0, \
        f"tar in {cwd} failed ({proc.returncode}):\n{proc.stdout}"
