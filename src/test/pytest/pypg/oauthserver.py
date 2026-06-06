# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Launches the mock OAuth authorization server used by the oauth tests.

The actual server is the Python daemon t/oauth_server.py; this is just the glue
that starts it, reads the ephemeral port it prints to stdout, and stops it.
"""

import os
import signal
import subprocess
import sys


class OAuthServer:
    """A running instance of the mock OAuth provider (oauth_server.py).

    *script* is the path to oauth_server.py; it is run with its grandparent
    (the module source dir) as the working directory, so it invokes
    "t/oauth_server.py" from the test directory.
    """

    def __init__(self, script):
        python = os.environ.get("PYTHON") or sys.executable
        script = os.path.abspath(script)
        cwd = os.path.dirname(os.path.dirname(script))  # the module source dir
        rel = os.path.join("t", os.path.basename(script))

        # The daemon's lifetime is managed by stop(), not a with block.
        # pylint: disable-next=consider-using-with
        self._proc = subprocess.Popen(
            [python, rel],
            cwd=cwd,
            stdout=subprocess.PIPE,
            text=True,
        )
        # The daemon prints its port then closes stdout, so a full read blocks
        # only until the port is known.
        line = self._proc.stdout.readline()
        if not line.strip().isdigit():
            raise RuntimeError(f"server did not advertise a valid port: {line!r}")
        self.port = int(line.strip())

    def stop(self):
        if self._proc is None:
            return
        try:
            self._proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self._proc.stdout.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        self._proc.wait()
        self._proc = None
