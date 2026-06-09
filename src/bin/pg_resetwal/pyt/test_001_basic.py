# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_resetwal option handling, dry-run output, and value resets."""

import os
import re
import stat

from pypg.util import WINDOWS_OS


def _check_mode_recursive(path, dir_mode, file_mode):
    """Check that every entry under *path* has the expected permissions.

    Returns True if all directories match *dir_mode* and all files match
    *file_mode*.
    """
    ok = True
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, d) for d in dirs]:
            actual = stat.S_IMODE(os.lstat(name).st_mode)
            if actual != dir_mode:
                print(
                    f"# Directory permissions check failed for {name}: "
                    f"expected {dir_mode:#o}, got {actual:#o}"
                )
                ok = False
        for f in files:
            full = os.path.join(root, f)
            if os.path.islink(full):
                continue
            actual = stat.S_IMODE(os.lstat(full).st_mode)
            if actual != file_mode:
                print(
                    f"# File permissions check failed for {full}: "
                    f"expected {file_mode:#o}, got {actual:#o}"
                )
                ok = False
    return ok


def _get_slru_files(data_dir, subdir):
    files = sorted(
        f for f in os.listdir(os.path.join(data_dir, subdir)) if re.search(r"[0-9A-F]+", f)
    )
    return files


def test_pg_resetwal_basic(pg_bin, create_pg):
    pg_bin.program_help_ok("pg_resetwal")
    pg_bin.program_version_ok("pg_resetwal")
    pg_bin.program_options_handling_ok("pg_resetwal")

    node = create_pg("main", start=False)
    node.append_conf("track_commit_timestamp = on")

    pg_bin.command_like(
        ["pg_resetwal", "-n", node.data_dir],
        re.compile(r"checkpoint"),
        "pg_resetwal -n produces output",
    )

    # Permissions on PGDATA should be default (unix-only; skipped on Windows).
    if not WINDOWS_OS:
        assert _check_mode_recursive(node.data_dir, 0o700, 0o600), \
            "check PGDATA permissions"

    pg_bin.command_ok(
        ["pg_resetwal", "--pgdata", node.data_dir],
        "pg_resetwal runs",
    )
    node.start()
    assert node.safe_sql("SELECT 1;") == "1", "server running and working after reset"

    pg_bin.command_fails_like(
        ["pg_resetwal", node.data_dir],
        re.compile(r"lock file .* exists"),
        "fails if server running",
    )

    node.stop("immediate")
    pg_bin.command_fails_like(
        ["pg_resetwal", node.data_dir],
        re.compile(r"database server was not shut down cleanly"),
        "does not run after immediate shutdown",
    )
    pg_bin.command_ok(
        ["pg_resetwal", "--force", node.data_dir],
        "runs after immediate shutdown with force",
    )
    node.start()
    assert node.safe_sql("SELECT 1;") == "1", "server running and working after forced reset"

    node.stop()

    # check various command-line handling

    # Note: This test intends to check that a nonexistent data directory
    # gives a reasonable error message.  Because of the way the code is
    # currently structured, you get an error about readings permissions,
    # which is perhaps suboptimal, so feel free to update this test if
    # this gets improved.
    pg_bin.command_fails_like(
        ["pg_resetwal", "foo"],
        re.compile(r"error: could not read permissions of directory"),
        "fails with nonexistent data directory",
    )

    pg_bin.command_fails_like(
        ["pg_resetwal", "foo", "bar"],
        re.compile(r"too many command-line arguments"),
        "fails with too many command-line arguments",
    )

    # PGDATA set but not used by pg_resetwal (it requires the argument)
    pg_bin.command_fails_like(
        ["pg_resetwal"],
        re.compile(r"no data directory specified"),
        "fails with too few command-line arguments",
        extra_env={"PGDATA": node.data_dir},
    )

    # error cases
    # -c
    pg_bin.command_fails_like(
        ["pg_resetwal", "-c", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option -c"),
        "fails with incorrect -c option",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-c", "10,bar", node.data_dir],
        re.compile(r"error: invalid argument for option -c"),
        "fails with incorrect -c option part 2",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-c", "1,10", node.data_dir],
        re.compile(r"greater than"),
        "fails with -c ids value 1 part 1",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-c", "10,1", node.data_dir],
        re.compile(r"greater than"),
        "fails with -c value 1 part 2",
    )
    # -e
    pg_bin.command_fails_like(
        ["pg_resetwal", "-e", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option -e"),
        "fails with incorrect -e option",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-e", "-1", node.data_dir],
        re.compile(r"error: invalid argument for option -e"),
        "fails with -e value -1",
    )
    # -l
    pg_bin.command_fails_like(
        ["pg_resetwal", "-l", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option -l"),
        "fails with incorrect -l option",
    )
    # -m
    pg_bin.command_fails_like(
        ["pg_resetwal", "-m", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option -m"),
        "fails with incorrect -m option",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-m", "10,bar", node.data_dir],
        re.compile(r"error: invalid argument for option -m"),
        "fails with incorrect -m option part 2",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-m", "0,10", node.data_dir],
        re.compile(r"must not be 0"),
        "fails with -m value 0 in the first part",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-m", "10,0", node.data_dir],
        re.compile(r"must not be 0"),
        "fails with -m value 0 in the second part",
    )
    # -o
    pg_bin.command_fails_like(
        ["pg_resetwal", "-o", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option -o"),
        "fails with incorrect -o option",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-o", "0", node.data_dir],
        re.compile(r"must not be 0"),
        "fails with -o value 0",
    )
    # -O
    pg_bin.command_fails_like(
        ["pg_resetwal", "-O", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option -O"),
        "fails with incorrect -O option",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-O", "-1", node.data_dir],
        re.compile(r"error: invalid argument for option -O"),
        "fails with -O value -1",
    )
    # --wal-segsize
    pg_bin.command_fails_like(
        ["pg_resetwal", "--wal-segsize", "foo", node.data_dir],
        re.compile(r"error: invalid value"),
        "fails with incorrect --wal-segsize option",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "--wal-segsize", "13", node.data_dir],
        re.compile(r"must be a power"),
        "fails with invalid --wal-segsize value",
    )
    # -u
    pg_bin.command_fails_like(
        ["pg_resetwal", "-u", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option -u"),
        "fails with incorrect -u option",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-u", "1", node.data_dir],
        re.compile(r"must be greater than"),
        "fails with -u value too small",
    )
    # -x
    pg_bin.command_fails_like(
        ["pg_resetwal", "-x", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option -x"),
        "fails with incorrect -x option",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-x", "1", node.data_dir],
        re.compile(r"must be greater than"),
        "fails with -x value too small",
    )

    # Check out of range values with -x. These are forbidden for all other
    # 32-bit values too, but we use just -x to exercise the parsing.
    pg_bin.command_fails_like(
        ["pg_resetwal", "-x", "-1", node.data_dir],
        re.compile(r"error: invalid argument for option -x"),
        "fails with -x value -1",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-x", "-100", node.data_dir],
        re.compile(r"error: invalid argument for option -x"),
        "fails with negative -x value",
    )
    pg_bin.command_fails_like(
        ["pg_resetwal", "-x", "10000000000", node.data_dir],
        re.compile(r"error: invalid argument for option -x"),
        "fails with -x value too large",
    )

    # --char-signedness
    pg_bin.command_fails_like(
        ["pg_resetwal", "--char-signedness", "foo", node.data_dir],
        re.compile(r"error: invalid argument for option --char-signedness"),
        "fails with incorrect --char-signedness option",
    )

    # run with control override options

    out = pg_bin.result(["pg_resetwal", "--dry-run", node.data_dir]).stdout
    m = re.search(r"^Database block size: *(\d+)$", out, re.MULTILINE)
    assert m, "could not find Database block size in dry-run output"
    blcksz = int(m.group(1))

    cmd = ["pg_resetwal", "--pgdata", node.data_dir]

    # some not-so-critical hardcoded values
    cmd += ["--epoch", "1"]
    cmd += ["--next-wal-file", "00000001000000320000004B"]
    cmd += ["--next-oid", "100000"]
    cmd += ["--wal-segsize", "1"]

    # these use the guidance from the documentation

    data_dir = node.data_dir

    files = _get_slru_files(data_dir, "pg_commit_ts")
    # XXX: Should there be a multiplier, similar to the other options?
    # -c argument is "old,new"
    cmd += [
        "--commit-timestamp-ids",
        "%d,%d" % (3 if int(files[0], 16) == 0 else int(files[0], 16), int(files[-1], 16)),
    ]

    files = _get_slru_files(data_dir, "pg_multixact/offsets")
    mult = 32 * blcksz // 8
    # --multixact-ids argument is "new,old".  For the "old" part, the filename
    # is coerced to a decimal number, multiplied by mult, then re-read as hex.
    old_mxid = 1 if int(files[0], 16) == 0 else int(str(int(files[0]) * mult), 16)
    cmd += [
        "--multixact-ids",
        "%d,%d" % ((int(files[-1], 16) + 1) * mult, old_mxid),
    ]

    files = _get_slru_files(data_dir, "pg_multixact/members")
    mult = 32 * (blcksz // 20) * 4
    cmd += ["--multixact-offset", str((int(files[-1], 16) + 1) * mult)]

    files = _get_slru_files(data_dir, "pg_xact")
    mult = 32 * blcksz * 4
    cmd += [
        "--oldest-transaction-id",
        str(3 if int(files[0], 16) == 0 else int(files[0], 16) * mult),
        "--next-transaction-id",
        str((int(files[-1], 16) + 1) * mult),
    ]

    pg_bin.command_ok(cmd + ["--dry-run"], "runs with control override options, dry run")
    pg_bin.command_ok(cmd, "runs with control override options")
    pg_bin.command_like(
        ["pg_resetwal", "--dry-run", node.data_dir],
        re.compile(r"^Latest checkpoint's NextOID: *100000$", re.MULTILINE),
        "spot check that control changes were applied",
    )

    node.start()
    assert True, "server started after reset"
