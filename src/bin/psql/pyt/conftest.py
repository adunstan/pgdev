# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Interactive (pty-driven) psql helper for the psql test suite.

Drives a psql session through a pseudo-terminal so that psql believes it is
interactive (readline/libedit and the pager are active), using pexpect.  The
helper exposes a ``query_until`` method that sends input and reads output up to
a given pattern, with timeout tracking.

pexpect is an optional dependency: the ``interactive_psql`` fixture issues
``pytest.importorskip("pexpect")`` so tests that need a real terminal
(010_tab_completion, 030_pager) skip cleanly where it is not installed.
"""

import os

import pytest

from pypg.util import TIMEOUT_DEFAULT


class InteractivePsql:
    """A psql session driven through a pseudo-terminal.

    psql believes it is interactive (so readline/libedit and the pager are
    active).  Output includes psql's prompts and the echoed input.
    """

    def __init__(self, pexpect_mod, psql_path, node, dbname="postgres",
                 history_file=None, extra_params=None, extra_env=None,
                 dimensions=(24, 80), timeout=TIMEOUT_DEFAULT):
        self._pexpect = pexpect_mod
        self.timeout = timeout
        # True if the most recent query_until() timed out.
        self.timed_out = False

        # Since the invoked psql will believe it's interactive, it will use
        # readline/libedit if available.  Adjust the environment to prevent
        # unwanted side-effects:
        env = dict(os.environ)
        # Redirect readline history somewhere harmless (or the caller's file).
        env["PSQL_HISTORY"] = history_file or "/dev/null"
        # Ignore any ~/.inputrc that could change readline's behavior.
        env["INPUTRC"] = "/dev/null"
        # Unset TERM so readline/libedit won't emit terminal-dependent escapes.
        env.pop("TERM", None)
        # Some versions of readline inspect LS_COLORS; drop it for luck.
        env.pop("LS_COLORS", None)
        if extra_env:
            env.update(extra_env)

        args = [
            "--no-psqlrc", "--no-align", "--tuples-only",
            "--dbname", node.connstr(dbname),
        ]
        if extra_params:
            args += list(extra_params)

        self.child = pexpect_mod.spawn(
            psql_path, args, env=env, encoding="utf-8",
            timeout=timeout, dimensions=dimensions,
        )
        # Wait until psql has connected and printed its first prompt.
        self.child.expect(r"=[#>] ")

    def query_until(self, until, send):
        """Send *send*, then read output until regex *until* appears.

        *until* may be a regex string or a compiled pattern (use re.MULTILINE
        for ``$``-anchored patterns).  Returns the output produced since the
        previous match -- psql prompts and echoed input included.  On timeout,
        sets ``timed_out`` and returns whatever was captured so far instead of
        raising.
        """
        self.timed_out = False
        self.child.send(send)
        try:
            self.child.expect(until, timeout=self.timeout)
        except self._pexpect.TIMEOUT:
            self.timed_out = True
            return self.child.before
        return self.child.before + self.child.after

    def quit(self):
        """Send an explicit \\q so the pty closes cleanly."""
        if self.child.isalive():
            try:
                self.child.send("\\q\n")
                self.child.expect(self._pexpect.EOF, timeout=self.timeout)
            except (self._pexpect.TIMEOUT, self._pexpect.EOF, OSError):
                pass
        if self.child.isalive():
            self.child.close(force=True)


@pytest.fixture
def interactive_psql(bindir):
    """Factory yielding :class:`InteractivePsql` sessions.

    Skips the whole test if pexpect is not installed.  All sessions created
    through the factory are quit at teardown.
    """
    pexpect_mod = pytest.importorskip(
        "pexpect", reason="pexpect is needed to drive an interactive psql"
    )
    psql_path = os.path.join(bindir, "psql")
    sessions = []

    def _factory(node, dbname="postgres", **kwargs):
        sess = InteractivePsql(pexpect_mod, psql_path, node, dbname, **kwargs)
        sessions.append(sess)
        return sess

    yield _factory

    for sess in sessions:
        try:
            sess.quit()
        except Exception:
            pass
