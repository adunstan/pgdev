# Copyright (c) 2026, PostgreSQL Global Development Group

"""Tests pg_upgrade with extensions found via extension_control_path."""

import os
import re
import shutil

import pytest


def _create_extension_files(ext_name, ext_dir):
    """Write .control and --1.0.sql files into ``ext_dir/extension/``.

    ``module_pathname`` contains the ``$libdir/`` prefix to simulate most
    extensions that use it by default in module_pathname.
    """
    control_file = os.path.join(ext_dir, "extension", f"{ext_name}.control")
    with open(control_file, "w", encoding="utf-8") as cf:
        cf.write(
            "comment = 'Test C extension for pg_upgrade + "
            "extension_control_path'\n"
        )
        cf.write("default_version = '1.0'\n")
        cf.write(f"module_pathname = '$libdir/{ext_name}'\n")
        cf.write("relocatable = true\n")

    sql_file = os.path.join(ext_dir, "extension", f"{ext_name}--1.0.sql")
    with open(sql_file, "w", encoding="utf-8") as sqlf:
        sqlf.write(f"/* {ext_name}--1.0.sql */\n")
        sqlf.write(
            "-- complain if script is sourced in psql, rather than via "
            "CREATE EXTENSION\n"
        )
        sqlf.write(
            f'\\echo Use "CREATE EXTENSION {ext_name}" to load this file. '
            "\\quit\n"
        )
        sqlf.write("CREATE FUNCTION test_ext()\n")
        sqlf.write("RETURNS void AS 'MODULE_PATHNAME'\n")
        sqlf.write("LANGUAGE C;\n")


def test_extension_control_path(create_pg, tmp_path):
    # Make sure the extension .so path is provided.
    ext_lib_so = os.environ.get("TEST_EXT_LIB")
    if not ext_lib_so:
        pytest.skip("couldn't get the extension so path (TEST_EXT_LIB not set)")

    # Create the custom extension directory layout:
    #   ext_dir/extension/  -- .control and .sql files
    #   ext_dir/lib/        -- .so file
    ext_dir = str(tmp_path / "ext_dir")
    os.makedirs(os.path.join(ext_dir, "extension"))
    os.makedirs(os.path.join(ext_dir, "lib"))
    ext_lib = os.path.join(ext_dir, "lib")

    # Copy the .so file into the lib/ subdirectory.
    shutil.copy(ext_lib_so, ext_lib)

    _create_extension_files("test_ext", ext_dir)

    # Unix-only port: path separator is ":".
    sep = ":"
    ext_path = ext_dir
    ext_lib_path = ext_lib

    extension_control_path_conf = (
        f"\nextension_control_path = '$system{sep}{ext_path}'\n"
        f"dynamic_library_path = '$libdir{sep}{ext_lib_path}'\n"
    )

    old = create_pg("old", start=False)

    # Configure extension_control_path so the .control file is found in our
    # extension/ directory, and dynamic_library_path so the .so is found in
    # lib/.
    old.append_conf(extension_control_path_conf)

    old.start()

    # CREATE EXTENSION 'test_ext'
    old.safe_sql("CREATE EXTENSION test_ext")

    # Verify the extension works before the upgrade.
    sess = old.session()
    sess.clear_notices()
    res = sess.query("SELECT test_ext()")
    assert res.error_message is None, "extension works before upgrade"
    assert re.search(r"NOTICE:  running successful", sess.get_notices_str()), \
        "extension working"

    old.stop()

    new = create_pg("new", start=False)

    # Pre-configure the new cluster with dynamic_library_path and
    # extension_control_path before running pg_upgrade.
    new.append_conf(extension_control_path_conf)

    # In a VPATH build, we'll be started in the source directory, but we want
    # to run pg_upgrade in the build directory so that any generated files
    # finish there, like delete_old_cluster.{sh,bat}.  Run from a tmp cwd.
    run_cwd = str(tmp_path / "run")
    os.makedirs(run_cwd)
    old_cwd = os.getcwd()
    os.chdir(run_cwd)
    try:
        new.command_ok(
            [
                "pg_upgrade", "--no-sync",
                "--old-datadir", old.data_dir,
                "--new-datadir", new.data_dir,
                "--old-bindir", old.bindir,
                "--new-bindir", new.bindir,
                "--socketdir", new.host,
                "--old-port", str(old.port),
                "--new-port", str(new.port),
                "--copy",
            ],
            "pg_upgrade succeeds with extension installed via "
            "extension_control_path",
        )
    finally:
        os.chdir(old_cwd)

    new.start()

    # Verify the extension still works after the upgrade.
    sess = new.session()
    sess.clear_notices()
    res = sess.query("SELECT test_ext()")
    assert res.error_message is None, "extension works after upgrade"
    assert re.search(r"NOTICE:  running successful", sess.get_notices_str()), \
        "extension working"

    new.stop()
