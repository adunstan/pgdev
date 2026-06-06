# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Running programs and asserting on their results.

A :class:`PgBin` runs binaries from a given bindir (optionally with extra
environment, e.g. a node's PGHOST/PGPORT) and asserts on exit code, stdout and
stderr.  Failures raise AssertionError so pytest/pgtap report them.
"""

import os
from dataclasses import dataclass
from typing import Optional, Sequence

from .util import run_captured


@dataclass
class CommandResult:
    """Outcome of running a command."""

    returncode: int
    stdout: str
    stderr: str


# Mirrors program_help_ok's line-length convention.
_MAX_HELP_LINE_LENGTH = 95


def _describe(cmd: Sequence[str], result: CommandResult) -> str:
    return "command: {}\nexit code: {}\nstderr:\n{}\nstdout:\n{}".format(
        " ".join(str(c) for c in cmd), result.returncode, result.stderr, result.stdout
    )


class PgBin:
    """Runs PostgreSQL binaries located in *bindir*."""

    def __init__(self, bindir, extra_env: Optional[dict] = None):
        self.bindir = str(bindir)
        self.extra_env = dict(extra_env or {})

    def command_env(self, extra_env: Optional[dict]) -> dict:
        env = dict(os.environ)
        env.update(self.extra_env)
        if extra_env:
            env.update(extra_env)
        # A None value means "unset this variable" (e.g. to drop an inherited
        # TZ), since subprocess env values must all be strings.
        return {k: v for k, v in env.items() if v is not None}

    def resolve(self, name):
        """Resolve a program name to its path within bindir if present."""
        candidate = os.path.join(self.bindir, name)
        return candidate if os.path.exists(candidate) else name

    def result(self, cmd: Sequence[str], *, extra_env=None) -> CommandResult:
        """Run *cmd* (list) and capture its result. cmd[0] is resolved in bindir."""
        argv = [self.resolve(cmd[0]), *map(str, cmd[1:])]
        print("# Running: " + " ".join(argv))
        # Capture via files, not pipes: a program that launches a server (e.g.
        # "pg_ctl start") leaves the postmaster holding the pipe open on
        # Windows, which would deadlock the read.  Output is decoded leniently
        # since programs may emit non-UTF-8 bytes (e.g. LATIN1 object names)
        # that we only regex-match.
        returncode, stdout, stderr = run_captured(argv, env=self.command_env(extra_env))
        return CommandResult(returncode, stdout, stderr)

    # -- command_* assertions -----------------------------------------------

    def command_ok(self, cmd, msg=None, *, extra_env=None) -> CommandResult:
        res = self.result(cmd, extra_env=extra_env)
        assert res.returncode == 0, (
            (msg or "command should succeed") + "\n" + _describe(cmd, res)
        )
        return res

    def command_fails(self, cmd, msg=None, *, extra_env=None) -> CommandResult:
        res = self.result(cmd, extra_env=extra_env)
        assert res.returncode != 0, (
            (msg or "command should fail") + "\n" + _describe(cmd, res)
        )
        return res

    def command_exit_is(self, cmd, code, msg=None, *, extra_env=None) -> CommandResult:
        res = self.result(cmd, extra_env=extra_env)
        assert res.returncode == code, (
            (msg or f"exit code should be {code}") + "\n" + _describe(cmd, res)
        )
        return res

    def command_like(self, cmd, pattern, msg=None, *, extra_env=None) -> CommandResult:
        import re

        res = self.result(cmd, extra_env=extra_env)
        assert res.returncode == 0, (
            (msg or "command should succeed") + "\n" + _describe(cmd, res)
        )
        assert res.stderr == "", (msg or "no stderr") + "\n" + _describe(cmd, res)
        assert re.search(pattern, res.stdout), (
            (msg or "stdout should match") + f" /{pattern}/\n" + _describe(cmd, res)
        )
        return res

    def command_fails_like(
        self, cmd, pattern, msg=None, *, extra_env=None
    ) -> CommandResult:
        import re

        res = self.result(cmd, extra_env=extra_env)
        assert res.returncode != 0, (
            (msg or "command should fail") + "\n" + _describe(cmd, res)
        )
        assert re.search(pattern, res.stderr), (
            (msg or "stderr should match") + f" /{pattern}/\n" + _describe(cmd, res)
        )
        return res

    def command_checks_all(
        self, cmd, expected_ret, stdout_res, stderr_res, msg=None, *, extra_env=None
    ) -> CommandResult:
        """Check exit code plus a list of stdout and stderr regexes."""
        import re

        res = self.result(cmd, extra_env=extra_env)
        label = msg or "command"
        assert res.returncode == expected_ret, (
            f"{label} status (got {res.returncode} vs expected {expected_ret})\n"
            + _describe(cmd, res)
        )
        for pattern in stdout_res:
            assert re.search(
                pattern, res.stdout
            ), f"{label} stdout /{pattern}/\n" + _describe(cmd, res)
        for pattern in stderr_res:
            assert re.search(
                pattern, res.stderr
            ), f"{label} stderr /{pattern}/\n" + _describe(cmd, res)
        return res

    # -- program_* assertions -----------------------------------------------

    def program_help_ok(self, name):
        res = self.result([name, "--help"])
        assert res.returncode == 0, f"{name} --help exit code 0\n" + _describe(
            [name, "--help"], res
        )
        assert res.stdout != "", f"{name} --help goes to stdout"
        assert res.stderr == "", f"{name} --help nothing to stderr:\n{res.stderr}"
        long_lines = [
            ln for ln in res.stdout.splitlines() if len(ln) > _MAX_HELP_LINE_LENGTH
        ]
        assert not long_lines, (
            f"{name} --help maximum line length (>{_MAX_HELP_LINE_LENGTH}):\n"
            + "\n".join(long_lines)
        )

    def program_version_ok(self, name):
        res = self.result([name, "--version"])
        assert res.returncode == 0, f"{name} --version exit code 0\n" + _describe(
            [name, "--version"], res
        )
        assert res.stdout != "", f"{name} --version goes to stdout"
        assert res.stderr == "", f"{name} --version nothing to stderr:\n{res.stderr}"

    def program_options_handling_ok(self, name):
        res = self.result([name, "--not-a-valid-option"])
        assert res.returncode != 0, f"{name} with invalid option nonzero exit code"
        assert res.stderr != "", f"{name} with invalid option prints error message"
