# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test the extension_control_path GUC."""

import os


def _create_extension(ext_dir, ext_name, directory=None):
    """Write a .control and a --1.0.sql file for *ext_name* under *ext_dir*."""
    control_file = os.path.join(ext_dir, "extension", f"{ext_name}.control")
    if directory is not None:
        sql_file = os.path.join(ext_dir, directory, f"{ext_name}--1.0.sql")
    else:
        sql_file = os.path.join(ext_dir, "extension", f"{ext_name}--1.0.sql")

    with open(control_file, "w", encoding="utf-8") as cf:
        cf.write("comment = 'Test extension_control_path'\n")
        cf.write("default_version = '1.0'\n")
        cf.write("relocatable = true\n")
        if directory is not None:
            cf.write(f"directory = {directory}")

    with open(sql_file, "w", encoding="utf-8") as sqlf:
        sqlf.write(f"/* {sql_file} */\n")
        sqlf.write(
            "-- complain if script is sourced in psql, rather than via "
            "CREATE EXTENSION\n"
        )
        sqlf.write(
            f'\\echo Use "CREATE EXTENSION {ext_name}" to load this file. \\quit\n'
        )


def test_extension_control_path(create_pg, tmp_path):
    # create_pg runs initdb; start=False so we can append config first.
    node = create_pg("node", start=False)

    # Create temporary directories for the extension control files
    ext_dir = str(tmp_path / "ext_dir")
    os.makedirs(os.path.join(ext_dir, "extension"))
    ext_dir2 = str(tmp_path / "ext_dir2")
    os.makedirs(os.path.join(ext_dir2, "extension"))

    ext_name = "test_custom_ext_paths"
    _create_extension(ext_dir, ext_name)
    _create_extension(ext_dir2, ext_name)

    ext_name2 = "test_custom_ext_paths_using_directory"
    os.makedirs(os.path.join(ext_dir, ext_name2))
    _create_extension(ext_dir, ext_name2, directory=ext_name2)

    # Unix-only port: canonicalized path equals the directory itself, and the
    # path separator is ":".
    ext_dir_canonicalized = ext_dir
    sep = ":"

    node.append_conf(
        f"extension_control_path = '$system{sep}{ext_dir}{sep}{ext_dir2}'\n"
    )

    # Start node
    node.start()

    # Create a user to test permissions to read extension locations.
    user = "user01"
    node.safe_sql(f"CREATE USER {user}")

    ecp = node.safe_sql("show extension_control_path;")
    assert ecp == f"$system{sep}{ext_dir}{sep}{ext_dir2}", (
        "custom extension control directory path configured"
    )

    node.safe_sql(f"CREATE EXTENSION {ext_name}")
    node.safe_sql(f"CREATE EXTENSION {ext_name2}")

    ret = node.safe_sql(
        f"select * from pg_available_extensions where name = '{ext_name}'"
    )
    assert ret == (
        f"test_custom_ext_paths|1.0|1.0|{ext_dir_canonicalized}/extension|"
        "Test extension_control_path"
    ), "extension is shown correctly in pg_available_extensions"

    ret = node.safe_sql(
        f"select * from pg_available_extension_versions where name = '{ext_name}'"
    )
    assert ret == (
        f"test_custom_ext_paths|1.0|t|t|f|t|||{ext_dir_canonicalized}/extension|"
        "Test extension_control_path"
    ), "extension is shown correctly in pg_available_extension_versions"

    ret = node.safe_sql(
        f"select * from pg_available_extensions where name = '{ext_name2}'"
    )
    assert ret == (
        f"test_custom_ext_paths_using_directory|1.0|1.0|"
        f"{ext_dir_canonicalized}/extension|Test extension_control_path"
    ), "extension is shown correctly in pg_available_extensions"

    ret = node.safe_sql(
        "select * from pg_available_extension_versions "
        f"where name = '{ext_name2}'"
    )
    assert ret == (
        f"test_custom_ext_paths_using_directory|1.0|t|t|f|t|||"
        f"{ext_dir_canonicalized}/extension|Test extension_control_path"
    ), "extension is shown correctly in pg_available_extension_versions"

    # Test that a non-superuser is not able to read the extension location in
    # pg_available_extensions
    user_sess = node.connect(user=user)
    try:
        ret = user_sess.query_safe(
            "select location from pg_available_extensions "
            f"where name = '{ext_name2}'"
        )
        assert ret == "<insufficient privilege>", (
            "extension location is hidden in pg_available_extensions for users "
            "with insufficient privilege"
        )

        # Test that a non-superuser is not able to read the extension location in
        # pg_available_extension_versions
        ret = user_sess.query_safe(
            "select location from pg_available_extension_versions "
            f"where name = '{ext_name2}'"
        )
        assert ret == "<insufficient privilege>", (
            "extension location is hidden in pg_available_extension_versions for "
            "users with insufficient privilege"
        )
    finally:
        user_sess.close()

    # Ensure that extensions installed in $system are still visible when used
    # with custom extension control path.
    ret = node.safe_sql(
        "select count(*) > 0 as ok from pg_available_extensions "
        "where name = 'plpgsql'"
    )
    assert ret == "t", (
        "$system extension is shown correctly in pg_available_extensions"
    )

    ret = node.safe_sql(
        "set extension_control_path = ''; "
        "select location from pg_available_extensions where name = 'plpgsql'"
    )
    assert ret == "$system", (
        "$system location is shown correctly in pg_available_extensions with "
        "empty extension_control_path"
    )

    # Test with an extension that does not exist
    res = node.sql("CREATE EXTENSION invalid")
    assert res.error_message is not None, (
        "error creating an extension that does not exist"
    )
    assert 'extension "invalid" is not available' in res.error_message
