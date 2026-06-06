# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests for include directives in HBA and ident files.

This test can only run with Unix-domain sockets; this framework is always
Unix-socket-only, so no skip is needed.

It is largely a data-driven test: include files and trees are written into the
data directory, then the pg_hba_file_rules() and pg_ident_file_mappings()
system views are inspected.  add_hba_line()/add_ident_line() build the expected
view output as each entry is written.
"""

import os


# Stores the number of lines created for each file.  "hba_rule" and
# "ident_rule" track pg_hba_file_rules.rule_number and
# pg_ident_file_mappings.map_number, the global counters tracking the priority
# of each entry processed.
line_counters = {"hba_rule": 0, "ident_rule": 0}


def _basename(path):
    return os.path.basename(path)


# Add some data to the given HBA configuration file, generating the contents
# expected to match pg_hba_file_rules.
#
# Maintains line_counters, used to generate the catalog output for file lines
# and rule numbers.
#
# If the entry starts with "include", the function does not increase the
# general hba rule number as an include directive generates no data in
# pg_hba_file_rules.
#
# Returns the entry of pg_hba_file_rules expected when this is loaded by the
# backend.
def add_hba_line(node, filename, entry):
    # Append the entry to the given file
    node.append_conf(entry, filename=filename)

    base_filename = _basename(filename)

    # Get the current line_counters for the file.
    line_counters[filename] = line_counters.get(filename, 0) + 1
    fileline = line_counters[filename]

    # Include directive, that does not generate a view entry.
    if entry.startswith("include"):
        return ""

    # Increment pg_hba_file_rules.rule_number and save it.
    line_counters["hba_rule"] += 1
    globline = line_counters["hba_rule"]

    # Generate the expected pg_hba_file_rules line
    tokens = entry.split(" ")
    tokens[1] = "{" + tokens[1] + "}"  # database
    tokens[2] = "{" + tokens[2] + "}"  # user_name

    # Append empty options and error
    tokens.append("")
    tokens.append("")

    # Final line expected, output of the SQL query.
    line = ""
    if globline > 1:
        line += "\n"
    line += f"{globline}|{base_filename}|{fileline}|"
    line += "|".join(tokens)

    return line


# Add some data to the given ident configuration file, generating the contents
# expected to match pg_ident_file_mappings.
#
# Works pretty much the same as add_hba_line() above, except that it returns an
# entry to match pg_ident_file_mappings.
def add_ident_line(node, filename, entry):
    base_filename = _basename(filename)

    # Append the entry to the given file
    node.append_conf(entry, filename=filename)

    # Get the current line_counters counter for the file
    line_counters[filename] = line_counters.get(filename, 0) + 1
    fileline = line_counters[filename]

    # Include directive, that does not generate a view entry.
    if entry.startswith("include"):
        return ""

    # Increment pg_ident_file_mappings.map_number and get it.
    line_counters["ident_rule"] += 1
    globline = line_counters["ident_rule"]

    # Generate the expected pg_ident_file_mappings line
    tokens = entry.split(" ")
    # Append empty error
    tokens.append("")

    # Final line expected, output of the SQL query.
    line = ""
    if globline > 1:
        line += "\n"
    line += f"{globline}|{base_filename}|{fileline}|"
    line += "|".join(tokens)

    return line


def test_004_file_inclusion(create_pg):
    # Locations for the entry points of the HBA and ident files.
    hba_file = "subdir1/pg_hba_custom.conf"
    ident_file = "subdir2/pg_ident_custom.conf"

    node = create_pg("primary")

    data_dir = node.data_dir

    # Generating HBA structure with include directives

    hba_expected = ""
    ident_expected = ""

    # customise main auth file names
    node.safe_sql(f"ALTER SYSTEM SET hba_file = '{data_dir}/{hba_file}'")
    node.safe_sql(f"ALTER SYSTEM SET ident_file = '{data_dir}/{ident_file}'")

    # Remove the original ones, this node links to non-default ones now.
    os.unlink(os.path.join(data_dir, "pg_hba.conf"))
    os.unlink(os.path.join(data_dir, "pg_ident.conf"))

    # Generate HBA contents with include directives.
    os.mkdir(os.path.join(data_dir, "subdir1"))
    os.mkdir(os.path.join(data_dir, "hba_inc"))
    os.mkdir(os.path.join(data_dir, "hba_inc_if"))
    os.mkdir(os.path.join(data_dir, "hba_pos"))

    # First, make sure that we will always be able to connect.
    hba_expected += add_hba_line(node, hba_file, "local all all trust")

    # "include".  Note that as hba_file is located in data_dir/subdir1,
    # pg_hba_pre.conf is located at the root of the data directory.
    hba_expected += add_hba_line(node, hba_file, "include ../pg_hba_pre.conf")
    hba_expected += add_hba_line(node, "pg_hba_pre.conf", "local pre all reject")
    hba_expected += add_hba_line(node, hba_file, "local all all reject")
    add_hba_line(node, hba_file, "include ../hba_pos/pg_hba_pos.conf")
    hba_expected += add_hba_line(
        node, "hba_pos/pg_hba_pos.conf", "local pos all reject"
    )
    # When an include directive refers to a relative path, it is compiled from
    # the base location of the file loaded from.
    hba_expected += add_hba_line(
        node, "hba_pos/pg_hba_pos.conf", "include pg_hba_pos2.conf"
    )
    hba_expected += add_hba_line(
        node, "hba_pos/pg_hba_pos2.conf", "local pos2 all reject"
    )
    hba_expected += add_hba_line(
        node, "hba_pos/pg_hba_pos2.conf", "local pos3 all reject"
    )

    # include_if_exists data, nothing generated for the catalog.
    # Missing file, no catalog entries.
    hba_expected += add_hba_line(
        node, hba_file, "include_if_exists ../hba_inc_if/none"
    )
    # File with some contents loaded.
    hba_expected += add_hba_line(
        node, hba_file, "include_if_exists ../hba_inc_if/some"
    )
    hba_expected += add_hba_line(node, "hba_inc_if/some", "local if_some all reject")

    # include_dir
    hba_expected += add_hba_line(node, hba_file, "include_dir ../hba_inc")
    hba_expected += add_hba_line(node, "hba_inc/01_z.conf", "local dir_z all reject")
    hba_expected += add_hba_line(node, "hba_inc/02_a.conf", "local dir_a all reject")
    # Garbage file not suffixed by .conf, so it will be ignored.
    node.append_conf("should not be included", filename="hba_inc/garbageconf")

    # Authentication file expanded in an existing entry for database names.
    # As it is expanded, ignore the output generated.
    add_hba_line(node, hba_file, "local @../dbnames.conf all reject")
    node.append_conf("db1", filename="dbnames.conf")
    node.append_conf("db3", filename="dbnames.conf")
    hba_expected += (
        "\n"
        + str(line_counters["hba_rule"])
        + "|"
        + _basename(hba_file)
        + "|"
        + str(line_counters[hba_file])
        + "|local|{db1,db3}|{all}|reject||"
    )

    # Generating ident structure with include directives

    os.mkdir(os.path.join(data_dir, "subdir2"))
    os.mkdir(os.path.join(data_dir, "ident_inc"))
    os.mkdir(os.path.join(data_dir, "ident_inc_if"))
    os.mkdir(os.path.join(data_dir, "ident_pos"))

    # include.  Note that pg_ident_pre.conf is located at the root of the data
    # directory.
    ident_expected += add_ident_line(
        node, ident_file, "include ../pg_ident_pre.conf"
    )
    ident_expected += add_ident_line(node, "pg_ident_pre.conf", "pre foo bar")
    ident_expected += add_ident_line(node, ident_file, "test a b")
    ident_expected += add_ident_line(
        node, ident_file, "include ../ident_pos/pg_ident_pos.conf"
    )
    ident_expected += add_ident_line(
        node, "ident_pos/pg_ident_pos.conf", "pos foo bar"
    )
    # When an include directive refers to a relative path, it is compiled from
    # the base location of the file loaded from.
    ident_expected += add_ident_line(
        node, "ident_pos/pg_ident_pos.conf", "include pg_ident_pos2.conf"
    )
    ident_expected += add_ident_line(
        node, "ident_pos/pg_ident_pos2.conf", "pos2 foo bar"
    )
    ident_expected += add_ident_line(
        node, "ident_pos/pg_ident_pos2.conf", "pos3 foo bar"
    )

    # include_if_exists
    # Missing file, no catalog entries.
    ident_expected += add_ident_line(
        node, ident_file, "include_if_exists ../ident_inc_if/none"
    )
    # File with some contents loaded.
    ident_expected += add_ident_line(
        node, ident_file, "include_if_exists ../ident_inc_if/some"
    )
    ident_expected += add_ident_line(node, "ident_inc_if/some", "if_some foo bar")

    # include_dir
    ident_expected += add_ident_line(node, ident_file, "include_dir ../ident_inc")
    ident_expected += add_ident_line(node, "ident_inc/01_z.conf", "dir_z foo bar")
    ident_expected += add_ident_line(node, "ident_inc/02_a.conf", "dir_a foo bar")
    # Garbage file not suffixed by .conf, so it will be ignored.
    node.append_conf("should not be included", filename="ident_inc/garbageconf")

    node.restart()

    # Note that the base path is filtered out, keeping only the file name to
    # bypass portability issues.  The configuration files had better have
    # unique names.
    contents = node.safe_sql(
        "SELECT rule_number,\n"
        "  regexp_replace(file_name, '.*/', ''),\n"
        "  line_number,\n"
        "  type,\n"
        "  database,\n"
        "  user_name,\n"
        "  auth_method,\n"
        "  options,\n"
        "  error\n"
        " FROM pg_hba_file_rules ORDER BY rule_number;"
    )
    assert contents == hba_expected, "check contents of pg_hba_file_rules"

    contents = node.safe_sql(
        "SELECT map_number,\n"
        "  regexp_replace(file_name, '.*/', ''),\n"
        "  line_number,\n"
        "  map_name,\n"
        "  sys_name,\n"
        "  pg_username,\n"
        "  error\n"
        " FROM pg_ident_file_mappings ORDER BY map_number"
    )
    assert contents == ident_expected, "check contents of pg_ident_file_mappings"
