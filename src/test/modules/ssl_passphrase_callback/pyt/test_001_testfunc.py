# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Exercises the ssl_passphrase_func contrib module.

Tests an encrypted server key
whose passphrase is supplied by the module's TLS init hook callback.  Checks
that the server starts with the correct passphrase, warns when
ssl_passphrase_command is also set, fails to start with the wrong passphrase,
and (with SNI) bypasses the hook.
"""

import os
import re
import shutil

# Directory holding the module's own server.crt / server.key (the source dir,
# one level up from this pyt/ directory).  We anchor them to the module
# directory so the test works regardless of where pytest is invoked from.
_MODULE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _install_cert(node):
    """Copy the encrypted server key + cert into the data dir."""
    ddir = node.data_dir
    shutil.copy(os.path.join(_MODULE_DIR, "server.crt"), ddir)
    shutil.copy(os.path.join(_MODULE_DIR, "server.key"), ddir)
    os.chmod(os.path.join(ddir, "server.key"), 0o600)


def test_001_testfunc(create_pg, ssl_server):
    # The ssl_server fixture skips the test unless with_ssl=openssl.  It also
    # provides the LibreSSL check.
    libressl = ssl_server.is_libressl()

    rot13pass = "SbbOnE1"

    # see the Makefile/meson.build for how the certificate and key were
    # generated (the clear passphrase is "FooBaR1", rot13 of "SbbOnE1").
    node = create_pg("main", start=False)
    node.append_conf(f"ssl_passphrase.passphrase = '{rot13pass}'")
    node.append_conf("shared_preload_libraries = 'ssl_passphrase_func'")
    node.append_conf("ssl = 'on'")

    ddir = node.data_dir

    # install certificate and protected key
    _install_cert(node)

    node.start()

    # if the server is running we must have successfully transformed the
    # passphrase
    assert os.path.exists(node.pidfile), "postgres started"

    node.stop("fast")

    # should get a warning if ssl_passphrase_command is set
    node.rotate_logfile()

    node.append_conf("ssl_passphrase_command = 'echo spl0tz'")

    node.start()

    node.stop("fast")

    log_contents = node.log_content()

    assert re.search(
        r'WARNING.*"ssl_passphrase_command" setting ignored by '
        r"ssl_passphrase_func module",
        log_contents,
    ), "ssl_passphrase_command set warning"

    # set the wrong passphrase
    node.append_conf("ssl_passphrase.passphrase = 'blurfl'")

    # try to start the server again -- with a bad passphrase the server should
    # not start.  start(fail_ok=True) returns False instead of raising.
    started = node.start(fail_ok=True)

    assert not started, "pg_ctl fails with bad passphrase"
    assert not os.path.exists(node.pidfile), "postgres not started with bad passphrase"

    # just in case
    node.stop("fast")

    # Make sure the hook is bypassed when SNI is enabled.
    if libressl:
        # SNI not supported with LibreSSL.
        return

    node.append_conf("ssl_passphrase_command = 'echo FooBaR1'\nssl_sni = on\n")
    node.append_conf(
        f'example.org "{ddir}/server.crt" "{ddir}/server.key" "" '
        '"echo FooBaR1" on\n'
        f'example.com "{ddir}/server.crt" "{ddir}/server.key" "" '
        '"echo FooBaR1" on\n',
        filename="pg_hosts.conf",
    )

    # If the server starts and runs, the bad ssl_passphrase.passphrase was
    # correctly ignored.
    node.start()
    assert os.path.exists(node.pidfile), "postgres started after SNI"

    node.stop("fast")
    log_contents = node.log_content()
    assert re.search(
        r"WARNING.*SNI is enabled; installed TLS init hook will be ignored",
        log_contents,
    ), "server warns that init hook and SNI are incompatible"
    # Ensure that the warning was printed once and not once per host line
    count = len(re.findall(r"installed TLS init hook will be ignored", log_contents))
    assert count == 1, "Only one WARNING"
