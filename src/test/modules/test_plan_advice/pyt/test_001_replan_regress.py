# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Run the core regression tests under pg_plan_advice to check for problems."""

import os
import subprocess

import pytest


def test_replan_regress(create_pg, tmp_path):
    # Initialize the primary node
    node = create_pg("main", start=False)

    # Set up our desired configuration.
    node.append_conf(
        "shared_preload_libraries='test_plan_advice'\n"
        "wal_level=replica\n"
        "pg_plan_advice.always_explain_supplied_advice=false\n"
        "pg_plan_advice.feedback_warnings=true\n"
    )
    node.start()

    pg_regress = os.environ.get("PG_REGRESS")
    if not pg_regress:
        pytest.skip("PG_REGRESS not set in environment")
    regress_shlib = os.environ.get("REGRESS_SHLIB")
    if not regress_shlib:
        pytest.skip("REGRESS_SHLIB not set in environment")

    # The repository root, relative to this test file
    # (src/test/modules/test_plan_advice/pyt/).
    srcdir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
    )

    # --dlpath is needed to be able to find the location of regress.so
    # and any libraries the regression tests require.
    dlpath = os.path.dirname(regress_shlib)

    # --outputdir points to the path where to place the output files.
    outputdir = str(tmp_path)

    # --inputdir points to the path of the input files.
    inputdir = os.path.join(srcdir, "src", "test", "regress")

    # Run the tests.
    cmd = (
        pg_regress + " "
        "--bindir= "
        f'--dlpath="{dlpath}" '
        f"--host={node.host} "
        f"--port={node.port} "
        f"--schedule={srcdir}/src/test/regress/parallel_schedule "
        "--max-concurrent-tests=20 "
        f'--inputdir="{inputdir}" '
        f'--outputdir="{outputdir}"'
    )
    rc = subprocess.run(cmd, shell=True, check=False).returncode

    # Dump out the regression diffs file, if there is one
    if rc != 0:
        diffs = os.path.join(outputdir, "regression.diffs")
        if os.path.exists(diffs):
            print(f"=== dumping {diffs} ===")
            with open(diffs, "r", encoding="utf-8", errors="replace") as fh:
                print(fh.read())
            print("=== EOF ===")

    # Report results
    assert rc == 0, "regression tests pass"
