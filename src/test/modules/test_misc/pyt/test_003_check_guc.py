# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Cross-check the consistency of GUC parameters with postgresql.conf.sample."""

import os
import re


def test_003_check_guc(create_pg):
    node = create_pg("main")

    # Grab the names of all the parameters that can be listed in the
    # configuration sample file.  config_file is an exception, it is not
    # in postgresql.conf.sample but is part of the lists from guc_tables.c.
    # Custom GUCs loaded by extensions are excluded.
    all_params = node.safe_sql(
        "SELECT name\n"
        "     FROM pg_settings\n"
        "   WHERE NOT 'NOT_IN_SAMPLE' = ANY (pg_settings_get_flags(name)) AND\n"
        "       name <> 'config_file' AND category <> 'Customized Options'\n"
        "     ORDER BY 1")
    # Note the lower-case conversion, for consistency.
    all_params_array = all_params.lower().split("\n")

    # Grab the names of all parameters marked as NOT_IN_SAMPLE.
    not_in_sample = node.safe_sql(
        "SELECT name\n"
        "     FROM pg_settings\n"
        "   WHERE 'NOT_IN_SAMPLE' = ANY (pg_settings_get_flags(name))\n"
        "     ORDER BY 1")
    not_in_sample_array = not_in_sample.lower().split("\n")

    # use the sample file from the temp install
    share_dir = node.pg_bin.result(["pg_config", "--sharedir"]).stdout.strip()
    sample_file = os.path.join(share_dir, "postgresql.conf.sample")

    # List of all the GUCs found in the sample file.
    gucs_in_file = []

    # List of all lines with tabs in the sample file.
    lines_with_tabs = []

    # Read the sample file line-by-line, checking its contents to build a list
    # of everything known as a GUC.
    line_num = 0
    with open(sample_file, encoding="utf-8") as contents:
        for line in contents:
            line_num += 1
            if "\t" in line:
                lines_with_tabs.append(line_num)

            # Check if this line matches a GUC parameter:
            # - Each parameter is preceded by "#", but not "# " in the sample
            # file.
            # - Valid configuration options are followed immediately by " = ",
            # with one space before and after the equal sign.
            m = re.match(r"^#(\w+) = .*", line)
            if m:
                # Lower-case conversion matters for some of the GUCs.
                param_name = m.group(1).lower()

                # Ignore some exceptions.
                if param_name in ("include", "include_dir", "include_if_exists"):
                    continue

                # Update the list of GUCs found in the sample file, for the
                # follow-up tests.
                gucs_in_file.append(param_name)

                continue
            # Make sure each line starts with either a # or whitespace
            assert not re.match(r"^\s*[^#\s]", line), \
                f"{line} missing initial # in postgresql.conf.sample"

    # Cross-check that all the GUCs found in the sample file match the ones
    # fetched above.  This maps the arrays to a set, making the creation of
    # each exclude and intersection list easier.
    gucs_in_file_set = set(gucs_in_file)
    all_params_set = set(all_params_array)
    not_in_sample_set = set(not_in_sample_array)

    missing_from_file = [p for p in all_params_array if p not in gucs_in_file_set]
    missing_from_list = [p for p in gucs_in_file if p not in all_params_set]
    sample_intersect = [p for p in gucs_in_file if p in not_in_sample_set]

    # These would log some information only on errors.
    for param in missing_from_file:
        print(
            f"found GUC {param} in guc_tables.c, missing from "
            "postgresql.conf.sample")
    for param in missing_from_list:
        print(
            f"found GUC {param} in postgresql.conf.sample, with incorrect "
            "info in guc_tables.c")
    for param in sample_intersect:
        print(
            f"found GUC {param} in postgresql.conf.sample, marked as "
            "NOT_IN_SAMPLE")
    for param in lines_with_tabs:
        print(f"found tab in line {param} in postgresql.conf.sample")

    assert len(missing_from_file) == 0, \
        "no parameters missing from postgresql.conf.sample"
    assert len(missing_from_list) == 0, \
        "no parameters missing from guc_tables.c"
    assert len(sample_intersect) == 0, \
        "no parameters marked as NOT_IN_SAMPLE in postgresql.conf.sample"
    assert len(lines_with_tabs) == 0, \
        "no lines with tabs in postgresql.conf.sample"
