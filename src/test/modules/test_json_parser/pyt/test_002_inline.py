# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test the incremental JSON parser on small inline inputs.

Test success or failure of the incremental (table-driven) JSON parser for a
variety of small inputs.
"""

import re
import shutil
import subprocess

import pytest

# (name, json-bytes, error-regex-or-None).  The JSON payloads are bytes so we
# can include raw, non-UTF-8 bytes (e.g. the 0xF5 case) exactly as fed to the
# parser.  Error patterns are compiled as bytes for the same reason.
CASES = [
    ("number", b"12345", None),
    ("string", b'"hello"', None),
    ("false", b"false", None),
    ("true", b"true", None),
    ("null", b"null", None),
    ("empty object", b"{}", None),
    ("empty array", b"[]", None),
    ("array with number", b"[12345]", None),
    ("array with numbers", b"[12345,67890]", None),
    ("array with null", b"[null]", None),
    ("array with string", b'["hello"]', None),
    ("array with boolean", b"[false]", None),
    ("single pair", b'{"key": "value"}', None),
    ("heavily nested array", b"[" * 3200 + b"]" * 3200, None),
    ("serial escapes", b'"' + b"\\" * 8 + b'"', None),
    (
        "interrupted escapes",
        b'"' + b"\\" * 3 + b'"' + b"\\" * 5 + b'"' + b"\\" * 2 + b'"',
        None,
    ),
    ("whitespace", b'     ""     ', None),
    ("unclosed empty object", b"{", rb"input string ended unexpectedly"),
    ("bad key", b"{{", rb'Expected string or "}", but found "\{"'),
    ("bad key", b"{{}", rb'Expected string or "}", but found "\{"'),
    ("numeric key", b"{1234: 2}", rb'Expected string or "}", but found "1234"'),
    (
        "second numeric key",
        b'{"a": "a", 1234: 2}',
        rb'Expected string, but found "1234"',
    ),
    (
        "unclosed object with pair",
        b'{"key": "value"',
        rb"input string ended unexpectedly",
    ),
    ("missing key value", b'{"key": }', rb'Expected JSON value, but found "}"'),
    ("missing colon", b'{"key" 12345}', rb'Expected ":", but found "12345"'),
    (
        "missing comma",
        b'{"key": 12345 12345}',
        rb'Expected "," or "}", but found "12345"',
    ),
    ("overnested array", b"[" * 6401, rb"maximum permitted depth is 6400"),
    ("overclosed array", b"[]]", rb'Expected end of input, but found "]"'),
    (
        "unexpected token in array",
        b"[ }}} ]",
        rb'Expected array element or "]", but found "}"',
    ),
    ("junk punctuation", b"[ ||| ]", rb'Token "\|" is invalid'),
    (
        "missing comma in array",
        b"[123 123]",
        rb'Expected "," or "]", but found "123"',
    ),
    ("misspelled boolean", b"tru", rb'Token "tru" is invalid'),
    ("misspelled boolean in array", b"[tru]", rb'Token "tru" is invalid'),
    ("smashed top-level scalar", b"12zz", rb'Token "12zz" is invalid'),
    ("smashed scalar in array", b"[12zz]", rb'Token "12zz" is invalid'),
    (
        "unknown escape sequence",
        b'"hello\\vworld"',
        rb'Escape sequence "\\v" is invalid',
    ),
    (
        "unescaped control",
        b'"hello\tworld"',
        rb"Character with value 0x09 must be escaped",
    ),
    (
        "incorrect escape count",
        b'"' + b"\\" * 7 + b'"',
        rb'Token ""\\\\\\\\\\\\\\"" is invalid',
    ),
    # Case with three bytes: double-quote, backslash and <f5>.
    # Both invalid-token and invalid-escape are possible errors, because for
    # smaller chunk sizes the incremental parser skips the string parsing when
    # it cannot find an ending quote.
    (
        "incomplete UTF-8 sequence",
        b'"\\\xf5',
        rb'(Token|Escape sequence) ""?\\\xf5" is invalid',
    ),
]

# The four executable invocations exercised by this test.
EXES = [
    ["test_json_parser_incremental"],
    ["test_json_parser_incremental", "-o"],
    ["test_json_parser_incremental_shlib"],
    ["test_json_parser_incremental_shlib", "-o"],
]


def _exe_id(exe):
    return "_".join(exe).replace("-", "")


def _case_id(case):
    name, json, _err = case
    return f"{name}_{len(json)}"


def _run(exe, chunk, fname):
    """Run the parser in -r mode and return (stdout, stderr) as byte lists.

    Split the null-separated runs of output.  In -r
    mode the program writes a trailing null after each of the `chunk`
    iterations, so a trailing empty fragment from the final null is dropped.
    """
    argv = [shutil.which(exe[0]) or exe[0], *exe[1:], "-r", str(chunk), str(fname)]
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def split(buf):
        parts = buf.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return parts

    return split(proc.stdout), split(proc.stderr)


@pytest.mark.parametrize("exe", EXES, ids=_exe_id)
@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_002_inline(exe, case, tmp_path):
    if not shutil.which(exe[0]):
        pytest.skip(f"{exe[0]} not found on PATH")

    name, json, error = case

    # Test the input with chunk sizes from max(input_size, 64) down to 1.
    chunk = len(json)
    if chunk > 64:
        chunk = 64

    fname = tmp_path / "input.json"
    fname.write_bytes(json)

    stdout, stderr = _run(exe, chunk, fname)

    assert len(stdout) == chunk, f"{name}: stdout has correct number of entries"
    assert len(stderr) == chunk, f"{name}: stderr has correct number of entries"

    i = 0
    for size in reversed(range(1, chunk + 1)):
        if error is not None:
            assert b"SUCCESS" not in stdout[i], (
                f"{name}, chunk size {size}: test fails"
            )
            assert re.search(error, stderr[i]), (
                f"{name}, chunk size {size}: correct error output: {stderr[i]!r}"
            )
        else:
            assert b"SUCCESS" in stdout[i], (
                f"{name}, chunk size {size}: test succeeds"
            )
            assert stderr[i] == b"", f"{name}, chunk size {size}: no error output"
        i += 1
