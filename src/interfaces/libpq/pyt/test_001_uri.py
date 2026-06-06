# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Table-driven test of libpq URI / connection-string parsing.  Each case feeds an
input string to the build-dir ``libpq_uri_regress`` program (a single argv
argument) and checks the normalized stdout, the stderr, and the exit status.

``libpq_uri_regress`` lives in builddir/src/interfaces/libpq/test/ and is found
via PATH (PgBin falls back to the bare name when it is not in bindir).  No
server is needed.
"""

import pytest

# List of URI tests.  For each test the first element is the input string, the
# second the expected stdout and the third the expected stderr.  Optionally, a
# fourth element is a dict of key/value pairs which override environment
# variables for the duration of the test.
TESTS = [
    (
        r"postgresql://uri-user:secret@host:12345/db",
        r"user='uri-user' password='secret' dbname='db' host='host' port='12345' (inet)",
        r"",
    ),
    (
        r"postgresql://uri-user@host:12345/db",
        r"user='uri-user' dbname='db' host='host' port='12345' (inet)",
        r"",
    ),
    (
        r"postgresql://uri-user@host/db",
        r"user='uri-user' dbname='db' host='host' (inet)",
        r"",
    ),
    (
        r"postgresql://host:12345/db",
        r"dbname='db' host='host' port='12345' (inet)",
        r"",
    ),
    (r"postgresql://host/db", r"dbname='db' host='host' (inet)", r""),
    (
        r"postgresql://uri-user@host:12345/",
        r"user='uri-user' host='host' port='12345' (inet)",
        r"",
    ),
    (
        r"postgresql://uri-user@host/",
        r"user='uri-user' host='host' (inet)",
        r"",
    ),
    (r"postgresql://uri-user@", r"user='uri-user' (local)", r""),
    (r"postgresql://host:12345/", r"host='host' port='12345' (inet)", r""),
    (r"postgresql://host:12345", r"host='host' port='12345' (inet)", r""),
    (r"postgresql://host/db", r"dbname='db' host='host' (inet)", r""),
    (r"postgresql://host/", r"host='host' (inet)", r""),
    (r"postgresql://host", r"host='host' (inet)", r""),
    (r"postgresql://", r"(local)", r""),
    (
        r"postgresql://?hostaddr=127.0.0.1",
        r"hostaddr='127.0.0.1' (inet)",
        r"",
    ),
    (
        r"postgresql://example.com?hostaddr=63.1.2.4",
        r"host='example.com' hostaddr='63.1.2.4' (inet)",
        r"",
    ),
    (r"postgresql://%68ost/", r"host='host' (inet)", r""),
    (
        r"postgresql://host/db?user=uri-user",
        r"user='uri-user' dbname='db' host='host' (inet)",
        r"",
    ),
    (
        r"postgresql://host/db?user=uri-user&port=12345",
        r"user='uri-user' dbname='db' host='host' port='12345' (inet)",
        r"",
    ),
    (
        r"postgresql://host/db?u%73er=someotheruser&port=12345",
        r"user='someotheruser' dbname='db' host='host' port='12345' (inet)",
        r"",
    ),
    (
        r"postgresql://host/db?u%7aer=someotheruser&port=12345",
        r"",
        r'libpq_uri_regress: invalid URI query parameter: "uzer"',
    ),
    (
        r"postgresql://host:12345?user=uri-user",
        r"user='uri-user' host='host' port='12345' (inet)",
        r"",
    ),
    (
        r"postgresql://host?user=uri-user",
        r"user='uri-user' host='host' (inet)",
        r"",
    ),
    (
        # Leading and trailing spaces, works.
        r"postgresql://host?  user = uri-user & port  = 12345 ",
        r"user='uri-user' host='host' port='12345' (inet)",
        r"",
    ),
    (
        # Trailing data in parameter.
        r"postgresql://host?  user user  =  uri  & port = 12345 12 ",
        r"",
        r'libpq_uri_regress: unexpected spaces found in "  user user  ", use percent-encoded spaces (%20) instead',
    ),
    (
        # Trailing data in value.
        r"postgresql://host?  user  =  uri-user  & port = 12345 12 ",
        r"",
        r'libpq_uri_regress: unexpected spaces found in " 12345 12 ", use percent-encoded spaces (%20) instead',
    ),
    (r"postgresql://host?", r"host='host' (inet)", r""),
    (
        r"postgresql://[::1]:12345/db",
        r"dbname='db' host='::1' port='12345' (inet)",
        r"",
    ),
    (r"postgresql://[::1]/db", r"dbname='db' host='::1' (inet)", r""),
    (
        r"postgresql://[2001:db8::1234]/",
        r"host='2001:db8::1234' (inet)",
        r"",
    ),
    (
        r"postgresql://[200z:db8::1234]/",
        r"host='200z:db8::1234' (inet)",
        r"",
    ),
    (r"postgresql://[::1]", r"host='::1' (inet)", r""),
    (r"postgres://", r"(local)", r""),
    (r"postgres:///", r"(local)", r""),
    (r"postgres:///db", r"dbname='db' (local)", r""),
    (
        r"postgres://uri-user@/db",
        r"user='uri-user' dbname='db' (local)",
        r"",
    ),
    (
        r"postgres://?host=/path/to/socket/dir",
        r"host='/path/to/socket/dir' (local)",
        r"",
    ),
    (
        r"postgresql://host?uzer=",
        r"",
        r'libpq_uri_regress: invalid URI query parameter: "uzer"',
    ),
    (
        r"postgre://",
        r"",
        r'libpq_uri_regress: missing "=" after "postgre://" in connection info string',
    ),
    (
        r"postgres://[::1",
        r"",
        r'libpq_uri_regress: end of string reached when looking for matching "]" in IPv6 host address in URI: "postgres://[::1"',
    ),
    (
        r"postgres://[]",
        r"",
        r'libpq_uri_regress: IPv6 host address may not be empty in URI: "postgres://[]"',
    ),
    (
        r"postgres://[::1]z",
        r"",
        r'libpq_uri_regress: unexpected character "z" at position 17 in URI (expected ":" or "/"): "postgres://[::1]z"',
    ),
    (
        r"postgresql://host?zzz",
        r"",
        r'libpq_uri_regress: missing key/value separator "=" in URI query parameter: "zzz"',
    ),
    (
        r"postgresql://host?value1&value2",
        r"",
        r'libpq_uri_regress: missing key/value separator "=" in URI query parameter: "value1"',
    ),
    (
        r"postgresql://host?key=key=value",
        r"",
        r'libpq_uri_regress: extra key/value separator "=" in URI query parameter: "key"',
    ),
    (
        r"postgres://host?dbname=%XXfoo",
        r"",
        r'libpq_uri_regress: invalid percent-encoded token: "%XXfoo"',
    ),
    (
        r"postgresql://a%00b",
        r"",
        r'libpq_uri_regress: forbidden value %00 in percent-encoded value: "a%00b"',
    ),
    (
        r"postgresql://%zz",
        r"",
        r'libpq_uri_regress: invalid percent-encoded token: "%zz"',
    ),
    (
        r"postgresql://%1",
        r"",
        r'libpq_uri_regress: invalid percent-encoded token: "%1"',
    ),
    (
        r"postgresql://%",
        r"",
        r'libpq_uri_regress: invalid percent-encoded token: "%"',
    ),
    (r"postgres://@host", r"host='host' (inet)", r""),
    (r"postgres://host:/", r"host='host' (inet)", r""),
    (r"postgres://:12345/", r"port='12345' (local)", r""),
    (
        r"postgres://otheruser@?host=/no/such/directory",
        r"user='otheruser' host='/no/such/directory' (local)",
        r"",
    ),
    (
        r"postgres://otheruser@/?host=/no/such/directory",
        r"user='otheruser' host='/no/such/directory' (local)",
        r"",
    ),
    (
        r"postgres://otheruser@:12345?host=/no/such/socket/path",
        r"user='otheruser' host='/no/such/socket/path' port='12345' (local)",
        r"",
    ),
    (
        r"postgres://otheruser@:12345/db?host=/path/to/socket",
        r"user='otheruser' dbname='db' host='/path/to/socket' port='12345' (local)",
        r"",
    ),
    (
        r"postgres://:12345/db?host=/path/to/socket",
        r"dbname='db' host='/path/to/socket' port='12345' (local)",
        r"",
    ),
    (
        r"postgres://:12345?host=/path/to/socket",
        r"host='/path/to/socket' port='12345' (local)",
        r"",
    ),
    (
        r"postgres://%2Fvar%2Flib%2Fpostgresql/dbname",
        r"dbname='dbname' host='/var/lib/postgresql' (local)",
        r"",
    ),
    # Usually the default sslmode is 'prefer' (for libraries with SSL) or
    # 'disable' (for those without).  This default changes to 'verify-full' if
    # the system CA store is in use.
    (
        r"postgresql://host?sslmode=disable",
        r"host='host' sslmode='disable' (inet)",
        r"",
        {"PGSSLROOTCERT": "system"},
    ),
    (
        r"postgresql://host?sslmode=prefer",
        r"host='host' sslmode='prefer' (inet)",
        r"",
        {"PGSSLROOTCERT": "system"},
    ),
    (
        r"postgresql://host?sslmode=verify-full",
        r"host='host' (inet)",
        r"",
        {"PGSSLROOTCERT": "system"},
    ),
]


@pytest.mark.parametrize(
    "case",
    TESTS,
    ids=[t[0] for t in TESTS],
)
def test_001_uri(pg_bin, case):
    uri = case[0]
    expect_stdout = case[1]
    expect_stderr = case[2]
    envvars = case[3] if len(case) > 3 else {}

    res = pg_bin.result(["libpq_uri_regress", uri], extra_env=envvars)

    # IPC::Run chomps trailing newlines off the captured streams.
    got_stdout = res.stdout.rstrip("\n")
    got_stderr = res.stderr.rstrip("\n")

    # The expected exit status is success exactly when the expected stderr is
    # empty.
    expect_success = expect_stderr == ""

    assert got_stdout == expect_stdout, f"stdout for {uri}"
    assert got_stderr == expect_stderr, f"stderr for {uri}"
    assert (res.returncode == 0) == expect_success, f"exit status for {uri}"
