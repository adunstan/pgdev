# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Locate the libpq shared library.

A lightweight replacement for ctypes.util that searches caller-supplied
directories first and then common system locations, honoring LD_LIBRARY_PATH /
DYLD_LIBRARY_PATH, and returns the full path to the library file.
"""

import glob
import os
import sys


def find_lib_or_die(lib, libpath=None, systempath=True):
    """Return the full path to the shared library named *lib* (e.g. "pq").

    *libpath* is an iterable of directories searched first.  When
    *systempath* is false the common system locations are not searched (used
    when the caller passes the cluster's own --libdir and wants exactly that).
    Raises RuntimeError if nothing is found.
    """
    search_paths = list(libpath or [])
    if systempath:
        search_paths.extend(_system_lib_paths())

    patterns = _lib_patterns(lib)

    for directory in search_paths:
        if not os.path.isdir(directory):
            continue
        for pattern in patterns:
            for match in sorted(glob.glob(os.path.join(directory, pattern))):
                if os.path.isfile(match) and os.access(match, os.R_OK):
                    return match

    raise RuntimeError(
        f"find_lib_or_die: unable to find lib{lib} in: " + ", ".join(search_paths)
    )


def _lib_patterns(lib):
    if sys.platform == "darwin":
        return (f"lib{lib}.dylib", f"lib{lib}.*.dylib")
    if sys.platform in ("win32", "cygwin"):
        return (f"{lib}.dll", f"lib{lib}.dll")
    # Linux and other Unix-like systems
    return (f"lib{lib}.so", f"lib{lib}.so.*")


def _system_lib_paths():
    paths = ["/usr/lib", "/usr/local/lib", "/lib"]

    if sys.platform.startswith("linux"):
        paths += [
            "/usr/lib/x86_64-linux-gnu",
            "/usr/lib/aarch64-linux-gnu",
            "/usr/lib64",
            "/lib64",
        ]
        if os.environ.get("LD_LIBRARY_PATH"):
            paths += os.environ["LD_LIBRARY_PATH"].split(os.pathsep)

    if sys.platform == "darwin":
        paths += ["/opt/homebrew/lib", "/usr/local/opt/libpq/lib"]
        if os.environ.get("DYLD_LIBRARY_PATH"):
            paths += os.environ["DYLD_LIBRARY_PATH"].split(os.pathsep)

    return paths
