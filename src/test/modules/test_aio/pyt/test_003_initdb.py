# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test initdb for each IO method. This is done separately from 001_aio, as it
isn't fast. This way the more commonly failing / hacked-on 001_aio can be
iterated on more quickly.
"""

import re


# ---------------------------------------------------------------------------
# AIO test helpers
# ---------------------------------------------------------------------------


def _have_io_uring(pg_bin):
    """Return whether io_uring is a supported io_method.

    To detect if io_uring is supported, we
    look at the error message for assigning an invalid value to an enum GUC,
    which lists all the valid options.  We use -C to deal with running as
    administrator on Windows, as the superuser check is omitted if -C is used.
    """
    res = pg_bin.result(["postgres", "-C", "invalid", "-c", "io_method=invalid"])
    match = re.search(r"Available values: ([^\.]+)\.", res.stderr)
    assert match is not None, "can't determine supported io_method values"
    methods = match.group(1)
    print(f"# supported io_method values are: {methods}")
    return "io_uring" in methods


def _supported_io_methods(pg_bin):
    """Return the list of supported values for the io_method GUC."""
    methods = ["worker"]
    if _have_io_uring(pg_bin):
        methods.append("io_uring")
    # Return sync last, as it will least commonly fail.
    methods.append("sync")
    return methods


def _configure(node):
    """Prepare a cluster for AIO tests."""
    node.append_conf(
        """
shared_preload_libraries=test_aio
log_min_messages = 'DEBUG3'
log_statement=all
log_error_verbosity=default
restart_after_crash=false
temp_buffers=100
"""
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_003_initdb(pg_bin, create_pg):
    # Want to test initdb for each IO method, otherwise we could just reuse
    # the cluster.
    for io_method in _supported_io_methods(pg_bin):
        node = create_pg(
            io_method,
            start=False,
            initdb_extra=["-c", f"io_method={io_method}"],
        )

        _configure(node)

        # Even though we used -c io_method=... above, the test config may
        # override the setting persisted at initdb time.  While using (and
        # later verifying) the setting from initdb provides some verification
        # of having used the io_method during initdb, it's probably not worth
        # the complication of only appending conditionally.
        node.append_conf(f"\nio_method={io_method}\n")

        # ok: initdb succeeded for this io_method
        node.start()
        node.stop()
        # ok: start & stop succeeded for this io_method
