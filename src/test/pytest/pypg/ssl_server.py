# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Configure a PostgreSQL test cluster for SSL, for the ssl regression tests.

Only the OpenSSL backend is supported.

Certificates and keys live in src/test/ssl/ssl (built by the source tree).
The helper installs the server certs/keys into the cluster's data directory,
copies the client keys to a private-perms temp dir (so libpq accepts them),
sets up the trustdb/certdb/... databases and test roles, and rewrites
pg_hba.conf / pg_ident.conf for hostssl + certificate auth.
"""

import glob
import os
import shutil
import subprocess

# Server cert/key/CA/CRL files copied into the data directory by init().
_SERVER_GLOBS = ["server-*.crt", "server-*.key"]
_SERVER_FILES = [
    "root+client_ca.crt", "root+server_ca.crt", "root_ca.crt",
    "root+client.crl",
]
# Client keys copied to a private temp dir with 0600 perms (0644 for the
# deliberately-wrong-perms copy).
_CLIENT_KEYS = [
    "client.key", "client-revoked.key", "client-der.key",
    "client-encrypted-pem.key", "client-encrypted-der.key", "client-dn.key",
    "client_ext.key", "client-long.key", "client-revoked-utf8.key",
]


class SSLServer:
    """OpenSSL-backed SSL configuration for a test cluster.

    *ssl_dir* is the path to src/test/ssl/ssl; *keydir* is a writable temp dir
    for the permission-adjusted client keys; *bindir* locates pg_config.
    """

    def __init__(self, ssl_dir, keydir, bindir):
        self._library = "OpenSSL"
        self.ssl_dir = str(ssl_dir)
        self.keydir = str(keydir)
        self.bindir = str(bindir)
        self.key = {}

    # -- backend (OpenSSL) ---------------------------------------------------

    def _copy_glob(self, pattern, dest):
        for src in glob.glob(os.path.join(self.ssl_dir, pattern)):
            shutil.copy(src, os.path.join(dest, os.path.basename(src)))

    def _init_backend(self, pgdata):
        for pattern in _SERVER_GLOBS:
            self._copy_glob(pattern, pgdata)
        for key in glob.glob(os.path.join(pgdata, "server-*.key")):
            os.chmod(key, 0o600)
        for name in _SERVER_FILES:
            shutil.copy(os.path.join(self.ssl_dir, name),
                        os.path.join(pgdata, name))
        crldir = os.path.join(pgdata, "root+client-crldir")
        os.mkdir(crldir)
        self._copy_glob("root+client-crldir/*", crldir)

        # The client private keys must not be world-readable, so work from
        # copies under keydir with adjusted permissions.
        # The stored paths go into connection strings; use forward slashes so
        # libpq does not treat Windows backslashes as escape characters.
        for keyfile in _CLIENT_KEYS:
            dst = os.path.join(self.keydir, keyfile)
            shutil.copy(os.path.join(self.ssl_dir, keyfile), dst)
            os.chmod(dst, 0o600)
            self.key[keyfile] = dst.replace("\\", "/")
        # A deliberately world-readable copy, to test wrong permissions.
        wrong = os.path.join(self.keydir, "client_wrongperms.key")
        shutil.copy(os.path.join(self.ssl_dir, "client.key"), wrong)
        os.chmod(wrong, 0o644)
        self.key["client_wrongperms.key"] = wrong.replace("\\", "/")

    def sslkey(self, keyfile):
        """Return an ' sslkey=<path>' connection-string fragment."""
        return f" sslkey={self.key[keyfile]}"

    def ssl_library(self):
        return self._library

    def is_libressl(self):
        # HAVE_SSL_CTX_SET_CERT_CB is undefined for LibreSSL.
        return not self._check_pg_config("#define HAVE_SSL_CTX_SET_CERT_CB 1")

    def _check_pg_config(self, needle):
        out = subprocess.run(
            [os.path.join(self.bindir, "pg_config"), "--includedir-server"],
            stdout=subprocess.PIPE, text=True, check=True,
        ).stdout.strip()
        try:
            with open(os.path.join(out, "pg_config.h")) as fh:
                return any(needle in line for line in fh)
        except FileNotFoundError:
            return False

    # -- server configuration -----------------------------------------------

    def configure_test_server_for_ssl(self, node, serverhost, servercidr,
                                      authmethod, *, password=None,
                                      password_enc=None, extensions=None):
        """Set up databases/roles, enable SSL listening, and write pg_hba."""
        pgdata = node.data_dir
        databases = ["trustdb", "certdb", "certdb_dn", "certdb_dn_re",
                     "certdb_cn", "verifydb"]

        for role in ("ssltestuser", "md5testuser", "anotheruser",
                     "yetanotheruser"):
            node.safe_sql(f"CREATE USER {role}")
        for db in databases:
            node.safe_sql(f"CREATE DATABASE {db}")

        if password is not None:
            assert password_enc is not None, \
                "password_enc required when password is set"
            node.safe_sql(
                f"SET password_encryption='{password_enc}'; "
                f"ALTER USER ssltestuser PASSWORD '{password}';")
            # md5testuser always has an md5-encrypted password.
            node.safe_sql(
                f"SET password_encryption='md5'; "
                f"ALTER USER md5testuser PASSWORD '{password}';")
            node.safe_sql(
                f"SET password_encryption='{password_enc}'; "
                f"ALTER USER anotheruser PASSWORD '{password}';")

        for extension in (extensions or []):
            for db in databases:
                node.safe_sql(f"CREATE EXTENSION {extension} CASCADE;", dbname=db)

        node.append_conf(
            "fsync=off\n"
            "log_connections=all\n"
            "log_hostname=on\n"
            f"listen_addresses='{serverhost}'\n"
            "log_statement=all\n"
        )
        node.append_conf("include 'sslconfig.conf'")
        # The SSL configuration is written into sslconfig.conf later.
        open(os.path.join(pgdata, "sslconfig.conf"), "w").close()

        self._init_backend(pgdata)

        # Restart to load listen_addresses.
        node.restart()

        # pg_hba must be changed after restart because hostssl requires ssl=on.
        self._configure_hba_for_ssl(node, servercidr, authmethod)

    def switch_server_cert(self, node, *, certfile, keyfile=None, cafile=None,
                           crlfile=None, crldir=None, passphrase_cmd=None,
                           passphrase_cmd_reload=None, restart=True):
        """Point the server at a different cert/key/ca/crl and (re)load."""
        pgdata = node.data_dir
        if cafile is None:
            cafile = "root+client_ca"
        if crlfile is None:
            crlfile = "root+client.crl"
        if keyfile is None:
            keyfile = certfile

        os.unlink(os.path.join(pgdata, "sslconfig.conf"))
        lines = ["ssl=on"]
        lines.append(f"ssl_cert_file='{certfile}.crt'")
        lines.append(f"ssl_key_file='{keyfile}.key'")
        lines.append(f"ssl_crl_file='{crlfile}'")
        if cafile != "":
            lines.append(f"ssl_ca_file='{cafile}.crt'")
        else:
            lines.append("ssl_ca_file=''")
        if crldir is not None:
            lines.append(f"ssl_crl_dir='{crldir}'")
        # Lists of ECDH curves and cipher suites for syntax testing.
        lines.append("ssl_groups=prime256v1:secp521r1")
        lines.append(
            "ssl_tls13_ciphers=TLS_AES_256_GCM_SHA384:TLS_AES_128_GCM_SHA256")
        if passphrase_cmd is not None:
            lines.append(f"ssl_passphrase_command='{passphrase_cmd}'")
        if passphrase_cmd_reload is not None:
            lines.append(
                f"ssl_passphrase_command_supports_reload='{passphrase_cmd_reload}'")
        node.append_conf("\n".join(lines), filename="sslconfig.conf")

        if not restart:
            return
        node.restart()

    def _configure_hba_for_ssl(self, node, servercidr, authmethod):
        pgdata = node.data_dir
        os.unlink(os.path.join(pgdata, "pg_hba.conf"))
        node.append_conf(
            "# TYPE  DATABASE      USER            ADDRESS       METHOD        OPTIONS\n"
            f"hostssl trustdb       md5testuser     {servercidr}   md5\n"
            f"hostssl trustdb       all             {servercidr}   {authmethod}\n"
            f"hostssl verifydb      ssltestuser     {servercidr}   {authmethod}    clientcert=verify-full\n"
            f"hostssl verifydb      anotheruser     {servercidr}   {authmethod}    clientcert=verify-full\n"
            f"hostssl verifydb      yetanotheruser  {servercidr}   {authmethod}    clientcert=verify-ca\n"
            f"hostssl certdb        all             {servercidr}   cert\n"
            f"hostssl certdb_dn     all             {servercidr}   cert clientname=DN map=dn\n"
            f"hostssl certdb_dn_re  all             {servercidr}   cert clientname=DN map=dnre\n"
            f"hostssl certdb_cn     all             {servercidr}   cert clientname=CN map=cn\n",
            filename="pg_hba.conf",
        )
        os.unlink(os.path.join(pgdata, "pg_ident.conf"))
        node.append_conf(
            "# MAPNAME SYSTEM-USERNAME                                         PG-USERNAME\n"
            'dn        "CN=ssltestuser-dn,OU=Testing,OU=Engineering,O=PGDG"    ssltestuser\n'
            'dnre      "/^.*OU=Testing,.*$"                                    ssltestuser\n'
            "cn        ssltestuser-dn                                          ssltestuser\n",
            filename="pg_ident.conf",
        )
