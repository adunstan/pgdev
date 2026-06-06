# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""pgbench tests which do not need a server.

Invoke the installed pgbench program with various invalid/option-handling
arguments and check exit codes and stderr.
"""

import os
from typing import Any, List

import pytest

EMPTY = [r"^$"]


def _pgbench(pg_bin, opts, stat, out, err, name):
    """Run pgbench with whitespace-split *opts* and check exit/stdout/stderr."""
    cmd = ["pgbench", *opts.split()]
    pg_bin.command_checks_all(cmd, stat, out, err, name)


def _pgbench_scripts(pg_bin, testdir, opts, stat, out, err, name, files):
    """Run pgbench with extra --file scripts written into *testdir*."""
    cmd = ["pgbench", *(opts.split() if opts else [])]
    for fn in sorted(files):
        # cleanup file weight if any
        filename = os.path.join(testdir, fn)
        if "@" in fn:
            filename = filename.split("@")[0]
        # cleanup from prior runs
        if os.path.exists(filename):
            os.unlink(filename)
        with open(filename, "a", encoding="utf-8") as f:
            f.write(files[fn])
        cmd += ["--file", filename]
    pg_bin.command_checks_all(cmd, stat, out, err, name)


# name, options, stderr checks
# Each row is a heterogeneous [str, str, List[str]] record.
OPTIONS: List[List[Any]] = [
    [
        "bad option",
        "-h home -p 5432 -U calvin ---debug --bad-option",
        [r"--help.*more information"],
    ],
    ["no file", "-f no-such-file", [r'could not open file "no-such-file":']],
    ["no builtin", "-b no-such-builtin", [r'no builtin script .* "no-such-builtin"']],
    [
        "invalid weight",
        "--builtin=select-only@one",
        [r"invalid weight specification: \@one"],
    ],
    ["invalid weight", "-b select-only@-1", [r"weight spec.* out of range .*: -1"]],
    ["too many scripts", "-S " * 129, [r"at most 128 SQL scripts"]],
    ["bad #clients", "-c three", [r'invalid value "three" for option -c/--clients']],
    ["bad #threads", "-j eleven", [r'invalid value "eleven" for option -j/--jobs']],
    ["bad scale", "-i -s two", [r'invalid value "two" for option -s/--scale']],
    [
        "invalid #transactions",
        "-t zil",
        [r'invalid value "zil" for option -t/--transactions'],
    ],
    ["invalid duration", "-T ten", [r'invalid value "ten" for option -T/--time']],
    [
        "-t XOR -T",
        "-N -l --aggregate-interval=5 --log-prefix=notused -t 1000 -T 1",
        [r"specify either "],
    ],
    [
        "-T XOR -t",
        "-P 1 --progress-timestamp -l --sampling-rate=0.001 -T 10 -t 1000",
        [r"specify either "],
    ],
    ["bad variable", "--define foobla", [r"invalid variable definition"]],
    ["invalid fillfactor", "-F 1", [r"-F/--fillfactor must be in range"]],
    ["invalid query mode", "-M no-such-mode", [r"invalid query mode"]],
    ["invalid progress", "--progress=0", [r"-P/--progress must be in range"]],
    ["invalid rate", "--rate=0.0", [r"invalid rate limit"]],
    ["invalid latency", "--latency-limit=0.0", [r"invalid latency limit"]],
    ["invalid sampling rate", "--sampling-rate=0", [r"invalid sampling rate"]],
    [
        "invalid aggregate interval",
        "--aggregate-interval=-3",
        [r"--aggregate-interval must be in range"],
    ],
    ["weight zero", "-b se@0 -b si@0 -b tpcb@0", [r"weight must not be zero"]],
    ["init vs run", "-i -S", [r"cannot be used in initialization"]],
    ["run vs init", "-S -F 90", [r"cannot be used in benchmarking"]],
    ["ambiguous builtin", "-b s", [r"ambiguous"]],
    [
        "--progress-timestamp => --progress",
        "--progress-timestamp",
        [r"allowed only under"],
    ],
    ["-I without init option", "-I dtg", [r"cannot be used in benchmarking mode"]],
    [
        "invalid init step",
        "-i -I dta",
        [r"unrecognized initialization step", r"Allowed step characters are"],
    ],
    [
        "bad random seed",
        "--random-seed=one",
        [
            r'unrecognized random seed option "one"',
            r'Expecting an unsigned integer, "time" or "rand"',
            r"error while setting random seed from --random-seed option",
        ],
    ],
    [
        "bad partition method",
        "-i --partition-method=BAD",
        [r'"range"', r'"hash"', r'"BAD"'],
    ],
    ["bad partition number", "-i --partitions -1", [r"--partitions must be in range"]],
    [
        "partition method without partitioning",
        "-i --partition-method=hash",
        [r"partition-method requires greater than zero --partitions"],
    ],
    [
        "bad maximum number of tries",
        "--max-tries -10",
        [r'invalid number of maximum tries: "-10"'],
    ],
    [
        "an infinite number of tries",
        "--max-tries 0",
        [
            r"an unlimited number of transaction tries can only be used with "
            r"--latency-limit or a duration"
        ],
    ],
    # logging sub-options
    ["sampling => log", "--sampling-rate=0.01", [r"log sampling .* only when"]],
    [
        "sampling XOR aggregate",
        "-l --sampling-rate=0.1 --aggregate-interval=3",
        [r"sampling .* aggregation .* cannot be used at the same time"],
    ],
    ["aggregate => log", "--aggregate-interval=3", [r"aggregation .* only when"]],
    ["log-prefix => log", "--log-prefix=x", [r"prefix .* only when"]],
    [
        "duration & aggregation",
        "-l -T 1 --aggregate-interval=3",
        [r"aggr.* not be higher"],
    ],
    ["duration % aggregation", "-l -T 5 --aggregate-interval=3", [r"multiple"]],
]


@pytest.mark.parametrize(
    "name,opts,err_checks",
    OPTIONS,
    ids=[o[0] for o in OPTIONS],
)
def test_option_errors(pg_bin, name, opts, err_checks):
    _pgbench(pg_bin, opts, 1, EMPTY, err_checks, "pgbench option error: " + name)


def test_program_help_version_options(pg_bin):
    pg_bin.program_help_ok("pgbench")
    pg_bin.program_version_ok("pgbench")
    pg_bin.program_options_handling_ok("pgbench")


def test_builtin_list(pg_bin):
    # list of builtins
    _pgbench(
        pg_bin,
        "-b list",
        0,
        EMPTY,
        [r"Available builtin scripts:", r"tpcb-like", r"simple-update", r"select-only"],
        "pgbench builtin list",
    )


def test_builtin_listing(pg_bin):
    # builtin listing
    _pgbench(
        pg_bin,
        "--show-script se",
        0,
        EMPTY,
        [
            r"select-only: ",
            r"SELECT abalance FROM pgbench_accounts WHERE",
            r"(?!UPDATE)",
            r"(?!INSERT)",
        ],
        "pgbench builtin listing",
    )


# name, err, { file => contents }
# Each row is a heterogeneous [str, List[str], Dict[str, str]] record.
SCRIPT_TESTS: List[List[Any]] = [
    ["missing endif", [r"\\if without matching \\endif"], {"if-noendif.sql": "\\if 1"}],
    [
        "missing if on elif",
        [r"\\elif without matching \\if"],
        {"elif-noif.sql": "\\elif 1"},
    ],
    [
        "missing if on else",
        [r"\\else without matching \\if"],
        {"else-noif.sql": "\\else"},
    ],
    [
        "missing if on endif",
        [r"\\endif without matching \\if"],
        {"endif-noif.sql": "\\endif"},
    ],
    [
        "elif after else",
        [r"\\elif after \\else"],
        {"else-elif.sql": "\\if 1\n\\else\n\\elif 0\n\\endif"},
    ],
    [
        "else after else",
        [r"\\else after \\else"],
        {"else-else.sql": "\\if 1\n\\else\n\\else\n\\endif"},
    ],
    [
        "if syntax error",
        [r'syntax error in command "if"'],
        {"if-bad.sql": "\\if\n\\endif\n"},
    ],
    [
        "elif syntax error",
        [r'syntax error in command "elif"'],
        {"elif-bad.sql": "\\if 0\n\\elif +\n\\endif\n"},
    ],
    [
        "else syntax error",
        [r'unexpected argument in command "else"'],
        {"else-bad.sql": "\\if 0\n\\else BAD\n\\endif\n"},
    ],
    [
        "endif syntax error",
        [r'unexpected argument in command "endif"'],
        {"endif-bad.sql": "\\if 0\n\\endif BAD\n"},
    ],
    [
        "not enough arguments for least",
        [r"at least one argument expected \(least\)"],
        {"bad-least.sql": "\\set i least()\n"},
    ],
    [
        "not enough arguments for greatest",
        [r"at least one argument expected \(greatest\)"],
        {"bad-greatest.sql": "\\set i greatest()\n"},
    ],
    [
        "not enough arguments for hash",
        [r"unexpected number of arguments \(hash\)"],
        {"bad-hash-1.sql": "\\set i hash()\n"},
    ],
    [
        "too many arguments for hash",
        [r"unexpected number of arguments \(hash\)"],
        {"bad-hash-2.sql": "\\set i hash(1,2,3)\n"},
    ],
    # overflow
    [
        "bigint overflow 1",
        [r"bigint constant overflow"],
        {"overflow-1.sql": "\\set i 100000000000000000000\n"},
    ],
    [
        "double overflow 2",
        [r"double constant overflow"],
        {"overflow-2.sql": "\\set d 1.0E309\n"},
    ],
    [
        "double overflow 3",
        [r"double constant overflow"],
        {"overflow-3.sql": "\\set d .1E310\n"},
    ],
    ["set i", [r"set i 1 ", r"\^ error found here"], {"set_i_op": "\\set i 1 +\n"}],
    [
        "not enough arguments to permute",
        [r"unexpected number of arguments \(permute\)"],
        {"bad-permute-1.sql": "\\set i permute(1)\n"},
    ],
    [
        "too many arguments to permute",
        [r"unexpected number of arguments \(permute\)"],
        {"bad-permute-2.sql": "\\set i permute(1, 2, 3, 4)\n"},
    ],
]


@pytest.mark.parametrize(
    "name,err,files",
    SCRIPT_TESTS,
    ids=[t[0] for t in SCRIPT_TESTS],
)
def test_script_errors(pg_bin, tmp_path, name, err, files):
    testdir = tmp_path / "scripts"
    testdir.mkdir(exist_ok=True)
    _pgbench_scripts(
        pg_bin, str(testdir), "", 1, EMPTY, err, "pgbench option error: " + name, files
    )
