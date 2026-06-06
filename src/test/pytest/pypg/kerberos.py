# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""A stand-alone KDC for testing PostgreSQL GSSAPI / Kerberos support.

Import probes for MIT krb5 binaries; :data:`AVAILABLE` / :data:`SETUP_ERROR`
report usability.

The constructor writes krb5.conf / kdc.conf, sets the KRB5_CONFIG /
KRB5_KDC_PROFILE / KRB5CCNAME environment variables (so the postmaster and
client tools started afterward inherit them), creates the realm and service
principal, and starts krb5kdc.  Call :meth:`stop` (or use the ``kerberos``
fixture) to shut the KDC down.
"""

import os
import shutil
import signal
import socket
import subprocess
import sys


def _detect():
    """Locate the krb5 binaries; return a dict or None if unavailable."""
    bin_dir = sbin_dir = None
    if sys.platform == "darwin":
        base = (
            "/opt/homebrew/opt/krb5"
            if os.path.isdir("/opt/homebrew")
            else "/usr/local/opt/krb5"
        )
        bin_dir, sbin_dir = base + "/bin", base + "/sbin"
    elif sys.platform.startswith("freebsd"):
        bin_dir, sbin_dir = "/usr/local/bin", "/usr/local/sbin"
    elif sys.platform.startswith("linux"):
        sbin_dir = "/usr/sbin"

    def _bin(name, d):
        if d and os.path.isdir(d) and os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
        return shutil.which(name)

    tools = {
        "krb5_config": _bin("krb5-config", bin_dir),
        "kinit": _bin("kinit", bin_dir),
        "klist": _bin("klist", bin_dir),
        "kdb5_util": _bin("kdb5_util", sbin_dir),
        "kadmin_local": _bin("kadmin.local", sbin_dir),
        "krb5kdc": _bin("krb5kdc", sbin_dir),
    }
    if not all(tools.values()):
        return None
    return tools


_TOOLS = _detect()
AVAILABLE = _TOOLS is not None
SETUP_ERROR = None if AVAILABLE else "MIT Kerberos 5 installation not found"


class Kerberos:
    """A running krb5kdc with one realm and a PostgreSQL service principal.

    *basedir* is a writable scratch dir (tmp_path).  *srvnam* is the Kerberos
    service name (postgres), i.e. the with_krb_srvnam build value.
    """

    def __init__(self, basedir, host, hostaddr, realm, srvnam="postgres"):
        if not AVAILABLE:
            raise RuntimeError(SETUP_ERROR)
        t = _TOOLS

        basedir = str(basedir)
        self.krb5_conf = os.path.join(basedir, "krb5.conf")
        self.kdc_conf = os.path.join(basedir, "kdc.conf")
        self.krb5_cache = os.path.join(basedir, "krb5cc")
        self.krb5_log = os.path.join(basedir, "krb5libs.log")
        self.kdc_log = os.path.join(basedir, "krb5kdc.log")
        self.kdc_datadir = os.path.join(basedir, "krb5kdc")
        self.kdc_pidfile = os.path.join(basedir, "krb5kdc.pid")
        self.keytab = os.path.join(basedir, "krb5.keytab")
        self.kdc_port = _free_port()
        self.realm = realm

        ver = subprocess.run(
            [t["krb5_config"], "--version"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout
        if "heimdal" in ver.lower():
            raise RuntimeError("Heimdal is not supported")
        m = ver and __import__("re").search(r"Kerberos 5 release (\d+\.\d+)", ver)
        krb5_version = float(m.group(1)) if m else 0.0

        with open(self.krb5_conf, "w", encoding="utf-8") as fh:
            fh.write(
                f"""[logging]
default = FILE:{self.krb5_log}
kdc = FILE:{self.kdc_log}

[libdefaults]
dns_lookup_realm = false
dns_lookup_kdc = false
default_realm = {realm}
forwardable = false
rdns = false

[realms]
{realm} = {{
    kdc = {hostaddr}:{self.kdc_port}
}}
"""
            )

        with open(self.kdc_conf, "w", encoding="utf-8") as fh:
            fh.write("[kdcdefaults]\n")
            if krb5_version >= 1.15:
                fh.write(f"kdc_listen = {hostaddr}:{self.kdc_port}\n")
                fh.write(f"kdc_tcp_listen = {hostaddr}:{self.kdc_port}\n")
            else:
                fh.write(f"kdc_ports = {self.kdc_port}\n")
                fh.write(f"kdc_tcp_ports = {self.kdc_port}\n")
            fh.write(
                f"""
[realms]
{realm} = {{
    database_name = {self.kdc_datadir}/principal
    admin_keytab = FILE:{self.kdc_datadir}/kadm5.keytab
    acl_file = {self.kdc_datadir}/kadm5.acl
    key_stash_file = {self.kdc_datadir}/_k5.{realm}
}}"""
            )

        os.mkdir(self.kdc_datadir)

        # Make the test's config and cache files, not global ones, take effect.
        # The postmaster and client tools started later inherit these.
        os.environ["KRB5_CONFIG"] = self.krb5_conf
        os.environ["KRB5_KDC_PROFILE"] = self.kdc_conf
        os.environ["KRB5CCNAME"] = self.krb5_cache

        self._kdb5_util = t["kdb5_util"]
        self._kadmin_local = t["kadmin_local"]
        self._krb5kdc = t["krb5kdc"]
        self._kinit = t["kinit"]
        self._klist = t["klist"]

        service_principal = f"{srvnam}/{host}"
        self._bail(self._kdb5_util, "create", "-s", "-P", "secret0")
        self._bail(self._kadmin_local, "-q", f"addprinc -randkey {service_principal}")
        self._bail(
            self._kadmin_local, "-q", f"ktadd -k {self.keytab} {service_principal}"
        )
        self._bail(self._krb5kdc, "-P", self.kdc_pidfile)

    @staticmethod
    def _bail(*argv):
        subprocess.run(list(argv), check=True)

    def create_principal(self, principal, password):
        self._bail(self._kadmin_local, "-q", f"addprinc -pw {password} {principal}")

    def create_ticket(self, principal, password, forwardable=False):
        cmd = [self._kinit, principal]
        if forwardable:
            cmd.append("-f")
        subprocess.run(cmd, input=password + "\n", text=True, check=True)
        subprocess.run([self._klist, "-f"], check=True)

    def stop(self):
        try:
            with open(self.kdc_pidfile, encoding="utf-8") as fh:
                pid = int(fh.readline().strip())
        except (FileNotFoundError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
