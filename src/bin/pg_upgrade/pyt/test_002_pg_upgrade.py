# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Set of tests for pg_upgrade, including cross-version checks.

The test of pg_upgrade requires two clusters, an old one and a new one that
gets upgraded.  Before running the upgrade, a logical dump of the old cluster
is taken, and a second logical dump of the new one is taken after the upgrade.
The upgrade test passes if there are no differences (after filtering) in these
two dumps.

This test focuses on the same-version path (the default, where the environment
variables ``oldinstall`` and ``olddump`` are unset).  The cross-version
branches are kept faithfully but never execute here because the old and new
clusters are always built from the running source tree.

The dump-adjustment logic (which is version-conditional and substantial) is
delegated to the adjust_dump.pl wrapper, invoked as a subprocess, which reuses
the existing version-conditional dump-adjustment behavior.
"""

import difflib
import os
import re
import shutil
import subprocess

import pytest

from pypg.regress import pg_regress_available, run_pg_regress
from pypg.util import slurp_file

# Repository root, derived from this file's location
# (.../src/bin/pg_upgrade/pyt/test_002_pg_upgrade.py).
REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
ADJUST_DUMP_PL = os.path.join(
    REPO, "src", "bin", "pg_upgrade", "pyt", "adjust_dump.pl"
)
PERL_LIB = os.path.join(REPO, "src", "test", "perl")


def _pg_version_token(pg_bin):
    """Return the old cluster's major version token, like '19devel' or '18.1'.

    Parses ``pg_config --version`` (e.g. "PostgreSQL 19devel" -> "19devel").
    This token represents the old cluster's version and is what adjust_dump.pl
    is fed to drive its version-conditional behavior.
    """
    out = subprocess.run(
        [os.path.join(pg_bin.bindir, "pg_config"), "--version"],
        stdout=subprocess.PIPE, text=True, check=True,
    ).stdout
    # e.g. "PostgreSQL 19devel" / "PostgreSQL 18.1" / "PostgreSQL 17beta1"
    m = re.search(r"PostgreSQL (\d+(?:\.\d+)?(?:devel|beta\d+|rc\d+|alpha\d+)?)", out)
    assert m, f"could not parse pg_config --version output: {out!r}"
    return m.group(1)


def _version_at_least(version_token, target):
    """Compare *version_token* (e.g. '19devel') with *target* (e.g. '17devel').

    Same-version runs are always >= '17devel'; this helper exists so the
    version-conditional branches read clearly.
    """
    def major(tok):
        return int(re.match(r"(\d+)", tok).group(1))
    vt = major(version_token)
    tt = major(target.rstrip("devel"))
    if vt != tt:
        return vt > tt
    # Same major: 'devel' >= 'devel'/non-devel of same number.
    return True


def _adjust_dump(mode, arg, dump_path, out_path):
    """Filter *dump_path* through adjust_dump.pl <mode> <arg>, write *out_path*.

    DO NOT reimplement the adjust logic here -- delegate to the adjust_dump.pl
    wrapper so we reuse the exact version-conditional dump-adjustment behavior.
    """
    with open(dump_path, "rb") as fh:
        dump_bytes = fh.read()
    proc = subprocess.run(
        ["perl", "-I", PERL_LIB, ADJUST_DUMP_PL, mode, str(arg)],
        input=dump_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 0, (
        f"adjust_dump.pl {mode} {arg} failed: {proc.stderr.decode(errors='replace')}"
    )
    with open(out_path, "wb") as fh:
        fh.write(proc.stdout)
    return out_path


def _filter_dump(is_old, old_version, dump_file):
    """Adjust a dump and return the filtered path."""
    mode = "old" if is_old else "new"
    return _adjust_dump(mode, old_version, dump_file, f"{dump_file}_filtered")


def _get_dump_for_comparison(node, db, file_prefix, adjust_child_columns, tempdir):
    """Produce an adjusted dump suitable for before/after comparison.

    Dump *db* from *node* in plain format and adjust it (via adjust_dump.pl
    regress) for comparing dumps from the original and the restored database.
    Returns the path to the adjusted dump file.
    """
    dumpfile = os.path.join(tempdir, file_prefix + ".sql")
    dump_adjusted = dumpfile + "_adjusted"
    node.pg_bin.command_ok(
        [
            "pg_dump", "--no-sync",
            "--restrict-key", "test",
            "-d", node.connstr(db),
            "-f", dumpfile,
        ],
        "pg_dump for comparison",
    )
    _adjust_dump("regress", adjust_child_columns, dumpfile, dump_adjusted)
    return dump_adjusted


def _generate_db(node, prefix, from_char, to_char, suffix):
    """Create a database whose name spans a range of ASCII bytes.

    The name is built from *prefix*, the bytes *from_char*..*to_char* (skipping
    BEL/LF/CR), and *suffix*, then createdb is run as a subprocess.
    """
    dbname = prefix
    for i in range(from_char, to_char + 1):
        if i in (7, 10, 13):  # skip BEL, LF, and CR
            continue
        dbname += chr(i)
    dbname += suffix
    node.pg_bin.command_ok(
        ["createdb", dbname],
        f"created database with ASCII characters from {from_char} to {to_char}",
    )


def _compare_files(file1, file2, msg):
    """Assert the two files are byte-identical; show a bounded diff otherwise."""
    with open(file1, "r", encoding="utf-8", errors="replace") as fh:
        lines1 = fh.readlines()
    with open(file2, "r", encoding="utf-8", errors="replace") as fh:
        lines2 = fh.readlines()
    if lines1 == lines2:
        return
    diff = list(
        difflib.unified_diff(
            lines1, lines2,
            fromfile=file1, tofile=file2, n=3,
        )
    )
    # Bound the diff so a huge mismatch does not flood the output.
    snippet = "".join(diff[:200])
    pytest.fail(f"{msg}\n{snippet}")


def test_002_pg_upgrade(create_pg, pg_bin, tmp_path):
    # Running the full regression suite to populate the old cluster requires
    # the pg_regress driver; skip if the build did not supply it.
    if not pg_regress_available():
        pytest.skip("pg_regress not available (PG_REGRESS unset)")

    tempdir = str(tmp_path)

    # Can be changed to test the other modes.
    mode = os.environ.get("PG_TEST_PG_UPGRADE_MODE", "--copy")

    # Cross-version testing requires both "olddump" and "oldinstall" to be set;
    # having only one is an error.
    olddump = os.environ.get("olddump")
    oldinstall = os.environ.get("oldinstall")
    if bool(olddump) != bool(oldinstall):
        raise RuntimeError("olddump or oldinstall is undefined")

    # Paths to the dumps taken during the tests.
    dump1_file = os.path.join(tempdir, "dump1.sql")
    dump2_file = os.path.join(tempdir, "dump2.sql")

    print(f"# testing using transfer mode {mode}")

    # The old cluster is always built from the running source tree here, so its
    # version is the current major version (>= 18).
    old_version = _pg_version_token(pg_bin)

    # To increase coverage of non-standard segment size and group access
    # without increasing test runtime, run these tests with a custom setting.
    # --allow-group-access and --wal-segsize have been added in v11.
    custom_opts = []
    if _version_at_least(old_version, "11"):
        custom_opts += ["--wal-segsize", "1"]
        custom_opts += ["--allow-group-access"]

    # Account for field additions and changes (same-version: >= 17devel).
    if _version_at_least(old_version, "15"):
        old_provider_field = "datlocprovider"
        if _version_at_least(old_version, "17devel"):
            old_datlocale_field = "datlocale"
        else:
            old_datlocale_field = "daticulocale AS datlocale"
    else:
        old_provider_field = "'c' AS datlocprovider"
        old_datlocale_field = "NULL AS datlocale"

    # Set up the locale settings for the original cluster, so that we can test
    # that pg_upgrade copies the locale settings of template0 from the old to
    # the new cluster.
    original_datcollate = "C"
    original_datctype = "C"
    with_icu = os.environ.get("with_icu")
    if _version_at_least(old_version, "17devel"):
        original_enc_name = "UTF-8"
        original_provider = "b"
        original_datlocale = "C.UTF-8"
    elif _version_at_least(old_version, "15") and with_icu == "yes":
        original_enc_name = "UTF-8"
        original_provider = "i"
        original_datlocale = "fr-CA"
    else:
        original_enc_name = "SQL_ASCII"
        original_provider = "c"
        original_datlocale = ""

    encodings = {"UTF-8": 6, "SQL_ASCII": 0}
    original_encoding = encodings[original_enc_name]

    old_initdb_params = list(custom_opts)
    old_initdb_params += ["--encoding", original_enc_name]
    old_initdb_params += ["--lc-collate", original_datcollate]
    old_initdb_params += ["--lc-ctype", original_datctype]

    # add --locale-provider, if supported
    provider_name = {"b": "builtin", "i": "icu", "c": "libc"}
    if _version_at_least(old_version, "15"):
        old_initdb_params += ["--locale-provider", provider_name[original_provider]]
        if original_provider == "b":
            old_initdb_params += ["--builtin-locale", original_datlocale]
        elif original_provider == "i":
            old_initdb_params += ["--icu-locale", original_datlocale]

    # Since checksums are now enabled by default, and weren't before 18, pass
    # '-k' to initdb on old versions so that upgrades work.
    if not _version_at_least(old_version, "18"):
        old_initdb_params += ["-k"]

    # The create_pg fixture passes --locale=C and --encoding=UTF8 by default;
    # the explicit encoding/locale/provider opts above override those.
    oldnode = create_pg("old_node", start=False, initdb_extra=old_initdb_params)
    # Override log_statement=all set by the init helper.  This avoids large
    # amounts of log traffic that slow this test down even more when run under
    # valgrind.
    oldnode.append_conf("log_statement = none")
    # Set wal_level = replica to run the regression tests in the same wal_level
    # as when 'make check' runs.
    oldnode.append_conf("wal_level = replica")
    oldnode.start()

    result = oldnode.safe_sql(
        "SELECT encoding, %s, datcollate, datctype, %s "
        "FROM pg_database WHERE datname='template0'"
        % (old_provider_field, old_datlocale_field)
    )
    assert result == (
        f"{original_encoding}|{original_provider}|{original_datcollate}|"
        f"{original_datctype}|{original_datlocale}"
    ), "check locales in original cluster"

    # The default location of the source code is the root of this directory.
    srcdir = REPO

    # Set up the data of the old instance with a dump or pg_regress.
    if olddump:
        # Use the dump specified.  (Cross-version path; not exercised here.)
        assert os.path.exists(olddump), "no dump file found!"
        oldnode.pg_bin.command_ok(
            ["psql", "--no-psqlrc", "--file", olddump, "postgres"],
            "loaded old dump file",
        )
    else:
        # Default is to use pg_regress to set up the old instance.

        # Create databases with names covering most ASCII bytes.  The first
        # name exercises backslashes adjacent to double quotes, a Windows
        # special case.
        _generate_db(oldnode, 'regression\\"\\', 1, 45, '\\\\"\\\\\\')
        _generate_db(oldnode, "regression", 46, 90, "")
        _generate_db(oldnode, "regression", 91, 127, "")

        # run_pg_regress mirrors the command_ok([$ENV{PG_REGRESS}, ...]) call:
        # it honors EXTRA_REGRESS_OPTS, derives --dlpath from REGRESS_SHLIB,
        # passes --bindir= (empty), and points --host/--port at the node.
        res = run_pg_regress(
            oldnode,
            inputdir=os.path.join(srcdir, "src", "test", "regress"),
            outputdir=os.path.join(tempdir, "regress_outputdir"),
            schedule=os.path.join(
                srcdir, "src", "test", "regress", "parallel_schedule"
            ),
            max_concurrent_tests=20,
        )
        assert res.returncode == 0, (
            "regression tests in old instance\n"
            f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        )

    # Initialize a new node for the upgrade.  The new cluster will be
    # initialized with different locale settings, but these settings will be
    # overwritten with those of the original cluster.
    new_initdb_params = list(custom_opts)
    new_initdb_params += ["--encoding", "SQL_ASCII"]
    new_initdb_params += ["--locale-provider", "libc"]
    newnode = create_pg("new_node", start=False, initdb_extra=new_initdb_params)
    # Avoid unnecessary log noise
    newnode.append_conf("log_statement = none")
    # Stabilize stats for comparison.
    newnode.append_conf("autovacuum = off")

    # There is no node.config_data(); use pg_config --bindir directly.  Both
    # clusters come from the same install, so the bindir is shared.
    bindir = subprocess.run(
        [os.path.join(pg_bin.bindir, "pg_config"), "--bindir"],
        stdout=subprocess.PIPE, text=True, check=True,
    ).stdout.strip()
    newbindir = bindir
    oldbindir = bindir

    # Before dumping, get rid of objects not existing or not supported in later
    # versions.  This depends on the version of the old server used, and
    # matters only if different major versions are used for the dump.  This is
    # the cross-version path and is not exercised in the same-version run; it
    # would need a database-content adjustment step that is not wired through
    # adjust_dump.pl.
    if oldinstall:
        raise RuntimeError(
            "cross-version (oldinstall) adjust_database_contents path is not "
            "supported by this port"
        )

    # Stabilize stats before pg_dump / pg_dumpall.  Doing it after initializing
    # the new node gives enough time for autovacuum to update statistics on the
    # old node.
    oldnode.append_conf("autovacuum = off")
    oldnode.restart()

    # Test that dump/restore of the regression database roundtrips cleanly.
    # This doesn't work well when the nodes are different versions, so skip it
    # in that case.  Note that this isn't a pg_upgrade test, but it's
    # convenient to do it here because we've gone to the trouble of creating
    # the regression database.
    #
    # Do this while the old cluster is running before it is shut down by the
    # upgrade test but after turning its autovacuum off for stable statistics.
    pg_test_extra = os.environ.get("PG_TEST_EXTRA", "")
    if (
        pg_test_extra
        and re.search(r"\bregress_dump_restore\b", pg_test_extra)
        and not oldinstall
    ):
        # Set up destination database cluster with the same configuration as
        # the source cluster to avoid any differences between dumps taken from
        # both the clusters caused by differences in their configurations.
        dstnode = create_pg(
            "dst_node", start=False, initdb_extra=old_initdb_params
        )
        dstnode.append_conf("log_statement = none")
        dstnode.append_conf("autovacuum = off")
        dstnode.start()

        # Use --create in dump and restore commands so that the restored
        # database has the same configurable variable settings as the original
        # database so that the dumps taken from both databases do not differ
        # because of locale changes.  Additionally this provides test coverage
        # for --create option.  Use directory format so that we can use
        # parallel dump/restore.
        dump_file = os.path.join(tempdir, "regression.dump")
        oldnode.pg_bin.command_ok(
            [
                "pg_dump", "-Fd", "-j2", "--no-sync",
                "-d", oldnode.connstr("regression"),
                "--create", "-f", dump_file,
            ],
            "pg_dump on source instance",
        )
        dstnode.pg_bin.command_ok(
            ["pg_restore", "--create", "-j2", "-d", "postgres", dump_file],
            "pg_restore to destination instance",
        )

        # Dump original and restored database for comparison.
        src_dump = _get_dump_for_comparison(
            oldnode, "regression", "src_dump", 1, tempdir
        )
        dst_dump = _get_dump_for_comparison(
            dstnode, "regression", "dest_dump", 0, tempdir
        )
        _compare_files(
            src_dump, dst_dump,
            "dump outputs from original and restored regression databases match",
        )

    # Take a dump before performing the upgrade as a base comparison.  Note
    # that we need to use pg_dumpall from the new node here.
    dump_command = [
        "pg_dumpall", "--no-sync",
        "--restrict-key", "test",
        "--dbname", oldnode.connstr("postgres"),
        "--file", dump1_file,
    ]
    # --extra-float-digits is needed when upgrading from a version older than 11.
    if not _version_at_least(old_version, "12"):
        dump_command += ["--extra-float-digits", "0"]
    newnode.pg_bin.command_ok(dump_command, "dump before running pg_upgrade")

    # After dumping, update references to the old source tree's regress.so to
    # point to the new tree.  This is only relevant for the cross-version
    # (oldinstall) path, which is rejected above; skip otherwise.
    if oldinstall:
        # (Unreachable here, retained for the cross-version path.)
        output = oldnode.safe_sql(
            "SELECT DISTINCT probin::text FROM pg_proc "
            "WHERE probin NOT LIKE '$libdir%';"
        )
        libpaths = [p for p in output.split("\n") if p]
        dump_data = slurp_file(dump1_file)
        newregresssrc = os.path.dirname(os.environ["REGRESS_SHLIB"])
        for libpath in libpaths:
            libpath = os.path.dirname(libpath)
            dump_data = dump_data.replace(libpath, newregresssrc)
        with open(dump1_file, "w", encoding="utf-8") as fh:
            fh.write(dump_data)
        output = oldnode.safe_sql(
            "SELECT datname FROM pg_database WHERE datallowconn;"
        )
        for datname in [d for d in output.split("\n") if d]:
            oldnode.safe_sql(
                "UPDATE pg_proc SET probin = "
                f"regexp_replace(probin, '.*/', '{newregresssrc}/') "
                "WHERE probin NOT LIKE '$libdir/%'",
                dbname=datname,
            )

    # Create an invalid database, will be deleted below.  CREATE DATABASE
    # cannot run inside a transaction block, so issue the statements
    # separately (the in-process session would otherwise wrap them together).
    oldnode.safe_sql("CREATE DATABASE regression_invalid")
    oldnode.safe_sql(
        "UPDATE pg_database SET datconnlimit = -2 "
        "WHERE datname = 'regression_invalid'"
    )

    # In a VPATH build, we'll be started in the source directory, but we want
    # to run pg_upgrade in the build directory so that any files generated
    # finish in it, like delete_old_cluster.{sh,bat}.
    upgrade_cwd = os.path.join(tempdir, "pg_upgrade_cwd")
    os.makedirs(upgrade_cwd, exist_ok=True)
    os.chdir(upgrade_cwd)

    # Upgrade the instance.
    oldnode.stop()

    # Cause a failure at the start of pg_upgrade, this should create the
    # logging directory pg_upgrade_output.d but leave it around.  Keep --check
    # for an early exit.
    pg_bin.command_checks_all(
        [
            "pg_upgrade", "--no-sync",
            "--old-datadir", oldnode.data_dir,
            "--new-datadir", newnode.data_dir,
            "--old-bindir", oldbindir + "/does/not/exist/",
            "--new-bindir", newbindir,
            "--socketdir", newnode.host,
            "--old-port", str(oldnode.port),
            "--new-port", str(newnode.port),
            mode, "--check",
        ],
        1,
        [r'check for ".*?does/not/exist" failed'],
        [],
        "run of pg_upgrade --check for new instance with incorrect binary path",
    )
    assert os.path.isdir(
        os.path.join(newnode.data_dir, "pg_upgrade_output.d")
    ), "pg_upgrade_output.d/ not removed after pg_upgrade failure"
    shutil.rmtree(os.path.join(newnode.data_dir, "pg_upgrade_output.d"))

    # Check that pg_upgrade aborts when encountering an invalid database.
    # (Versions out of support by commit c66a7d75e652 don't know how to do
    # this; the old cluster is always new enough here, so no skip is needed.)
    if _version_at_least(old_version, "11"):
        pg_bin.command_checks_all(
            [
                "pg_upgrade", "--no-sync",
                "--old-datadir", oldnode.data_dir,
                "--new-datadir", newnode.data_dir,
                "--old-bindir", oldbindir,
                "--new-bindir", newbindir,
                "--socketdir", newnode.host,
                "--old-port", str(oldnode.port),
                "--new-port", str(newnode.port),
                mode, "--check",
            ],
            1,
            [r"datconnlimit"],
            [r"^$"],
            "invalid database causes failure",
        )
        shutil.rmtree(os.path.join(newnode.data_dir, "pg_upgrade_output.d"))

    # And drop it, so we can continue
    oldnode.start()
    oldnode.safe_sql("DROP DATABASE regression_invalid")
    oldnode.stop()

    # --check command works here, cleans up pg_upgrade_output.d.
    pg_bin.command_ok(
        [
            "pg_upgrade", "--no-sync",
            "--old-datadir", oldnode.data_dir,
            "--new-datadir", newnode.data_dir,
            "--old-bindir", oldbindir,
            "--new-bindir", newbindir,
            "--socketdir", newnode.host,
            "--old-port", str(oldnode.port),
            "--new-port", str(newnode.port),
            mode, "--check",
        ],
        "run of pg_upgrade --check for new instance",
    )
    assert not os.path.isdir(
        os.path.join(newnode.data_dir, "pg_upgrade_output.d")
    ), "pg_upgrade_output.d/ removed after pg_upgrade --check success"

    # Actual run, pg_upgrade_output.d is removed at the end.
    pg_bin.command_ok(
        [
            "pg_upgrade", "--no-sync",
            "--old-datadir", oldnode.data_dir,
            "--new-datadir", newnode.data_dir,
            "--old-bindir", oldbindir,
            "--new-bindir", newbindir,
            "--socketdir", newnode.host,
            "--old-port", str(oldnode.port),
            "--new-port", str(newnode.port),
            mode,
        ],
        "run of pg_upgrade for new instance",
    )
    assert not os.path.isdir(
        os.path.join(newnode.data_dir, "pg_upgrade_output.d")
    ), "pg_upgrade_output.d/ removed after pg_upgrade success"

    newnode.start()

    # Check if there are any logs coming from pg_upgrade, that would only be
    # retained on failure.
    log_path = os.path.join(newnode.data_dir, "pg_upgrade_output.d")
    if os.path.isdir(log_path):
        print(f"=== pg_upgrade logs found under {log_path} ===")
        for dirpath, _dirnames, filenames in os.walk(log_path):
            for filename in filenames:
                if filename.endswith(".log"):
                    log = os.path.join(dirpath, filename)
                    print(f"=== contents of {log} ===")
                    print(slurp_file(log))
                    print("=== EOF ===")

    # Test that upgraded cluster has original locale settings.
    result = newnode.safe_sql(
        "SELECT encoding, datlocprovider, datcollate, datctype, datlocale "
        "FROM pg_database WHERE datname='template0'"
    )
    assert result == (
        f"{original_encoding}|{original_provider}|{original_datcollate}|"
        f"{original_datctype}|{original_datlocale}"
    ), "check that locales in new cluster match original cluster"

    # Second dump from the upgraded instance.
    dump_command = [
        "pg_dumpall", "--no-sync",
        "--restrict-key", "test",
        "--dbname", newnode.connstr("postgres"),
        "--file", dump2_file,
    ]
    if not _version_at_least(old_version, "12"):
        dump_command += ["--extra-float-digits", "0"]
    newnode.pg_bin.command_ok(dump_command, "dump after running pg_upgrade")

    # Filter the contents of the dumps.
    dump1_filtered = _filter_dump(True, old_version, dump1_file)
    dump2_filtered = _filter_dump(False, old_version, dump2_file)

    # Compare the two dumps, there should be no differences.
    _compare_files(
        dump1_filtered, dump2_filtered,
        "old and new dumps match after pg_upgrade",
    )
