# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Small file and polling helpers used by the test framework."""

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

# Default per-operation timeout in seconds (PG_TEST_TIMEOUT_DEFAULT, 180).
TIMEOUT_DEFAULT = int(os.environ.get("PG_TEST_TIMEOUT_DEFAULT") or "180")

# Connection transport: use Unix-domain sockets everywhere except Windows,
# where we listen on TCP (127.0.0.1).  PG_TEST_USE_UNIX_SOCKETS forces Unix
# sockets even on Windows.
WINDOWS_OS = sys.platform in ("win32", "cygwin")
USE_UNIX_SOCKETS = (not WINDOWS_OS) or ("PG_TEST_USE_UNIX_SOCKETS" in os.environ)


def run_captured(argv, *, env=None, combine_stderr=False, timeout=None):
    """Run *argv*, capturing output through temporary files instead of pipes.

    Returns ``(returncode, stdout, stderr)`` as text.  With *combine_stderr*,
    stderr is folded into stdout and the returned stderr is ``""``.

    Output is captured to temporary files rather than ``subprocess.PIPE``
    because of how starting a server behaves on Windows: ``pg_ctl start``
    launches a postmaster that inherits and holds open the write end of the
    parent's stdout/stderr pipe for its entire lifetime.  Reading such a pipe
    to end-of-file -- as subprocess does to collect output -- then blocks until
    the postmaster exits, i.e. forever.  A regular file handle has no
    end-of-file dependency on the writer staying alive, so the parent reads the
    captured output as soon as the launched program returns.
    """
    out = tempfile.TemporaryFile()
    err = subprocess.STDOUT if combine_stderr else tempfile.TemporaryFile()
    try:
        proc = subprocess.run(
            argv, env=env, stdout=out, stderr=err, timeout=timeout, check=False
        )
        out.seek(0)
        stdout = _decode(out.read())
        if combine_stderr:
            stderr = ""
        else:
            err.seek(0)
            stderr = _decode(err.read())
    finally:
        out.close()
        if err is not subprocess.STDOUT:
            err.close()
    return proc.returncode, stdout, stderr


def _decode(data):
    """Decode captured output as text, translating newlines like text mode.

    Programs may emit non-UTF-8 bytes (e.g. LATIN1 object names) that we only
    regex-match, so decode leniently.  Reading a file gives no universal-newline
    handling, so fold CRLF/CR to LF to match what text-mode capture produced and
    what tests expect.
    """
    text = data.decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def dir_symlink(target, link):
    """Create *link* as a link to directory *target*.

    On Windows create a junction (via cmd's ``mklink /j``): that is what the
    server expects for linked directories such as pg_wal, and an ordinary
    symlink there is read back with an error.  Elsewhere create a real symlink.
    """
    if WINDOWS_OS:
        target = target.replace("/", "\\")
        link = link.replace("/", "\\")
        subprocess.run(["cmd", "/c", "mklink", "/j", link, target], check=True)
    else:
        os.symlink(target, link)


def short_tempdir(prefix="pgt"):
    """Create and return a uniquely-named directory under the system temp area.

    The short pathname keeps Unix-socket and tablespace-symlink targets within
    their length limits (a unix socket path and tar's ~100-byte symlink-target
    field).  The caller owns the directory and is responsible for removing it.

    On Windows the directory is created with the default mode so that it
    inherits the parent's access-control list.  That matters when the postmaster
    runs under a restricted access token (one with the Administrators group
    disabled, as happens when launched from an elevated context): it must be
    able to create files such as the socket lock file inside the directory, and
    the owner-only DACL that a private (0o700) mode produces would deny that.
    Elsewhere the directory is created private, like the rest of the framework.
    """
    base = tempfile.gettempdir()
    while True:
        path = os.path.join(base, prefix + secrets.token_hex(8))
        try:
            if sys.platform == "win32":
                os.mkdir(path)
            else:
                os.mkdir(path, 0o700)
            return path
        except FileExistsError:
            continue


def slurp_file(path, offset=0):
    """Return the contents of *path* as text, optionally from *offset* bytes."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        if offset:
            fh.seek(offset)
        return fh.read()


def append_to_file(path, text):
    """Append *text* to *path* (creating it if needed)."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def copy_live_tree(src, dst):
    """Recursively copy *src* to *dst*, tolerating entries that vanish.

    Copying a running server's data directory races with the server: a file
    present when a directory is scanned (e.g. a WAL archive_status flag) can be
    gone before it is copied.  Such ENOENT cases are silently skipped, as a
    low-level base backup must.  Symlinks are recreated as symlinks.
    """
    os.makedirs(dst, exist_ok=True)
    try:
        entries = list(os.scandir(src))
    except FileNotFoundError:
        return
    for entry in entries:
        srcpath = os.path.join(src, entry.name)
        dstpath = os.path.join(dst, entry.name)
        try:
            if entry.is_symlink():
                os.symlink(os.readlink(srcpath), dstpath)
            elif entry.is_dir():
                copy_live_tree(srcpath, dstpath)
            else:
                shutil.copy2(srcpath, dstpath)
        except FileNotFoundError:
            # Entry vanished between the scan and the copy; skip it.
            continue


def poll_until(predicate, timeout=TIMEOUT_DEFAULT, interval=0.1):
    """Call *predicate* until it returns truthy or *timeout* seconds elapse.

    Returns True on success, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(interval)
