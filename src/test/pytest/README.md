# PostgreSQL Python tests (pytest)

This tree holds the Python port of PostgreSQL's Perl TAP test suite.  Tests are
written for [pytest](https://pytest.org) and run against a real PostgreSQL
server that the harness initializes, starts, and tears down for you.  Queries go
through an **in-process libpq binding** (ctypes) rather than by spawning `psql`,
so a test can open many sessions, drive async and pipeline-mode traffic, and
inspect results without subprocess overhead.

The framework deliberately mirrors the concepts of `PostgreSQL::Test::Cluster`
and `PostgreSQL::Test::Utils`, so a Perl `.pl` test maps fairly directly onto a
Python `test_*.py`.

## Layout

```
src/test/pytest/            this directory -- the shared framework
├── pyproject.toml          pytest config (also picked up from the repo root)
├── pgtap.py                pytest plugin: emits TAP for the meson harness
├── pyt/                    self-tests for the framework itself
├── libpq/                  in-process libpq binding
│   ├── bindings.py         ctypes declarations for the PQ* functions
│   ├── findlib.py          locate/load libpq at runtime
│   ├── session.py          Session: the connection + query API
│   ├── result.py           ResultData: status, columns, rows, psql-style text
│   ├── constants.py        enums (ExecStatusType, ConnStatusType, ...)
│   ├── pgnotify.py         LISTEN/NOTIFY payload parsing
│   └── oids.py / errors.py type OIDs and exception types
└── pypg/                   server + process management ("Cluster"/"Utils")
    ├── server.py           PostgresServer: init/start/stop/backup/replication
    ├── command.py          PgBin / CommandResult: run & assert on client programs
    ├── fixtures.py         the shared pytest fixtures (loaded as a plugin)
    ├── util.py             slurp_file, poll_until, TIMEOUT_DEFAULT
    ├── regress.py          pg_regress integration
    └── ldapserver.py / kerberos.py / oauthserver.py / ssl_server.py
```

The actual tests live in `pyt/` subdirectories next to the code they cover, the
same way Perl TAP tests live in `t/`:

```
src/bin/psql/pyt/test_001_basic.py
src/bin/pg_rewind/pyt/test_001_basic.py
src/test/authentication/pyt/test_001_password.py
contrib/<ext>/pyt/...
```

### Naming and discovery

* Test files are `test_NNN_<description>.py` (`test_001_basic.py`), matching the
  `NNN_name.pl` numbering of the Perl originals.
* Test functions are `test_<name>(...)`; pytest collects them automatically.
* Helper functions are prefixed with `_` so pytest does not collect them.
* `--import-mode=importlib` is set so that identically named files in different
  directories (the many `test_001_basic.py`) do not collide.

## Running the tests

### With meson (the CI / harness path)

The suite is wired into the meson build as test groups using the TAP protocol.
pytest is auto-detected: in the default `auto` mode the suite is enabled when a
usable `pytest` is found (preferring a `pytest` program on `PATH`, falling back
to `python -m pytest`).  Force it on or off with `-Dpytest=enabled|disabled`,
and override the interpreter discovery with `-DPYTEST=...`.

```bash
# build, then run a single pytest group (suite == the test_dir 'name'):
meson test -C <builddir> --suite setup        # once, to stage tmp_install
meson test -C <builddir> --suite pg_rewind
meson test -C <builddir> pytest               # the framework self-tests
```

Under meson the harness sets `TESTLOGDIR`; the `pgtap` plugin then redirects
pytest's own chatter to `$TESTLOGDIR/pytest.log` and writes a clean TAP stream
(a `1..N` plan plus one `ok`/`not ok` per test) to stdout, which is what meson's
`protocol: 'tap'` consumes.  The harness also prepends the temporary install's
`bin` to `PATH` (so `pg_config`, `initdb`, etc. resolve there) and adds this
directory to `PYTHONPATH`.

### Directly with pytest (the developer path)

When `TESTLOGDIR` is unset the plugin stays out of the way and pytest prints
normally.  You only need `pg_config` (and the matching binaries) on `PATH` and
the framework importable.  The repo-root `pyproject.toml` already sets
`pythonpath` and the required plugins, so from the repo root:

```bash
# point at the build you want to test:
export PATH=<builddir>/tmp_install/usr/local/pgsql/bin:$PATH

pytest src/test/pytest/pyt/                            # framework self-tests
pytest src/bin/pg_rewind/pyt/ -v                       # one suite, verbose
pytest src/test/pytest/pyt/test_libpq.py::test_pipeline   # one test
```

`pytest` discovers `pyproject.toml` by walking up from the current directory, so
running from the repo root (or anywhere beneath it) picks up the right config:

```toml
[tool.pytest.ini_options]
pythonpath   = ["src/test/pytest"]              # make libpq/ and pypg/ importable
addopts      = ["-p", "pgtap", "-p", "pypg.fixtures", "--import-mode=importlib"]
python_files = ["test_*.py"]
```

### Requirements

* Python 3 and `pytest` (`minversion = 7.0`).
* **No database driver** — `libpq` is bound directly via the stdlib `ctypes`
  module, so psycopg/asyncpg are not needed.
* `pexpect` is an *optional* dependency, used only by tests that drive a real
  terminal (e.g. psql tab-completion).  Those tests `pytest.importorskip(
  "pexpect")` and skip themselves when it is missing.
* External-service tests (LDAP/`slapd`, MIT Kerberos, OAuth, OpenSSL) skip
  automatically when the supporting software or build option is absent.

### Environment variables

| Variable | Meaning |
|----------|---------|
| `PG_TEST_EXTRA` | Space-separated opt-in for expensive/unsafe suites (`ssl`, `ldap`, `kerberos`, ...).  Tests that are not in the list `pytest.skip` themselves. |
| `PG_TEST_TIMEOUT_DEFAULT` | Default per-operation timeout in seconds (default `180`); backs `pypg.util.TIMEOUT_DEFAULT` and the polling helpers. |
| `TESTLOGDIR` | Set by the meson harness; switches the `pgtap` plugin into TAP-emitting mode. |

## The session framework

### Fixtures (`pypg.fixtures`, loaded via `-p pypg.fixtures`)

These are the building blocks almost every test starts from.  Servers and helper
processes are torn down automatically at the end of the test.

| Fixture | Scope | What you get |
|---------|-------|--------------|
| `pg_config`, `bindir`, `libdir` | session | located `pg_config` and its reported dirs |
| `pg_bin` | session | a `PgBin` for running client programs that need no server |
| `create_pg` | function | factory: `create_pg(name="main", *, start=True, initdb_extra=None, allows_streaming=False, has_archiving=False, has_restoring=False)` → `PostgresServer` |
| `pg` | function | a single, started `PostgresServer` (`create_pg("main")`) |
| `conn` | function | a libpq `Session` on `pg`'s `postgres` database |
| `ldap_server`, `kerberos`, `oauth_server`, `ssl_server` | function | factories for the matching external services; skip when unavailable |

A minimal test:

```python
def test_oneval(conn):
    assert conn.query_oneval("SELECT 1") == "1"

def test_two_servers(create_pg):
    primary = create_pg("primary", allows_streaming=True)
    primary.backup("my_backup")
    standby = create_pg("standby", start=False)
    standby.init_from_backup(primary, "my_backup", has_streaming=True)
    standby.start()
    primary.wait_for_catchup("standby")
```

### `PostgresServer` (`pypg.server`) — the "Cluster"

Created via the `create_pg` fixture.  Highlights (see the source for the full
list and exact signatures):

* **Lifecycle:** `init(...)`, `start()`, `stop(mode="fast")`, `restart()`,
  `reload()`, `promote()`, `kill9()`, `teardown()`, `postmaster_pid()`.
* **Config:** `append_conf(text, filename="postgresql.conf")`,
  `enable_archiving()`, `enable_streaming(root)`, `enable_restoring(root)`,
  `set_standby_mode()`, `set_recovery_mode()`.
* **Queries (in-process libpq):** `session(dbname="postgres")` (cached),
  `connect(...)` (uncached), `sql(query)` → `ResultData`, `safe_sql(query)` →
  text (raises on error), `poll_query_until(query, expected="t", ...)`.
* **Backup & replication:** `backup()`, `backup_fs_cold()`,
  `init_from_backup(...)`, `lsn(mode)`, `wait_for_catchup(...)`,
  `wait_for_replay_catchup(...)`, `wait_for_subscription_sync(...)`,
  `wait_for_event(...)`.
* **WAL:** `emit_wal(size)`, `advance_wal(n)`, `write_wal(...)`.
* **Logs:** `log_content()`, `log_position()`, `log_contains(pat, offset)`,
  `wait_for_log(pat, offset)`, `log_check(...)`.
* **Auth assertions:** `connect_ok(...)`, `connect_fails(...)`.
* **Client-program assertions** (delegating to `PgBin`): `command_ok`,
  `command_fails`, `command_like`, `command_fails_like`, `command_exit_is`,
  `command_checks_all`, `issues_sql_like`, `issues_sql_unlike`.

### `Session` (`libpq.session`) — the connection

The in-process equivalent of a `psql` session.  Returned by
`PostgresServer.session()` / `.connect()` and by the `conn` fixture.

* **Synchronous:** `do(*sql)`, `query(sql)` → `ResultData`, `query_safe(sql)`,
  `query_oneval(sql, missing_ok=False)`, `query_tuples(*sql)`.
* **Asynchronous:** `do_async(sql)`, `get_async_result()`,
  `try_get_async_result()`, `wait_for_completion()`,
  `wait_for_async_pattern(pattern, timeout)`.
* **Pipeline mode:** `enterPipelineMode()`, `exitPipelineMode()`,
  `pipelineSync()`, `query_tuples_pipelined(*queries)`.
* **LISTEN/NOTIFY:** `get_notification()`, `get_all_notifications()`.
* **Notices / errors / stderr:** `get_notices()`, `clear_notices()`,
  `get_stderr()`, `clear_stderr()`.
* **Lifecycle / introspection:** `wait_connect()`, `reconnect()`, `close()`,
  `backend_pid()`, `conn_status()`, `connstr`, `conninfo_value(keyword)`.

`query()` returns a `ResultData` dataclass:

```python
@dataclass
class ResultData:
    status: int                       # an ExecStatusType
    error_message: Optional[str]
    names:  List[str]                 # column names
    types:  List[int]                 # column type OIDs
    rows:   List[List[Optional[str]]] # values as text, NULL -> None
    psqlout: str                      # "-A -t" style rendering
```

```python
res = conn.query("SELECT n, s FROM (VALUES (1,'a'),(2,'b')) t(n,s) ORDER BY n")
assert res.names == ["n", "s"]
assert res.rows  == [["1", "a"], ["2", "b"]]
assert res.psqlout == "1|a\n2|b"
```

### `PgBin` / `CommandResult` (`pypg.command`) — running client programs

For exercising client executables (`pg_dump`, `pg_basebackup`, `psql`, ...):

```python
@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
```

`PgBin(bindir, extra_env=None)` runs a command and asserts on the outcome:
`result(cmd)`, `command_ok(cmd, msg)`, `command_fails(cmd, msg)`,
`command_exit_is(cmd, code, msg)`, `command_like(cmd, pattern, msg)`,
`command_fails_like(cmd, pattern, msg)`,
`command_checks_all(cmd, expected_ret, stdout_res, stderr_res, msg)`, plus the
`program_help_ok` / `program_version_ok` / `program_options_handling_ok`
boilerplate checks.

### Utilities (`pypg.util`)

`slurp_file(path, offset=0)`, `append_to_file(path, text)`, and
`poll_until(predicate, timeout=TIMEOUT_DEFAULT, interval=0.1)` — the building
block behind every `wait_for_*` / `poll_query_until` helper.

## Per-suite fixtures (conftest.py)

A `pyt/` directory may add its own `conftest.py` with fixtures specific to that
suite.  Examples already in the tree:

* **psql** (`src/bin/psql/pyt/conftest.py`): `interactive_psql` drives a real
  psql under a pty via `pexpect` (`InteractivePsql.query_until(until, send)`),
  `pytest.importorskip("pexpect")`-skipping when pexpect is absent.
* **pg_rewind** (`src/bin/pg_rewind/pyt/conftest.py`): a `rewind` fixture
  yielding a `RewindTest` driver (`setup_cluster`, `start_primary`,
  `create_standby`, `promote_standby`, `run_pg_rewind(mode)`, ...).
* **test_checksums** (`src/test/modules/test_checksums/pyt/conftest.py`): a
  `checksums` helper for enabling/disabling and polling data-checksum state.

Put fixtures that several suites share in `pypg/`; keep suite-specific ones in
that suite's `conftest.py`.

## Writing a new test

1. Create `…/pyt/test_NNN_<name>.py` (and a `pyt/meson.build` adding the suite
   to `tests` if the directory is new).
2. Start from the `pg` / `conn` / `create_pg` fixtures; reach for `pg_bin` to run
   client programs.
3. Prefer the in-process `Session` over shelling out to `psql`.
4. Use `wait_for_*` / `poll_query_until` / `poll_until` instead of fixed sleeps.
5. Gate anything expensive or unsafe behind `PG_TEST_EXTRA`, and
   `importorskip` / skip cleanly when an optional dependency is missing.
6. Run it directly with `pytest …/pyt/test_NNN_<name>.py -v`, then confirm it
   passes under `meson test`.
