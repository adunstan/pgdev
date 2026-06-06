# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Small file and polling helpers used by the test framework."""

import os
import time

# Default per-operation timeout in seconds (PG_TEST_TIMEOUT_DEFAULT, 180).
TIMEOUT_DEFAULT = int(os.environ.get("PG_TEST_TIMEOUT_DEFAULT", "180"))


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
