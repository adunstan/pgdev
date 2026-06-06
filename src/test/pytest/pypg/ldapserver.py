# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""An OpenLDAP (slapd) server for testing pg_hba.conf ldap authentication.

Module import probes for a usable slapd binary and the OpenLDAP schema
directory; :data:`AVAILABLE` says whether a server can be set up and
:data:`SETUP_ERROR` explains why not.
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time

from .util import TIMEOUT_DEFAULT


def _detect_slapd():
    """Return (slapd_path, schema_dir) or (None, None) if unavailable."""
    if sys.platform == "darwin":
        candidates = [
            (
                "/opt/homebrew/opt/openldap/libexec/slapd",
                "/opt/homebrew/etc/openldap/schema",
            ),
            ("/usr/local/opt/openldap/libexec/slapd", "/usr/local/etc/openldap/schema"),
            ("/opt/local/libexec/slapd", "/opt/local/etc/openldap/schema"),
        ]
        for slapd, schema in candidates:
            if os.path.isdir(os.path.dirname(schema)) and os.path.isdir(schema):
                return slapd, schema
        return None, None
    if sys.platform.startswith("linux"):
        for schema in ("/etc/ldap/schema", "/etc/openldap/schema"):
            if os.path.isdir(schema):
                return "/usr/sbin/slapd", schema
        return None, None
    if sys.platform.startswith("freebsd"):
        schema = "/usr/local/etc/openldap/schema"
        if os.path.isdir(schema):
            return "/usr/local/libexec/slapd", schema
        return None, None
    return None, None


SLAPD, SCHEMA_DIR = _detect_slapd()
AVAILABLE = bool(SLAPD) and os.path.exists(SLAPD or "")
SETUP_ERROR = (
    None
    if AVAILABLE
    else (
        "ldap tests not supported on this platform"
        if sys.platform not in ("darwin",)
        and not sys.platform.startswith(("linux", "freebsd"))
        else "OpenLDAP server installation not found"
    )
)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LdapServer:
    """A running slapd instance for a single test.

    *basedir* is a writable scratch directory (e.g. tmp_path); *certdir* is the
    directory holding the test SSL certs (src/test/ssl/ssl).
    """

    def __init__(self, basedir, rootpw, authtype, certdir):
        if not AVAILABLE:
            raise RuntimeError(SETUP_ERROR or "no suitable LDAP binaries found")

        basedir = str(basedir)
        ldap_datadir = os.path.join(basedir, "openldap-data")
        slapd_certs = os.path.join(basedir, "slapd-certs")
        self.pidfile = os.path.join(basedir, "slapd.pid")
        slapd_conf = os.path.join(basedir, "slapd.conf")
        slapd_logfile = os.path.join(basedir, "slapd.log")

        self.server = "localhost"
        self.port = _free_port()
        self.s_port = _free_port()
        self.url = f"ldap://{self.server}:{self.port}"
        self.s_url = f"ldaps://{self.server}:{self.s_port}"
        self.basedn = "dc=example,dc=net"
        self.rootdn = "cn=Manager,dc=example,dc=net"
        self.pwfile = os.path.join(basedir, "ldappassword")

        conf = f"""\
include {SCHEMA_DIR}/core.schema
include {SCHEMA_DIR}/cosine.schema
include {SCHEMA_DIR}/nis.schema
include {SCHEMA_DIR}/inetorgperson.schema

pidfile {self.pidfile}
logfile {slapd_logfile}

access to *
        by * read
        by {authtype} auth

database ldif
directory {ldap_datadir}

TLSCACertificateFile {slapd_certs}/ca.crt
TLSCertificateFile {slapd_certs}/server.crt
TLSCertificateKeyFile {slapd_certs}/server.key

suffix "dc=example,dc=net"
rootdn "{self.rootdn}"
rootpw "{rootpw}"
"""
        with open(slapd_conf, "w", encoding="utf-8") as fh:
            fh.write(conf)

        os.mkdir(ldap_datadir)
        os.mkdir(slapd_certs)
        shutil.copy(
            os.path.join(certdir, "server_ca.crt"), os.path.join(slapd_certs, "ca.crt")
        )
        shutil.copy(
            os.path.join(certdir, "server-cn-only.crt"),
            os.path.join(slapd_certs, "server.crt"),
        )
        shutil.copy(
            os.path.join(certdir, "server-cn-only.key"),
            os.path.join(slapd_certs, "server.key"),
        )

        with open(self.pwfile, "w", encoding="utf-8") as fh:
            fh.write(rootpw)
        os.chmod(self.pwfile, 0o600)

        # -s0 prevents log messages ending up in syslog.
        subprocess.run(
            [SLAPD, "-f", slapd_conf, "-s0", "-h", f"{self.url} {self.s_url}"],
            check=True,
        )

        # Wait until slapd accepts requests.
        deadline = time.monotonic() + TIMEOUT_DEFAULT
        while True:
            rc = subprocess.run(
                [
                    "ldapsearch",
                    "-sbase",
                    "-H",
                    self.url,
                    "-b",
                    self.basedn,
                    "-D",
                    self.rootdn,
                    "-y",
                    self.pwfile,
                    "-n",
                    "objectclass=*",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            if rc == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("cannot connect to slapd")
            time.sleep(0.5)

    def _env(self):
        env = dict(os.environ)
        env["LDAPURI"] = self.url
        env["LDAPBINDDN"] = self.rootdn
        return env

    def ldapadd_file(self, path):
        """Add the LDIF data in *path* to the server."""
        subprocess.run(
            ["ldapadd", "-x", "-y", self.pwfile, "-f", str(path)],
            env=self._env(),
            check=True,
        )

    def ldapsetpw(self, user, password):
        """Set *user*'s password on the server."""
        subprocess.run(
            ["ldappasswd", "-x", "-y", self.pwfile, "-s", password, user],
            env=self._env(),
            check=True,
        )

    def prop(self, *names):
        """Return the requested properties (url, port, basedn, ...)."""
        return [getattr(self, n) for n in names]

    def stop(self):
        """Terminate the slapd instance."""
        try:
            with open(self.pidfile, encoding="utf-8") as fh:
                pid = int(fh.readline().strip())
        except (FileNotFoundError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
