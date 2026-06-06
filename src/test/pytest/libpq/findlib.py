# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Locate the libpq shared library.

A lightweight replacement for ctypes.util that searches caller-supplied
directories first and then common system locations, honoring LD_LIBRARY_PATH /
DYLD_LIBRARY_PATH, and returns the full path to the library file.
"""

import ctypes
import glob
import os
import sys


def libpq_abi_skip_reason(libdir):
    """Return a reason to skip if this Python cannot load the build's libpq.

    The framework loads libpq in-process via ctypes, so the interpreter and
    the library must share an ABI.  The common mismatch is a 64-bit Python
    against a 32-bit libpq (meson's ``-m32`` build), which otherwise fails
    every test with ``OSError: wrong ELF class``.  Detect it by reading the
    library's ELF header rather than dlopen()ing it -- a trial dlopen of an
    ASan-instrumented libpq would abort the process, not raise.  Returns None
    when the ABI matches, when libpq cannot be located (the normal load path
    reports that), or when the file is not ELF (macOS/Windows).
    """
    try:
        if libdir:
            path = find_lib_or_die("pq", libpath=[libdir], systempath=False)
        else:
            path = find_lib_or_die("pq", systempath=True)
    except RuntimeError:
        return None

    elf_class = _elf_class(path)
    if elf_class is None:
        return None

    py_bits = ctypes.sizeof(ctypes.c_void_p) * 8
    lib_bits = 64 if elf_class == 2 else 32
    if py_bits != lib_bits:
        return (
            f"{py_bits}-bit Python cannot load {lib_bits}-bit libpq ({path}); "
            f"the in-process libpq framework needs a {lib_bits}-bit interpreter"
        )
    return None


def _elf_class(path):
    """Return 1 (ELFCLASS32), 2 (ELFCLASS64), or None if *path* is not ELF."""
    try:
        with open(path, "rb") as fh:
            ident = fh.read(5)
    except OSError:
        return None
    if ident[:4] != b"\x7fELF":
        return None
    return ident[4]  # e_ident[EI_CLASS]: 1 = 32-bit, 2 = 64-bit


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
    search_paths = _with_windows_bindir(search_paths)

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


def _with_windows_bindir(paths):
    """On Windows, add the sibling ``bin`` of each search directory.

    The runtime DLL is installed in ``bin`` there, while ``lib`` (which is what
    ``pg_config --libdir`` reports) holds only the import library.  Elsewhere
    the list is returned unchanged.
    """
    if sys.platform not in ("win32", "cygwin"):
        return paths
    expanded = []
    for directory in paths:
        if directory not in expanded:
            expanded.append(directory)
        sibling_bin = os.path.join(os.path.dirname(directory.rstrip("\\/")), "bin")
        if sibling_bin not in expanded:
            expanded.append(sibling_bin)
    return expanded


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
