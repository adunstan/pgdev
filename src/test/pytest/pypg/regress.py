# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Run the core SQL regression suite (pg_regress) against a running node.

A handful of tests that exercise the full regression suite -- e.g. the
streaming-regression recovery test and the pg_upgrade test -- shell out to the
pg_regress C driver rather than running SQL through the in-process Session.
This helper builds that command line from the PG_REGRESS / REGRESS_SHLIB
environment variables the meson harness supplies (see test_env in meson.build).
"""

import os
import shlex
import subprocess

from pypg.command import CommandResult


def pg_regress_available():
    """True if PG_REGRESS is set (the build provides the pg_regress driver)."""
    return bool(os.environ.get("PG_REGRESS"))


def run_pg_regress(node, *, inputdir, outputdir, schedule=None, tests=None,
                   dlpath=None, bindir="", max_concurrent_tests=None,
                   extra_opts=None, extra_args=None):
    """Run pg_regress against *node* and return its CommandResult.

    *inputdir* holds the sql/ and expected/ trees; *outputdir* receives
    results/.  Pass either a *schedule* file or an explicit list of *tests*.
    *dlpath* defaults to the directory of REGRESS_SHLIB (where regress.so
    lives).  EXTRA_REGRESS_OPTS from the environment is honored.  The caller
    asserts on the returncode.
    """
    pg_regress = os.environ.get("PG_REGRESS")
    if not pg_regress:
        raise RuntimeError("PG_REGRESS is not set; pg_regress is unavailable")

    if dlpath is None:
        shlib = os.environ.get("REGRESS_SHLIB")
        dlpath = os.path.dirname(shlib) if shlib else "."

    argv = [pg_regress]
    # EXTRA_REGRESS_OPTS is split on whitespace.
    argv += shlex.split(os.environ.get("EXTRA_REGRESS_OPTS", ""))
    if extra_opts:
        argv += list(extra_opts)
    argv += [
        f"--dlpath={dlpath}",
        f"--bindir={bindir}",
        f"--host={node.host}",
        f"--port={node.port}",
        f"--inputdir={inputdir}",
        f"--outputdir={outputdir}",
    ]
    if max_concurrent_tests is not None:
        argv.append(f"--max-concurrent-tests={max_concurrent_tests}")
    if schedule is not None:
        argv.append(f"--schedule={schedule}")
    if extra_args:
        argv += list(extra_args)
    if tests:
        argv += list(tests)

    os.makedirs(outputdir, exist_ok=True)
    print("# Running pg_regress: " + " ".join(argv))
    proc = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)
