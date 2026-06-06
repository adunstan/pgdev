# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Exercise the SELinux integration (sepgsql).

This is a potentially unsafe test that exercises the SELinux integration
(sepgsql).  It only runs when ``sepgsql`` is listed in PG_TEST_EXTRA AND the
host is running a suitable SELinux environment (enforcing mode, the
sepgsql-regtest policy module loaded, the right booleans turned on, and the
launching user in the unconfined_t domain).  When any of those preconditions
is not met, the test skips with a helpful diagnostic, since the operator
opted in but the environment was not actually ready.

The regression tests themselves are driven by pg_regress with a custom
``launcher`` script, which uses ``runcon`` to enter the right security
context.
"""

import os
import subprocess

import pytest

# 1) PG_TEST_EXTRA must opt in to this potentially unsafe test.
if "sepgsql" not in os.environ.get("PG_TEST_EXTRA", "").split():
    pytest.skip(
        "Potentially unsafe test sepgsql not enabled in PG_TEST_EXTRA",
        allow_module_level=True,
    )


# Directory holding this test (where the launcher script lives), and the
# sepgsql source directory.
_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_SEPGSQL_DIR = os.path.abspath(os.path.join(_TEST_DIR, ".."))


def _run(cmd, **kwargs):
    """Run *cmd* (a string for shell, list otherwise) capturing all output.

    Returns the completed process; never raises on a nonzero exit.
    """
    shell = isinstance(cmd, str)
    return subprocess.run(
        cmd,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        **kwargs,
    )


def _capture(cmd):
    """Run *cmd* and return stdout text (empty string on failure)."""
    proc = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _check_selinux_environment():
    """Verify the SELinux environment, skipping with a diagnostic if unready.

    Runs a chain of preflight checks.  Each failed check calls pytest.skip()
    with an explanation so a host that opted in but is not actually ready does
    not produce a spurious failure.
    """
    # matchpathcon must be present to assess whether the installation
    # environment is OK.
    if _run("matchpathcon -n . >/dev/null 2>&1").returncode != 0:
        pytest.skip(
            "The matchpathcon command must be available.\n"
            "Please install it or update your PATH to include it\n"
            "(it is typically in '/usr/sbin', which might not be in your "
            "PATH).\n"
            "matchpathcon is typically included in the libselinux-utils "
            "package."
        )

    # runcon must be present to launch psql using the correct environment.
    if _run("runcon --help >/dev/null 2>&1").returncode != 0:
        pytest.skip(
            "The runcon command must be available.\n"
            "runcon is typically included in the coreutils package."
        )

    # check sestatus too, since that lives in yet another package.
    if _run("sestatus >/dev/null 2>&1").returncode != 0:
        pytest.skip(
            "The sestatus command must be available.\n"
            "sestatus is typically included in the policycoreutils package."
        )

    # check that the user is running in the unconfined_t domain.
    id_z = _capture("id -Z 2>/dev/null")
    parts = id_z.split(":")
    domain = parts[2] if len(parts) > 2 else ""
    if domain != "unconfined_t":
        pytest.skip(
            "The regression tests must be launched from the unconfined_t "
            "domain.\n"
            "The unconfined_t domain is typically the default domain for "
            "user shell processes."
        )

    # SELinux must be configured in enforcing mode.
    current_mode = ""
    for line in _capture("LANG=C sestatus").splitlines():
        if line.startswith("Current mode:"):
            current_mode = line.split(":", 1)[1].strip()
            break
    if current_mode == "enforcing":
        pass  # OK
    elif current_mode in ("permissive", "disabled"):
        pytest.skip(
            "Before running the regression tests, SELinux must be enabled "
            "and must be running in enforcing mode.\n"
            "If SELinux is currently running in permissive mode, you can "
            "switch to enforcing mode using 'sudo setenforce 1'."
        )
    else:
        pytest.skip(
            "Unable to determine the current selinux operating mode.  "
            "Please verify that the sestatus command is installed and in "
            "your PATH."
        )

    # 'sepgsql-regtest' policy module must be loaded.
    selinux_mnt = ""
    for line in _capture("sestatus").splitlines():
        if line.startswith("SELinuxfs mount:"):
            selinux_mnt = line.split(":", 1)[1].strip()
            break
    if selinux_mnt == "":
        pytest.skip(
            "Unable to find SELinuxfs mount point.\n"
            "The sestatus command should report the location where "
            "SELinuxfs is mounted, but did not do so."
        )
    if not os.path.exists(
        os.path.join(selinux_mnt, "booleans", "sepgsql_regression_test_mode")
    ):
        pytest.skip(
            "The 'sepgsql-regtest' policy module appears not to be "
            "installed.\nWithout this policy installed, the regression "
            "tests will fail.\nYou can install it with:\n"
            "  $ make -f /usr/share/selinux/devel/Makefile\n"
            "  $ sudo semodule -u sepgsql-regtest.pp"
        )

    # Verify that the required SELinux booleans are active.
    for policy in ("sepgsql_regression_test_mode", "sepgsql_enable_users_ddl"):
        out = _capture(["getsebool", policy])
        fields = out.split()
        status = fields[2] if len(fields) > 2 else ""
        if status != "on":
            pytest.skip(
                f"The SELinux boolean '{policy}' must be turned on in order "
                "to enable the rules necessary to run the regression "
                "tests.\n"
                f"  $ sudo setsebool {policy} on"
            )


def test_001_sepgsql(create_pg):
    # checking selinux environment
    _check_selinux_environment()

    #
    # checking complete - let's run the tests
    #

    # The single-user installation step reads sepgsql.sql from the contrib
    # share directory, which the meson/make harness exports as
    # share_contrib_dir (cf. test_env in meson.build).
    share_contrib_dir = os.environ.get("share_contrib_dir")
    if not share_contrib_dir:
        pytest.skip("share_contrib_dir not set in the environment")

    pg_regress = os.environ.get("PG_REGRESS")
    if not pg_regress:
        pytest.skip("PG_REGRESS not set; pg_regress is unavailable")

    node = create_pg("test", start=False)
    node.append_conf("log_statement=none")

    # Run the sepgsql installation script in single-user mode against
    # template0.  postgres --single reads the SQL from stdin.
    sepgsql_sql = os.path.join(share_contrib_dir, "sepgsql.sql")
    with open(sepgsql_sql, "r", encoding="utf-8") as fh:
        sql = fh.read()
    proc = subprocess.run(
        [
            os.path.join(node.bindir, "postgres"),
            "--single",
            "-F",
            "-c", "exit_on_error=true",
            "-D", node.data_dir,
            "template0",
        ],
        input=sql,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"sepgsql installation script\n{proc.stdout}"
    )

    node.append_conf("shared_preload_libraries=sepgsql")
    node.start()

    tests = ["label", "dml", "ddl", "alter", "misc"]

    # Check if the truncate permission exists in the loaded policy, and if so,
    # run the truncate test.
    if os.path.isfile("/sys/fs/selinux/class/db_table/perms/truncate"):
        tests.append("truncate")

    # Drive the sepgsql regression tests via pg_regress with the launcher
    # script (which uses runcon to enter the right security context).  Run
    # from the sepgsql source directory so that --inputdir '.' and the
    # ./launcher relative path resolve.  pg_regress is given this node's
    # host/port so the launched psql connects to it.
    env = dict(os.environ)
    env["PGHOST"] = node.host
    env["PGPORT"] = str(node.port)
    proc = subprocess.run(
        [
            pg_regress,
            "--bindir", "",
            "--inputdir", ".",
            "--launcher", "./launcher",
            f"--host={node.host}",
            f"--port={node.port}",
            *tests,
        ],
        cwd=_SEPGSQL_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"sepgsql tests\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
