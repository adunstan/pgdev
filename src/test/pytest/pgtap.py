# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""A pytest plugin that emits TAP output for the meson/prove harness.

When the meson test harness runs the suite it sets TESTLOGDIR.  In that case
this plugin hijacks the standard streams as early as possible: pytest's own
output is redirected to ``$TESTLOGDIR/pytest.log`` and the TAP stream (plan
line plus one ``ok``/``not ok`` per test) is written to the real stdout, which
is what meson's ``protocol: 'tap'`` consumes.

When TESTLOGDIR is unset (a developer running ``pytest`` directly) the plugin
stays out of the way and lets pytest print normally.
"""

import os
import sys

import pytest

_enabled = False
# Per-test accumulated state, keyed by nodeid, filled across setup/call/teardown.
_results: dict = {}


class _Tap:
    def __init__(self):
        self.count = 0

    def _emit(self, *args):
        print(*args, file=sys.__stdout__)

    def plan(self, num):
        self._emit(f"1..{num}")

    def ok(self, name):
        self.count += 1
        self._emit(f"ok {self.count} - {name}")

    def not_ok(self, name, details=""):
        self.count += 1
        self._emit(f"not ok {self.count} - {name}")
        # meson does not surface TAP diagnostics reliably, so send the details
        # to the real stderr where they show up in the failure report.
        if details:
            print(details, file=sys.__stderr__)

    def skip(self, name, reason):
        self.count += 1
        suffix = f" # skip {reason}" if reason else " # skip"
        self._emit(f"ok {self.count} - {name}{suffix}")


_tap = _Tap()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):  # noqa: ARG001 (pytest calls with config)
    global _enabled  # pylint: disable=global-statement
    logdir = os.getenv("TESTLOGDIR")
    if not logdir:
        return
    _enabled = True
    os.makedirs(logdir, exist_ok=True)
    logpath = os.path.join(logdir, "pytest.log")
    # The stream intentionally stays open as stdout/stderr for the whole run.
    # pylint: disable-next=consider-using-with
    stream = open(logpath, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
    sys.stdout = stream
    sys.stderr = stream


def pytest_collection_finish(session):
    if _enabled:
        _tap.plan(len(session.items))


def pytest_runtest_logreport(report):
    if not _enabled:
        return
    rec = _results.setdefault(
        report.nodeid, {"failed": False, "skipped": False, "reason": "", "details": ""}
    )
    if report.failed:
        rec["failed"] = True
        rec["details"] += report.longreprtext
    elif report.skipped:
        rec["skipped"] = True
        rec["reason"] = _skip_reason(report)


def pytest_runtest_logfinish(nodeid, location):  # noqa: ARG001
    if not _enabled:
        return
    rec = _results.pop(
        nodeid, {"failed": False, "skipped": False, "reason": "", "details": ""}
    )
    if rec["skipped"]:
        _tap.skip(nodeid, rec["reason"])
    elif rec["failed"]:
        _tap.not_ok(nodeid, rec["details"])
    else:
        _tap.ok(nodeid)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if not _enabled:
        return
    # A whole-module skip (``pytest.skip(..., allow_module_level=True)``)
    # collects zero test items, which pytest reports as exit code 5
    # (NO_TESTS_COLLECTED).  Under the meson harness that is a legitimate
    # "entire test skipped" outcome, and pytest_collection_finish has already
    # emitted a ``1..0`` TAP plan that
    # meson reads as a skip.  Map the exit code to success so meson does not
    # treat the skip as an error.
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK


def _skip_reason(report):
    longrepr = getattr(report, "longrepr", None)
    # Skips are reported as a (path, lineno, "Skipped: reason") tuple.
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return ""
