# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests to check connection string handling in the utility programs."""


def _generate_ascii_string(from_char, to_char):
    """Build a string from the given inclusive range of byte values, mapping
    each byte to the same Unicode code point (Latin-1 semantics)."""
    return "".join(chr(i) for i in range(from_char, to_char + 1))


def test_connstr_unusual_db_names(create_pg):
    # We're going to use byte sequences that aren't valid UTF-8 strings.  Use
    # LATIN1, which accepts any byte and has a conversion from each byte to
    # UTF-8.  These are applied to the client programs via extra_env.
    extra_env = {"LC_ALL": "C", "PGCLIENTENCODING": "LATIN1"}

    # Create database names covering the range of LATIN1 characters and
    # run the utilities' --all options over them.
    dbname1 = _generate_ascii_string(1, 63)  # contains '='
    dbname2 = _generate_ascii_string(67, 129)  # skip 64-66 to keep length to 62
    dbname3 = _generate_ascii_string(130, 192)
    dbname4 = _generate_ascii_string(193, 255)

    node = create_pg("main", start=False, initdb_extra=["--locale=C", "--encoding=LATIN1"])
    node.start()

    bin = node.pg_bin
    for dbname in (dbname1, dbname2, dbname3, dbname4, "CamelCase"):
        # run_log: run and log, ignoring the exit status.
        bin.result(["createdb", dbname], extra_env=extra_env)

    bin.command_ok(
        ["vacuumdb", "--all", "--echo", "--analyze-only"],
        "vacuumdb --all with unusual database names",
        extra_env=extra_env,
    )
    bin.command_ok(
        ["reindexdb", "--all", "--echo"],
        "reindexdb --all with unusual database names",
        extra_env=extra_env,
    )
    bin.command_ok(
        ["clusterdb", "--all", "--echo", "--verbose"],
        "clusterdb --all with unusual database names",
        extra_env=extra_env,
    )
