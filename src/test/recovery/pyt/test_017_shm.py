# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests of pg_shmem.h functions."""

# PostgreSQL keys its main System V shared memory segment on the data
# directory's inode.  These tests create a conflicting segment at that key,
# then exercise the postmaster's detection of pre-existing segments across
# restarts, crashes, and a stuck live backend.  We drive shmget/shmctl through
# ctypes so the suite keeps pytest as its only third-party dependency.

import ctypes
import ctypes.util
import os
import re
import signal
import subprocess
import sys
import time

import pytest

from pypg.util import TIMEOUT_DEFAULT

# -- System V shared memory via libc (the IPC::SharedMem stand-in) -----------

IPC_CREAT = 0o1000
IPC_EXCL = 0o2000
IPC_RMID = 0
# S_IRUSR | S_IWUSR
SHM_MODE = 0o600

try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    # key_t is int; size is size_t; flags/cmd are int.
    _libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
    _libc.shmget.restype = ctypes.c_int
    _libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    _libc.shmctl.restype = ctypes.c_int
    _SYSV_AVAILABLE = True
except (OSError, AttributeError):
    _SYSV_AVAILABLE = False


def _key_t(value):
    """Truncate an inode to a signed 32-bit C key_t, matching the backend.

    PostgreSQL keys its main segment on the datadir inode via
    ``NextShmemSegID = statbuf.st_ino`` (src/backend/port/sysv_shmem.c), an
    implicit cast of ino_t to key_t (int).  We reproduce that two's-complement
    truncation so our conflicting segment lands on the same key the postmaster
    will pick.  This is correct on every platform PostgreSQL's SysV path runs
    on; the one assumption to revisit is an exotic filesystem handing out
    inodes wide enough that the truncation diverges from the backend's cast.
    """
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value


class SysVSharedMem:
    """A System V shared memory segment, mirroring the bits of IPC::SharedMem
    that 017_shm needs: create-exclusive and remove."""

    def __init__(self, key, size, flags):
        ctypes.set_errno(0)
        shmid = _libc.shmget(_key_t(key), size, flags)
        if shmid == -1:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self.shmid = shmid

    def remove(self):
        _libc.shmctl(self.shmid, IPC_RMID, None)


def make_conflict_shm(key):
    """Create a 1024-byte segment at *key*, or return None if the key is taken.

    Creates the segment with IPC_CREAT|IPC_EXCL|S_IRUSR|S_IWUSR; a key
    collision is fine and just exercises a different scenario.
    """
    try:
        return SysVSharedMem(key, 1024, IPC_CREAT | IPC_EXCL | SHM_MODE)
    except OSError:
        return None


# -- ipcs diff logging (best effort, purely diagnostic) ----------------------


def _ipcs():
    try:
        return subprocess.run(
            ["ipcs", "-am"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return None


def _log_ipcs(baseline):
    """Print the diff of current `ipcs -am` against *baseline*, swallowing any
    error (the platform may lack ipcs)."""
    current = _ipcs()
    if current is None or baseline is None:
        return
    if current != baseline:
        print("# ipcs -am diff:\n" + current)


# -- start with retries (poll_start) -----------------------------------------


def poll_start(node):
    """Start *node*, retrying for the reasons Cluster::start can transiently
    fail after a kill9 (slow SIGKILL delivery, slow child exit, etc.)."""
    max_attempts = 10 * TIMEOUT_DEFAULT
    for _ in range(max_attempts):
        if node.start(fail_ok=True):
            return True
        time.sleep(0.1)
        # Clean up in case the attempt timed out with a postmaster half-up.
        node.stop("fast", fail_ok=True)
    # One last try that raises on failure.
    return node.start()


_PRE_EXISTING = re.compile(r"pre-existing shared memory block")


def test_017_shm(create_pg):
    if sys.platform == "win32" or not _SYSV_AVAILABLE:
        pytest.skip("SysV shared memory not supported by this platform")

    baseline = _ipcs()

    # Node setup (not started yet; we create the conflicting segment first).
    gnat = create_pg("gnat", start=False)

    # Create a shmem segment that will conflict with gnat's first choice of
    # shmem key.  (If something else already holds that key, that's fine,
    # though the test then exercises a different scenario than usual.)
    gnat_inode = os.stat(gnat.data_dir).st_ino
    print(f"# gnat's datadir inode = {gnat_inode}")

    conflict = make_conflict_shm(gnat_inode)
    if conflict is None:
        print("# could not create conflicting shmem")
    _log_ipcs(baseline)

    gnat.start()
    _log_ipcs(baseline)

    gnat.restart()  # should keep same shmem key
    _log_ipcs(baseline)

    # Upon postmaster death, postmaster children exit automatically.
    gnat.kill9()
    _log_ipcs(baseline)
    poll_start(gnat)  # gnat recycles its former shm key.
    _log_ipcs(baseline)

    print("# removing the conflicting shmem ...")
    if conflict:
        conflict.remove()
    _log_ipcs(baseline)

    # Upon postmaster death, postmaster children exit automatically.
    gnat.kill9()
    _log_ipcs(baseline)

    # In this start, gnat uses its normal shmem key, and fails to remove the
    # higher-keyed segment that the previous postmaster was using.  That's not
    # great, but key collisions should be rare enough not to matter much.
    poll_start(gnat)
    _log_ipcs(baseline)
    gnat.stop()
    _log_ipcs(baseline)

    # Re-create the conflicting segment, and start/stop normally, just so this
    # test doesn't leak the higher-keyed segment.
    print("# re-creating conflicting shmem ...")
    conflict = make_conflict_shm(gnat_inode)
    if conflict is None:
        print("# could not create conflicting shmem")
    _log_ipcs(baseline)

    gnat.start()
    _log_ipcs(baseline)
    gnat.stop()
    _log_ipcs(baseline)

    print("# removing the conflicting shmem ...")
    if conflict:
        conflict.remove()
    _log_ipcs(baseline)

    # Scenarios involving no postmaster.pid, a dead postmaster, and a live
    # backend.  Use the regress.c wait_pid() function to emulate the
    # responsiveness of a backend working through a CPU-intensive task.
    gnat.start()
    _log_ipcs(baseline)

    regress_shlib = os.environ.get("REGRESS_SHLIB")
    if not regress_shlib:
        pytest.skip("REGRESS_SHLIB not set in environment")
    gnat.safe_sql(
        "CREATE FUNCTION wait_pid(int) "
        "RETURNS void "
        f"AS '{regress_shlib}' "
        "LANGUAGE C STRICT"
    )

    # Start a slow backend stuck in wait_pid() on its own PID; it spins until
    # signalled and keeps its shared memory attachment after postmaster death.
    slow_client = gnat.connect()
    slow_pid = int(slow_client.query_oneval("SELECT pg_backend_pid()"))
    slow_query = f"SELECT wait_pid({slow_pid})"
    assert slow_client.do_async(slow_query)
    assert gnat.poll_query_until(
        f"SELECT 1 FROM pg_stat_activity WHERE pid = {slow_pid} AND state = 'active'",
        "1",
    ), "slow query started"

    gnat.kill9()
    os.unlink(gnat.pidfile)
    gnat.rotate_logfile()  # on Windows, can't open old log for writing
    _log_ipcs(baseline)

    # Reject ordinary startup.  Retry for the same reasons poll_start() does,
    # every 0.1s for at least TIMEOUT_DEFAULT seconds.
    max_attempts = 10 * TIMEOUT_DEFAULT
    for _ in range(max_attempts):
        if gnat.start(fail_ok=True) or _PRE_EXISTING.search(gnat.log_content()):
            break
        time.sleep(0.1)
    assert _PRE_EXISTING.search(
        gnat.log_content()
    ), "detected live backend via shared memory"

    # Reject single-user startup.
    gnat.command_fails_like(
        ["postgres", "--single", "-D", gnat.data_dir, "template1"],
        _PRE_EXISTING,
        "single-user mode detected live backend via shared memory",
    )
    _log_ipcs(baseline)

    # Cleanup slow backend: SIGQUIT it (cf 'pg_ctl kill QUIT <pid>'), then the
    # client detects the backend's termination.
    os.kill(slow_pid, signal.SIGQUIT)
    slow_client.close()
    _log_ipcs(baseline)

    # Now startup should work.
    poll_start(gnat)
    _log_ipcs(baseline)

    # Finish testing.
    gnat.stop()
    _log_ipcs(baseline)
