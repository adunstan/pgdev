# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests connection-string handling in pg_dump / pg_dumpall / pg_restore.

Exercises connection-string handling in pg_dump, pg_dumpall and pg_restore
against databases and roles whose names contain the full range of LATIN1
characters (control characters, quotes, spaces and high-bit bytes).

pg_dump / pg_dumpall / pg_restore (and psql, for the restore-through-psql
steps) are the programs under test and are run as subprocesses.  The names use
byte sequences that aren't valid UTF-8, so the tools are run with
PGCLIENTENCODING=LATIN1 and the argv is encoded as raw LATIN1 bytes; the
framework's str-based command helpers would mangle the high-bit bytes through
their UTF-8 encoding, so a small bytes-aware runner is used instead.

The SQL that creates the databases and roles is run in-process via the node's
libpq session with client_encoding=UTF8: a Python code point U+00xx is sent as
UTF-8 and converted by the server to the matching LATIN1 byte, so the names
round-trip to exactly the intended bytes.
"""

import os
import subprocess

import pytest

from libpq import Session


def _generate_ascii_string(from_char, to_char):
    """Build a string spanning a range of byte values.

    Build a string from the given inclusive range of byte values, mapping each
    byte to the same Unicode code point (Latin-1 semantics).
    """
    return "".join(chr(i) for i in range(from_char, to_char + 1))


def _quote_ident(name):
    """Double-quote an SQL identifier, doubling embedded double quotes."""
    return '"' + name.replace('"', '""') + '"'


def _connstr_dbname(host, port, dbname):
    """Build a libpq connection string targeting *dbname*.

    The dbname is single-quoted with backslashes and single quotes escaped per
    libpq connection-string rules.  host/port are supplied explicitly rather
    than reused from node.connstr() so the dbname quoting is under our control.
    """
    quoted = dbname.replace("\\", "\\\\").replace("'", "\\'")
    return f"host='{host}' port={port} dbname='{quoted}'"


def _run_bytes(node, argv, msg, extra_env=None, check=True):
    """Run *argv* (a list of str) as a program under test, LATIN1-encoded.

    Mirrors node.command_ok(), but encodes every argv element as raw LATIN1
    bytes so high-bit and control characters in database/role names survive
    intact (the str-based helpers would re-encode them as UTF-8).  cmd[0] is
    resolved within the node's bindir.  With *check* (the default) a non-zero
    exit raises, like command_ok; the completed process is returned.
    """
    prog = argv[0]
    candidate = os.path.join(node.bindir, prog)
    if os.path.exists(candidate):
        prog = candidate

    env = dict(os.environ)
    env["PGHOST"] = node.host
    env["PGPORT"] = str(node.port)
    env["PGDATABASE"] = "postgres"
    env["LC_ALL"] = "C"
    env["PGCLIENTENCODING"] = "LATIN1"
    if node.libdir:
        env["LD_LIBRARY_PATH"] = node.libdir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    if extra_env:
        env.update(extra_env)
    env = {k: v for k, v in env.items() if v is not None}

    bargv = [prog.encode("latin-1")] + [a.encode("latin-1") for a in argv[1:]]
    print("# Running: " + " ".join(argv[:1] + [repr(a) for a in argv[1:]]))
    proc = subprocess.run(bargv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr = proc.stderr.decode("latin-1", "replace")
    if check:
        assert proc.returncode == 0, (
            f"{msg}\nexit code: {proc.returncode}\nstderr:\n{stderr}"
        )
    return proc


def test_dump_connstr(create_pg):
    src_bootstrap_super = "regress_postgres"
    dst_bootstrap_super = "boot"

    # Create database and user names covering the range of LATIN1 characters,
    # for use in a connection string by pg_dumpall.  Skip ',' because of
    # pg_regress --create-role, skip [\n\r] because pg_dumpall does not allow
    # them.  We also skip many ASCII letters, to keep the total number of
    # tested characters to what will fit in four names.
    dbname1 = (
        "regression"
        + _generate_ascii_string(1, 9)
        + _generate_ascii_string(11, 12)
        + _generate_ascii_string(14, 33)
        + '"x"'
        + _generate_ascii_string(35, 43)  # skip ','
        + _generate_ascii_string(45, 54)
    )
    dbname2 = (
        "regression"
        + _generate_ascii_string(55, 65)  # skip 'B'-'W'
        + _generate_ascii_string(88, 99)  # skip 'd'-'w'
        + _generate_ascii_string(120, 149)
    )
    dbname3 = "regression" + _generate_ascii_string(150, 202)
    dbname4 = "regression" + _generate_ascii_string(203, 255)

    username1 = "regress_" + dbname1[len("regression"):]
    username2 = "regress_" + dbname2[len("regression"):]
    username3 = "regress_" + dbname3[len("regression"):]
    username4 = "regress_" + dbname4[len("regression"):]

    dbnames = [dbname1, dbname2, dbname3, dbname4]
    usernames = [username1, username2, username3, username4]

    node = create_pg(
        "main",
        start=False,
        initdb_extra=[
            "--username", src_bootstrap_super,
            "--locale", "C",
            "--encoding", "LATIN1",
        ],
    )
    node.start()

    backupdir = node.backup_dir
    discard = os.path.join(backupdir, "discard.sql")
    plain = os.path.join(backupdir, "plain.sql")
    dirfmt = os.path.join(backupdir, "dirfmt")

    # Create the databases and superuser roles via libpq with an UTF8 client
    # encoding: each code point is converted by the server to the matching
    # LATIN1 byte, so the stored names are exactly the byte sequences above.
    # CREATE DATABASE cannot run inside a transaction block, so each statement
    # is issued on its own.
    admin = Session(
        connstr=_connstr_dbname(node.host, node.port, "postgres")
        + f" client_encoding=UTF8 user='{src_bootstrap_super}'",
        libdir=node.libdir,
    )
    try:
        for dbname, username in zip(dbnames, usernames):
            admin.query_safe("CREATE DATABASE " + _quote_ident(dbname))
            admin.query_safe(
                "CREATE ROLE " + _quote_ident(username) + " SUPERUSER LOGIN"
            )
    finally:
        admin.close()

    # For these tests, pg_dumpall --roles-only is used because it produces a
    # short dump.  Each long ASCII name is reached through a connection string.
    _run_bytes(
        node,
        [
            "pg_dumpall", "--roles-only",
            "--file", discard,
            "--dbname", _connstr_dbname(node.host, node.port, dbname1),
            "--username", username4,
        ],
        "pg_dumpall with long ASCII name 1",
    )
    _run_bytes(
        node,
        [
            "pg_dumpall", "--no-sync", "--roles-only",
            "--file", discard,
            "--dbname", _connstr_dbname(node.host, node.port, dbname2),
            "--username", username3,
        ],
        "pg_dumpall with long ASCII name 2",
    )
    _run_bytes(
        node,
        [
            "pg_dumpall", "--no-sync", "--roles-only",
            "--file", discard,
            "--dbname", _connstr_dbname(node.host, node.port, dbname3),
            "--username", username2,
        ],
        "pg_dumpall with long ASCII name 3",
    )
    _run_bytes(
        node,
        [
            "pg_dumpall", "--no-sync", "--roles-only",
            "--file", discard,
            "--dbname", _connstr_dbname(node.host, node.port, dbname4),
            "--username", username1,
        ],
        "pg_dumpall with long ASCII name 4",
    )
    _run_bytes(
        node,
        [
            "pg_dumpall", "--no-sync", "--roles-only",
            "--username", src_bootstrap_super,
            "--dbname", "dbname=template1",
        ],
        "pg_dumpall --dbname accepts connection string",
    )

    # make a table, so the parallel worker has something to dump.  dbname1
    # contains a single quote, so node.connstr() (which does not escape) can't
    # be used; build an explicitly-escaped connection string instead.
    db1_sess = Session(
        connstr=_connstr_dbname(node.host, node.port, dbname1)
        + f" client_encoding=UTF8 user='{src_bootstrap_super}'",
        libdir=node.libdir,
    )
    try:
        db1_sess.query_safe("CREATE TABLE t0()")
    finally:
        db1_sess.close()

    # XXX no printed message when this fails, just SIGPIPE termination
    _run_bytes(
        node,
        [
            "pg_dump",
            "--format", "directory",
            "--no-sync",
            "--jobs", "2",
            "--file", dirfmt,
            "--username", username1,
            _connstr_dbname(node.host, node.port, dbname1),
        ],
        "parallel dump",
    )

    # recreate $dbname1 for restore test
    admin = Session(
        connstr=_connstr_dbname(node.host, node.port, "postgres")
        + f" client_encoding=UTF8 user='{src_bootstrap_super}'",
        libdir=node.libdir,
    )
    try:
        admin.query_safe("DROP DATABASE " + _quote_ident(dbname1))
        admin.query_safe("CREATE DATABASE " + _quote_ident(dbname1))
    finally:
        admin.close()

    _run_bytes(
        node,
        [
            "pg_restore",
            "--verbose",
            "--dbname", "template1",
            "--jobs", "2",
            "--username", username1,
            dirfmt,
        ],
        "parallel restore",
    )

    admin = Session(
        connstr=_connstr_dbname(node.host, node.port, "postgres")
        + f" client_encoding=UTF8 user='{src_bootstrap_super}'",
        libdir=node.libdir,
    )
    try:
        admin.query_safe("DROP DATABASE " + _quote_ident(dbname1))
    finally:
        admin.close()

    _run_bytes(
        node,
        [
            "pg_restore",
            "--create",
            "--verbose",
            "--dbname", "template1",
            "--jobs", "2",
            "--username", username1,
            dirfmt,
        ],
        "parallel restore with create",
    )

    _run_bytes(
        node,
        [
            "pg_dumpall",
            "--no-sync",
            "--file", plain,
            "--username", username1,
        ],
        "take full dump",
    )

    restore_super = 'regress_a\'b\\c=d\\ne"f'

    # Restore full dump through psql using environment variables for
    # dbname/user connection parameters.
    envar_node = create_pg(
        "destination_envar",
        start=False,
        initdb_extra=[
            "--username", dst_bootstrap_super,
            "--locale", "C",
            "--encoding", "LATIN1",
        ],
    )
    envar_node.start()

    # make superuser for restore
    envar_admin = Session(
        connstr=_connstr_dbname(envar_node.host, envar_node.port, "postgres")
        + f" client_encoding=UTF8 user='{dst_bootstrap_super}'",
        libdir=envar_node.libdir,
    )
    try:
        envar_admin.query_safe(
            "CREATE ROLE " + _quote_ident(restore_super) + " SUPERUSER LOGIN"
        )
    finally:
        envar_admin.close()

    proc = _run_bytes(
        envar_node,
        ["psql", "--no-psqlrc", "--file", plain],
        "restore full dump using environment variables for connection parameters",
        extra_env={
            "PGHOST": envar_node.host,
            "PGPORT": str(envar_node.port),
            "PGUSER": restore_super,
        },
        check=False,
    )
    assert proc.returncode == 0, (
        "restore full dump using environment variables for connection "
        f"parameters\nstderr:\n{proc.stderr.decode('latin-1', 'replace')}"
    )
    assert proc.stderr == b"", (
        "no dump errors\nstderr:\n" + proc.stderr.decode("latin-1", "replace")
    )

    # Restore full dump through psql using command-line options for
    # dbname/user connection parameters.  "\connect dbname=" forgets user/port
    # from command line.
    cmdline_node = create_pg(
        "destination_cmdline",
        start=False,
        initdb_extra=[
            "--username", dst_bootstrap_super,
            "--locale", "C",
            "--encoding", "LATIN1",
        ],
    )
    cmdline_node.start()

    cmdline_admin = Session(
        connstr=_connstr_dbname(cmdline_node.host, cmdline_node.port, "postgres")
        + f" client_encoding=UTF8 user='{dst_bootstrap_super}'",
        libdir=cmdline_node.libdir,
    )
    try:
        cmdline_admin.query_safe(
            "CREATE ROLE " + _quote_ident(restore_super) + " SUPERUSER LOGIN"
        )
    finally:
        cmdline_admin.close()

    proc = _run_bytes(
        cmdline_node,
        [
            "psql",
            "--port", str(cmdline_node.port),
            "--username", restore_super,
            "--no-psqlrc",
            "--file", plain,
        ],
        "restore full dump with command-line options for connection parameters",
        check=False,
    )
    assert proc.returncode == 0, (
        "restore full dump with command-line options for connection "
        f"parameters\nstderr:\n{proc.stderr.decode('latin-1', 'replace')}"
    )
    assert proc.stderr == b"", (
        "no dump errors\nstderr:\n" + proc.stderr.decode("latin-1", "replace")
    )
