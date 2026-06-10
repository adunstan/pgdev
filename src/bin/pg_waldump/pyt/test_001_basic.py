# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Basic tests for pg_waldump: option handling and decoding generated WAL."""

import glob
import os
import random
import re
import shutil
import struct
import subprocess


def _find_tar():
    """Locate a tar program, honoring the TAR environment variable."""
    return os.environ.get("TAR") or shutil.which("tar") or ""


def _tar_portability_options(tar):
    """Return tar flags producing portable, reproducible archives across the
    GNU, BSD, and OpenBSD tar variants."""
    if not tar:
        return []

    devnull = os.devnull
    # GNU tar (Linux), BSD tar (FreeBSD, NetBSD, macOS, Windows)
    rc = subprocess.run(
        f"{tar} --format=ustar --owner=0 --group=0 -cf {devnull} {devnull} "
        f"2>{devnull}",
        shell=True,
    ).returncode
    if rc == 0:
        return ["--format=ustar", "--owner=0", "--group=0"]
    # OpenBSD tar
    rc = subprocess.run(
        f"{tar} -F ustar -cf {devnull} {devnull} 2>{devnull}", shell=True
    ).returncode
    if rc == 0:
        return ["-F", "ustar"]
    return []


def _check_pg_config(pg_config, define):
    """Return True if *define* appears in the installed pg_config.h."""
    include_server = subprocess.run(
        [pg_config, "--includedir-server"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()
    header = os.path.join(include_server, "pg_config.h")
    with open(header, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if define in line:
                return True
    return False


def _wal_path(node, walfile):
    # Forward slashes: pg_waldump splits a WAL path on "/" to find the
    # directory, so a Windows backslash path would not be located.
    return "/".join([node.data_dir.replace("\\", "/"), "pg_wal", walfile])


def _run_waldump(pg_bin, args):
    """Run pg_waldump and return CommandResult."""
    return pg_bin.result(["pg_waldump", *args])


def _test_pg_waldump_skip_bytes(pg_bin, path, startlsn, endlsn):
    """Test that a message is shown if `from` wasn't a record start."""
    # Construct a new LSN that is one byte past the original start_lsn.
    part1, part2 = startlsn.split("/")
    lsn2 = int(part2, 16) + 1
    new_start = "%s/%X" % (part1, lsn2)

    res = _run_waldump(
        pg_bin,
        ["--start", new_start, "--end", endlsn, "--path", path],
    )
    assert res.returncode == 0, "runs with start segment and start LSN specified"
    assert re.search(r"first record is after", res.stderr), "info message printed"


def _test_pg_waldump(pg_bin, path, startlsn, endlsn, *opts):
    """Run pg_waldump with options; return list of output lines."""
    res = _run_waldump(
        pg_bin,
        ["--start", startlsn, "--end", endlsn, "--path", path, *opts],
    )
    label = " ".join(str(o) for o in opts)
    assert res.returncode == 0, f"pg_waldump {label}: runs ok"
    assert res.stderr == "", f"pg_waldump {label}: no stderr"
    lines = res.stdout.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    assert len(lines) > 0, f"pg_waldump {label}: some lines are output"
    return lines


def _generate_archive(tar, tar_p_flags, archive, directory, compression_flags):
    """Create a tar archive, shuffling the file order."""
    files = [e for e in os.listdir(directory) if e not in (".", "..")]
    random.shuffle(files)

    # Run tar from within the WAL directory so member names are relative.
    argv = [tar, *tar_p_flags, compression_flags, archive, *files]
    proc = subprocess.run(argv, cwd=directory)
    assert proc.returncode == 0, "tar archive created"


def test_pg_waldump_basic(pg_bin, create_pg, pg_config, tempdir_short):
    tar = _find_tar()
    tar_p_flags = _tar_portability_options(tar)

    pg_bin.program_help_ok("pg_waldump")
    pg_bin.program_version_ok("pg_waldump")
    pg_bin.program_options_handling_ok("pg_waldump")

    # wrong number of arguments
    pg_bin.command_fails_like(
        ["pg_waldump"], r"error: no arguments", "no arguments"
    )
    pg_bin.command_fails_like(
        ["pg_waldump", "foo", "bar", "baz"],
        r"error: too many command-line arguments",
        "too many arguments",
    )

    # invalid option arguments
    pg_bin.command_fails_like(
        ["pg_waldump", "--block", "bad"],
        r"error: invalid block number",
        "invalid block number",
    )
    pg_bin.command_fails_like(
        ["pg_waldump", "--fork", "bad"],
        r"error: invalid fork name",
        "invalid fork name",
    )
    pg_bin.command_fails_like(
        ["pg_waldump", "--limit", "bad"],
        r"error: invalid value",
        "invalid limit",
    )
    pg_bin.command_fails_like(
        ["pg_waldump", "--relation", "bad"],
        r"error: invalid relation",
        "invalid relation specification",
    )
    pg_bin.command_fails_like(
        ["pg_waldump", "--rmgr", "bad"],
        r"error: resource manager .* does not exist",
        "invalid rmgr name",
    )
    pg_bin.command_fails_like(
        ["pg_waldump", "--start", "bad"],
        r"error: invalid WAL location",
        "invalid start LSN",
    )
    pg_bin.command_fails_like(
        ["pg_waldump", "--end", "bad"],
        r"error: invalid WAL location",
        "invalid end LSN",
    )

    # rmgr list: If you add one to the list, consider also adding a test
    # case exercising the new rmgr below.
    pg_bin.command_like(
        ["pg_waldump", "--rmgr=list"],
        re.compile(
            r"""^XLOG
Transaction
Storage
CLOG
Database
Tablespace
MultiXact
RelMap
Standby
Heap2
Heap
Btree
Hash
Gin
Gist
Sequence
SPGist
BRIN
CommitTs
ReplicationOrigin
Generic
LogicalMessage
XLOG2$""",
            re.MULTILINE,
        ),
        "rmgr list",
    )

    node = create_pg("main", start=False)
    node.append_conf(
        """
autovacuum = off
checkpoint_timeout = 1h

# for standbydesc
archive_mode=on
archive_command=''

# for XLOG_HEAP_TRUNCATE
wal_level=logical
"""
    )
    node.start()

    start_lsn, start_walfile = node.safe_sql(
        "SELECT pg_current_wal_insert_lsn(), "
        "pg_walfile_name(pg_current_wal_insert_lsn())"
    ).split("|")

    # heap, btree, hash, sequence and assorted DDL/DML.  Statements that cannot
    # run in a transaction block (VACUUM, VACUUM FULL, CREATE/DROP DATABASE) are
    # issued in separate safe_sql calls, since the in-process libpq session
    # wraps a multi-statement string in a single implicit transaction.
    node.safe_sql(
        """
-- heap, btree, hash, sequence
CREATE TABLE t1 (a int GENERATED ALWAYS AS IDENTITY, b text);
CREATE INDEX i1a ON t1 USING btree (a);
CREATE INDEX i1b ON t1 USING hash (b);
INSERT INTO t1 VALUES (default, 'one'), (default, 'two');
DELETE FROM t1 WHERE b = 'one';
TRUNCATE t1;

-- unlogged/init fork
CREATE UNLOGGED TABLE t2 (x int);
CREATE INDEX i2 ON t2 USING btree (x);
INSERT INTO t2 SELECT generate_series(1, 10);

-- gin
CREATE TABLE gin_idx_tbl (id bigserial PRIMARY KEY, data jsonb);
CREATE INDEX gin_idx ON gin_idx_tbl USING gin (data);
INSERT INTO gin_idx_tbl
    WITH random_json AS (
        SELECT json_object_agg(key, trunc(random() * 10)) as json_data
            FROM unnest(array['a', 'b', 'c']) as u(key))
          SELECT generate_series(1,500), json_data FROM random_json;

-- gist, spgist
CREATE TABLE gist_idx_tbl (p point);
CREATE INDEX gist_idx ON gist_idx_tbl USING gist (p);
CREATE INDEX spgist_idx ON gist_idx_tbl USING spgist (p);
INSERT INTO gist_idx_tbl (p) VALUES (point '(1, 1)'), (point '(3, 2)'), (point '(6, 3)');

-- brin
CREATE TABLE brin_idx_tbl (col1 int, col2 text, col3 text );
CREATE INDEX brin_idx ON brin_idx_tbl USING brin (col1, col2, col3) WITH (autosummarize=on);
INSERT INTO brin_idx_tbl SELECT generate_series(1, 10000), 'dummy', 'dummy';
UPDATE brin_idx_tbl SET col2 = 'updated' WHERE col1 BETWEEN 1 AND 5000;
SELECT brin_summarize_range('brin_idx', 0);
SELECT brin_desummarize_range('brin_idx', 0);
"""
    )

    # abort transaction (must be its own session block to ROLLBACK)
    node.safe_sql(
        """
START TRANSACTION;
INSERT INTO t1 VALUES (default, 'three');
ROLLBACK;
"""
    )

    node.safe_sql("VACUUM")

    # logical message
    node.safe_sql("SELECT pg_logical_emit_message(true, 'foo', 'bar')")

    # relmap
    node.safe_sql("VACUUM FULL pg_authid")

    # database
    node.safe_sql("CREATE DATABASE d1")
    node.safe_sql("DROP DATABASE d1")

    # Use a short location: on Windows the tablespace junction's target must be
    # short, and forward slashes avoid a malformed junction.
    tblspc_path = tempdir_short.replace("\\", "/")
    node.safe_sql(f"CREATE TABLESPACE ts1 LOCATION '{tblspc_path}'")
    node.safe_sql("DROP TABLESPACE ts1")

    # Test: Decode a continuation record (contrecord) that spans multiple WAL
    # segments.
    #
    # Now consume all remaining room in the current WAL segment, leaving
    # space enough only for the start of a largish record.
    node.safe_sql(
        """
DO $$
DECLARE
    wal_segsize int := setting::int FROM pg_settings WHERE name = 'wal_segment_size';
    remain int;
    iters  int := 0;
BEGIN
    LOOP
        INSERT into t1(b)
        select repeat(encode(sha256(g::text::bytea), 'hex'), (random() * 15 + 1)::int)
        from generate_series(1, 10) g;

        remain := wal_segsize - (pg_current_wal_insert_lsn() - '0/0') % wal_segsize;
        IF remain < 2 * setting::int from pg_settings where name = 'block_size' THEN
            RAISE log 'exiting after % iterations, % bytes to end of WAL segment', iters, remain;
            EXIT;
        END IF;
        iters := iters + 1;
    END LOOP;
END
$$;
"""
    )

    contrecord_lsn = node.safe_sql("SELECT pg_current_wal_insert_lsn()")
    # Generate contrecord record
    node.safe_sql(
        "SELECT pg_logical_emit_message(true, 'test 026', repeat('xyzxz', 123456))"
    )

    end_lsn, end_walfile = node.safe_sql(
        "SELECT pg_current_wal_insert_lsn(), "
        "pg_walfile_name(pg_current_wal_insert_lsn())"
    ).split("|")

    default_ts_oid = node.safe_sql(
        "SELECT oid FROM pg_tablespace WHERE spcname = 'pg_default'"
    )
    postgres_db_oid = node.safe_sql(
        "SELECT oid FROM pg_database WHERE datname = 'postgres'"
    )
    rel_t1_oid = node.safe_sql(
        "SELECT oid FROM pg_class WHERE relname = 't1'"
    )
    rel_i1a_oid = node.safe_sql(
        "SELECT oid FROM pg_class WHERE relname = 'i1a'"
    )

    data_dir = node.data_dir
    node.stop()

    # various ways of specifying WAL range
    pg_bin.command_fails_like(
        ["pg_waldump", "foo", "bar"],
        r'error: could not locate WAL file "foo"',
        "start file not found",
    )
    pg_bin.command_like(
        ["pg_waldump", _wal_path(node, start_walfile)],
        r".",
        "runs with start segment specified",
    )
    pg_bin.command_fails_like(
        ["pg_waldump", _wal_path(node, start_walfile), "bar"],
        r'error: could not open file "bar"',
        "end file not found",
    )
    pg_bin.command_like(
        [
            "pg_waldump",
            _wal_path(node, start_walfile),
            _wal_path(node, end_walfile),
        ],
        r".",
        "runs with start and end segment specified",
    )
    pg_bin.command_like(
        [
            "pg_waldump",
            "--quiet",
            "--path", os.path.join(data_dir, "pg_wal") + "/",
            start_walfile,
        ],
        re.compile(r"^$"),
        "no output with --quiet option",
    )

    # Test that pg_waldump reports a detailed error message when dumping
    # a WAL file with an invalid magic number (0000).
    #
    # The broken WAL file is created by copying a valid WAL file and
    # overwriting its magic number with 0000.
    broken_wal_dir = os.path.join(node.basedir, "broken_wal")
    os.makedirs(broken_wal_dir, exist_ok=True)
    # Forward slashes: pg_waldump locates a WAL file by splitting its path on
    # "/", so a Windows backslash path would not be found.
    broken_wal = broken_wal_dir.replace("\\", "/") + "/" + start_walfile
    shutil.copy(_wal_path(node, start_walfile), broken_wal)

    with open(broken_wal, "r+b") as fh:
        fh.seek(0)
        fh.write(struct.pack("=H", 0))

    pg_bin.command_fails_like(
        ["pg_waldump", broken_wal],
        re.compile(r"invalid magic number 0000", re.IGNORECASE),
        "detailed error message shown for invalid WAL page magic",
    )

    tmp_dir = os.path.join(node.basedir, "archives")
    os.makedirs(tmp_dir, exist_ok=True)

    have_libz = _check_pg_config(pg_config, "#define HAVE_LIBZ 1")

    scenarios = [
        {"path": data_dir, "is_archive": False, "enabled": True},
        {
            "path": os.path.join(tmp_dir, "pg_wal.tar"),
            "compression_method": "none",
            "compression_flags": "-cf",
            "is_archive": True,
            "enabled": True,
        },
        {
            "path": os.path.join(tmp_dir, "pg_wal.tar.gz"),
            "compression_method": "gzip",
            "compression_flags": "-czf",
            "is_archive": True,
            "enabled": have_libz,
        },
    ]

    for scenario in scenarios:
        # pg_waldump splits the path on "/", so feed it forward slashes.
        path = scenario["path"].replace("\\", "/")

        if scenario["is_archive"] and (not tar):
            # skip "tar command is not available"
            continue
        if scenario["is_archive"] and not scenario["enabled"]:
            # skip "<method> compression not supported by this build"
            continue

        # create pg_wal archive
        if scenario["is_archive"]:
            _generate_archive(
                tar,
                tar_p_flags,
                path,
                os.path.join(data_dir, "pg_wal"),
                scenario["compression_flags"],
            )

        pg_bin.command_fails_like(
            ["pg_waldump", "--path", path],
            r"error: no start WAL location given",
            "path option requires start location",
        )
        pg_bin.command_like(
            [
                "pg_waldump",
                "--path", path,
                "--start", start_lsn,
                "--end", end_lsn,
            ],
            r".",
            "runs with path option and start and end locations",
        )
        pg_bin.command_fails_like(
            ["pg_waldump", "--path", path, "--start", start_lsn],
            r"error: error in WAL record at",
            "falling off the end of the WAL results in an error",
        )
        pg_bin.command_fails_like(
            ["pg_waldump", "--quiet", "--path", path, "--start", start_lsn],
            r"error: error in WAL record at",
            "errors are shown with --quiet",
        )

        _test_pg_waldump_skip_bytes(pg_bin, path, start_lsn, end_lsn)

        lines = _test_pg_waldump(pg_bin, path, start_lsn, end_lsn)
        assert len([ln for ln in lines if not re.match(r"^rmgr: \w", ln)]) == 0, (
            "all output lines are rmgr lines"
        )

        lines = _test_pg_waldump(pg_bin, path, contrecord_lsn, end_lsn)
        assert len([ln for ln in lines if not re.match(r"^rmgr: \w", ln)]) == 0, (
            "all output lines are rmgr lines"
        )

        _test_pg_waldump_skip_bytes(pg_bin, path, contrecord_lsn, end_lsn)

        lines = _test_pg_waldump(pg_bin, path, start_lsn, end_lsn, "--limit", "6")
        assert len(lines) == 6, "limit option observed"

        lines = _test_pg_waldump(pg_bin, path, start_lsn, end_lsn, "--fullpage")
        assert len(
            [ln for ln in lines if not re.search(r"^rmgr:.*\bFPW\b", ln)]
        ) == 0, "all output lines are FPW"

        lines = _test_pg_waldump(pg_bin, path, start_lsn, end_lsn, "--stats")
        assert re.search(r"WAL statistics", lines[0]), "statistics on stdout"
        assert len([ln for ln in lines if re.search(r"^rmgr:", ln)]) == 0, (
            "no rmgr lines output"
        )

        lines = _test_pg_waldump(
            pg_bin, path, start_lsn, end_lsn, "--stats=record"
        )
        assert re.search(r"WAL statistics", lines[0]), "statistics on stdout"
        assert len([ln for ln in lines if re.search(r"^rmgr:", ln)]) == 0, (
            "no rmgr lines output"
        )

        lines = _test_pg_waldump(
            pg_bin, path, start_lsn, end_lsn, "--rmgr", "Btree"
        )
        assert len(
            [ln for ln in lines if not re.search(r"^rmgr: Btree", ln)]
        ) == 0, "only Btree lines"

        lines = _test_pg_waldump(
            pg_bin, path, start_lsn, end_lsn, "--fork", "init"
        )
        assert len(
            [ln for ln in lines if not re.search(r"fork init", ln)]
        ) == 0, "only init fork lines"

        rel = f"{default_ts_oid}/{postgres_db_oid}/{rel_t1_oid}"
        lines = _test_pg_waldump(
            pg_bin, path, start_lsn, end_lsn, "--relation", rel
        )
        relre = re.compile(
            r"rel %s/%s/%s" % (default_ts_oid, postgres_db_oid, rel_t1_oid)
        )
        assert len([ln for ln in lines if not relre.search(ln)]) == 0, (
            "only lines for selected relation"
        )

        rel_i = f"{default_ts_oid}/{postgres_db_oid}/{rel_i1a_oid}"
        lines = _test_pg_waldump(
            pg_bin, path, start_lsn, end_lsn,
            "--relation", rel_i, "--block", "1",
        )
        assert len(
            [ln for ln in lines if not re.search(r"\bblk 1\b", ln)]
        ) == 0, "only lines for selected block"

        # Cleanup.
        if scenario["is_archive"]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
