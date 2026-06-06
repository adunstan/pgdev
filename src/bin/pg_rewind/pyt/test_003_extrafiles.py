# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test how pg_rewind reacts to extra files and directories in the data dirs.
All the files that were present in the standby should be present after
rewind, and all the files that were added on the primary should be removed.
"""

import os
import platform
import re

import pytest


def append_to_file(path, content):
    """Append *content* to the file at *path*."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(content)


def run_test(rewind, test_mode):
    rewind.setup_cluster(test_mode)
    rewind.start_primary()

    test_primary_datadir = rewind.node_primary.data_dir

    # Create a subdir and files that will be present in both
    os.mkdir(os.path.join(test_primary_datadir, "tst_both_dir"))
    append_to_file(
        os.path.join(test_primary_datadir, "tst_both_dir", "both_file1"), "in both1"
    )
    append_to_file(
        os.path.join(test_primary_datadir, "tst_both_dir", "both_file2"), "in both2"
    )
    os.mkdir(os.path.join(test_primary_datadir, "tst_both_dir", "both_subdir"))
    append_to_file(
        os.path.join(test_primary_datadir, "tst_both_dir", "both_subdir", "both_file3"),
        "in both3",
    )

    rewind.create_standby(test_mode)

    # Create different subdirs and files in primary and standby
    test_standby_datadir = rewind.node_standby.data_dir

    os.mkdir(os.path.join(test_standby_datadir, "tst_standby_dir"))
    append_to_file(
        os.path.join(test_standby_datadir, "tst_standby_dir", "standby_file1"),
        "in standby1",
    )
    append_to_file(
        os.path.join(test_standby_datadir, "tst_standby_dir", "standby_file2"),
        "in standby2",
    )
    append_to_file(
        os.path.join(
            test_standby_datadir, "tst_standby_dir", "standby_file3 with 'quotes'"
        ),
        "in standby3",
    )
    os.mkdir(os.path.join(test_standby_datadir, "tst_standby_dir", "standby_subdir"))
    append_to_file(
        os.path.join(
            test_standby_datadir, "tst_standby_dir", "standby_subdir", "standby_file4"
        ),
        "in standby4",
    )
    # Skip testing .DS_Store files on macOS to avoid risk of side effects
    if platform.system() != "Darwin":
        append_to_file(
            os.path.join(test_standby_datadir, "tst_standby_dir", ".DS_Store"),
            "macOS system file",
        )

    os.mkdir(os.path.join(test_primary_datadir, "tst_primary_dir"))
    append_to_file(
        os.path.join(test_primary_datadir, "tst_primary_dir", "primary_file1"),
        "in primary1",
    )
    append_to_file(
        os.path.join(test_primary_datadir, "tst_primary_dir", "primary_file2"),
        "in primary2",
    )
    os.mkdir(os.path.join(test_primary_datadir, "tst_primary_dir", "primary_subdir"))
    append_to_file(
        os.path.join(
            test_primary_datadir, "tst_primary_dir", "primary_subdir", "primary_file3"
        ),
        "in primary3",
    )

    rewind.promote_standby()
    rewind.run_pg_rewind(test_mode)

    # List files in the data directory after rewind. All the files that
    # were present in the standby should be present after rewind, and
    # all the files that were added on the primary should be removed.
    paths = []
    for root, dirs, files in os.walk(test_primary_datadir):
        for name in dirs + files:
            full = os.path.join(root, name)
            if re.search(r".*tst_.*", full):
                paths.append(full)
    paths = sorted(paths)

    assert paths == [
        os.path.join(test_primary_datadir, "tst_both_dir"),
        os.path.join(test_primary_datadir, "tst_both_dir", "both_file1"),
        os.path.join(test_primary_datadir, "tst_both_dir", "both_file2"),
        os.path.join(test_primary_datadir, "tst_both_dir", "both_subdir"),
        os.path.join(test_primary_datadir, "tst_both_dir", "both_subdir", "both_file3"),
        os.path.join(test_primary_datadir, "tst_standby_dir"),
        os.path.join(test_primary_datadir, "tst_standby_dir", "standby_file1"),
        os.path.join(test_primary_datadir, "tst_standby_dir", "standby_file2"),
        os.path.join(
            test_primary_datadir, "tst_standby_dir", "standby_file3 with 'quotes'"
        ),
        os.path.join(test_primary_datadir, "tst_standby_dir", "standby_subdir"),
        os.path.join(
            test_primary_datadir, "tst_standby_dir", "standby_subdir", "standby_file4"
        ),
    ], "file lists match"

    rewind.clean_rewind_test()


# Run the test in both modes.
@pytest.mark.parametrize("mode", ["local", "remote"])
def test_003_extrafiles(rewind, mode):
    run_test(rewind, mode)
