# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""A managed PostgreSQL server instance for tests.

:class:`PostgresServer` manages the lifecycle (initdb, start, stop, restart,
reload), configuration, and query execution of a server instance.  Queries run
in-process through :class:`libpq.Session` (no psql subprocess); the command_*
helpers run client programs with this server's PGHOST/PGPORT.
"""

import os
import re
import shutil
import signal
import socket
import subprocess

from libpq import ConnStatusType, Session

from .command import CommandResult, PgBin
from .util import (
    TIMEOUT_DEFAULT,
    USE_UNIX_SOCKETS,
    WINDOWS_OS,
    poll_until,
    run_captured,
)


def _pid_alive(pid):
    """Whether process *pid* currently exists.

    On Windows ``os.kill(pid, 0)`` would terminate the process (any signal maps
    to TerminateProcess), so probe with OpenProcess/GetExitCodeProcess instead.
    """
    if WINDOWS_OS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class PostgresServer:
    """One initdb'd data directory and the server running on it."""

    def __init__(self, name, bindir, libdir, basedir, port, sockdir,
                 listen_host=None):
        self.name = name
        self._bindir = str(bindir)
        self.libdir = str(libdir)
        self.basedir = str(basedir)
        self.port = int(port)
        self._sockdir = str(sockdir)
        # The connection host.  When listen_host is given (own_host), bind that
        # loopback address over TCP; combined with an explicit shared port this
        # lets several nodes coexist, distinguished by IP.  Otherwise use the
        # socket directory with Unix-domain sockets, or 127.0.0.1 on TCP
        # (Windows).  Backslashes in a Windows socket path are converted to '/'
        # so the value is valid in postgresql.conf.
        self._own_host = listen_host is not None
        if self._own_host:
            self.host = listen_host
        elif USE_UNIX_SOCKETS:
            self.host = self._sockdir.replace("\\", "/")
        else:
            self.host = "127.0.0.1"
        self._running = False
        self._sessions = {}
        self._logfile_generation = 0
        os.makedirs(self.basedir, exist_ok=True)

    # -- paths / connection info --------------------------------------------

    @property
    def data_dir(self):
        return os.path.join(self.basedir, "pgdata")

    @property
    def logfile(self):
        # Generation 0 keeps the plain "server.log" name that other tests and
        # helpers expect; rotate_logfile() bumps the generation for tests that
        # must keep a fresh log across a restart.
        if self._logfile_generation == 0:
            return os.path.join(self.basedir, "server.log")
        return os.path.join(self.basedir, f"server_{self._logfile_generation}.log")

    def rotate_logfile(self):
        """Switch to a fresh log file for the next start.

        Needed where the old log can't be reopened for writing (e.g. on
        Windows) or a test wants to scan only the newest postmaster's output.
        """
        self._logfile_generation += 1
        return self.logfile

    @property
    def pidfile(self):
        return os.path.join(self.data_dir, "postmaster.pid")

    @property
    def backup_dir(self):
        """Output path for backups taken with :meth:`backup`."""
        return os.path.join(self.basedir, "backup")

    @property
    def archive_dir(self):
        """Directory WAL is archived into when has_archiving is enabled."""
        return os.path.join(self.basedir, "archives")

    @property
    def bindir(self):
        return self._bindir

    def connstr(self, dbname="postgres"):
        return f"host='{self.host}' port={self.port} dbname='{dbname}'"

    def _listen_conf_lines(self):
        """postgresql.conf lines selecting the connection transport.

        Unix-domain sockets in this node's private directory, or TCP on the
        loopback address (Windows, or any own_host node).
        """
        if self._own_host or not USE_UNIX_SOCKETS:
            return [
                f"listen_addresses = '{self.host}'",
                "unix_socket_directories = ''",
            ]
        return [
            "listen_addresses = ''",
            f"unix_socket_directories = '{self.host}'",
        ]

    def raw_connect(self):
        """Open and return a raw socket to the server, caller closes it.

        Connects to the Unix-domain socket (``<host>/.s.PGSQL.<port>``) or, on
        TCP, to (host, port), for tests that speak the wire protocol directly
        or just consume a connection.
        """
        if USE_UNIX_SOCKETS:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(os.path.join(self.host, f".s.PGSQL.{self.port}"))
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.host, self.port))
        return sock

    def raw_connect_works(self):
        """Whether :meth:`raw_connect` is usable on this platform.

        Always true on TCP.  With Unix-domain sockets it needs a working
        AF_UNIX implementation (absent on some Windows pythons); probe once.
        """
        if USE_UNIX_SOCKETS:
            if not hasattr(socket, "AF_UNIX"):
                return False
            try:
                self.raw_connect().close()
            except OSError:
                return False
        return True

    @property
    def pg_bin(self):
        """A PgBin whose environment targets this server.

        Sets PGHOST/PGPORT and defaults PGDATABASE to 'postgres', so client
        programs invoked without an explicit database connect to a database
        that exists.
        """
        return PgBin(
            self._bindir,
            extra_env={
                "PGHOST": self.host,
                "PGPORT": str(self.port),
                "PGDATABASE": "postgres",
            },
        )

    # -- internal command runner --------------------------------------------

    def _run(self, *argv, check=True):
        argv = [self._resolve(argv[0]), *map(str, argv[1:])]
        print("# Running: " + " ".join(argv))
        # Capture via files, not pipes: "pg_ctl start" leaves a postmaster
        # holding the pipe open on Windows, which would deadlock the read.
        returncode, stdout, _ = run_captured(argv, combine_stderr=True)
        if check and returncode != 0:
            raise RuntimeError(
                f"command failed ({returncode}): {' '.join(argv)}\n{stdout}"
            )
        return CommandResult(returncode, stdout, "")

    def _resolve(self, name):
        candidate = os.path.join(self._bindir, name)
        return candidate if os.path.exists(candidate) else name

    # -- lifecycle -----------------------------------------------------------

    def init(
        self,
        extra=None,
        *,
        allows_streaming=False,
        has_archiving=False,
        has_restoring=False,
        wal_level=None,
    ):
        """Run initdb and write a test configuration.

        Keyword params (all default off, preserving the existing minimal
        config):

        - ``allows_streaming``: set up postgresql.conf for replication.
          Pass ``"logical"`` for ``wal_level = logical``; any other truthy
          value yields ``wal_level = replica``.
        - ``has_archiving``: enable ``archive_mode`` with an archive_command
          that copies WAL into :attr:`archive_dir`.
        - ``has_restoring``: accepted but has no effect here; restoring is
          actually configured on a standby in :meth:`init_from_backup`.
        - ``wal_level``: explicit override of ``wal_level``.
        """
        os.makedirs(self.backup_dir, exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)
        # Pre-create the (empty) data directory so initdb takes its
        # "present but empty" path instead of calling pg_mkdir_p.  Python's
        # makedirs tolerates a concurrent create of a shared parent, whereas
        # initdb's pg_mkdir_p does not: under parallel test execution several
        # initdb processes race to create a common temp ancestor and all but
        # one fail with "File exists".
        os.makedirs(self.data_dir, exist_ok=True)

        argv = [
            "initdb",
            "-D", self.data_dir,
            "--no-sync",
            "--no-instructions",
            "-A", "trust",
            "--locale=C",
            "--encoding=UTF8",
        ]
        if extra:
            argv += list(extra)
        self._run(*argv)

        lines = []
        if allows_streaming:
            if wal_level is None:
                wal_level = "logical" if allows_streaming == "logical" else "replica"
            lines += [
                f"wal_level = {wal_level}",
                "max_wal_senders = 10",
                "max_replication_slots = 10",
                "wal_log_hints = on",
                "hot_standby = on",
                # conservative settings to ensure we can run multiple postmasters:
                "shared_buffers = 1MB",
                "max_connections = 10",
                # limit disk space consumption, too:
                "max_wal_size = 128MB",
            ]
        elif wal_level is not None:
            lines.append(f"wal_level = {wal_level}")

        lines.append(f"port = {self.port}")
        lines += self._listen_conf_lines()
        lines += ["fsync = off", ""]
        self.append_conf("\n".join(lines))

        if has_archiving:
            self.enable_archiving()

    @staticmethod
    def _file_copy_command(src, dst):
        """A shell command that copies file *src* to *dst*.

        *src*/*dst* may embed the archive/restore ``%p``/``%f`` placeholders.
        Elsewhere this is ``cp`` with forward-slash paths.  On Windows it is
        cmd's ``copy``; there the path's backslashes must be doubled for the
        command to work once stored in postgresql.conf and to identify the
        target file, and both paths are double-quoted to tolerate spaces.
        """
        if WINDOWS_OS:
            def winpath(p):
                return p.replace("/", "\\").replace("\\", "\\\\")
            return f'copy "{winpath(src)}" "{winpath(dst)}"'
        return f'cp "{src}" "{dst}"'

    def enable_archiving(self):
        """Enable WAL archiving into :attr:`archive_dir`.

        Internal helper.
        """
        copy_command = self._file_copy_command("%p", f"{self.archive_dir}/%f")
        self.append_conf(
            "\n".join(
                [
                    "",
                    "archive_mode = on",
                    f"archive_command = '{copy_command}'",
                    "",
                ]
            )
        )

    def append_conf(self, text, filename="postgresql.conf"):
        with open(os.path.join(self.data_dir, filename), "a", encoding="utf-8") as fh:
            if not text.endswith("\n"):
                text += "\n"
            fh.write(text)

    def start(self, fail_ok=False):
        """Start the postmaster.  Returns True on success.

        With *fail_ok* a failed start returns False instead of raising, like
        Cluster::start(fail_ok => 1).  If pg_ctl reports failure but a
        postmaster is in fact still alive (e.g. pg_ctl timed out waiting), the
        running flag is set so a later stop() cleans it up.
        """
        proc = self._run(
            "pg_ctl", "-D", self.data_dir, "-l", self.logfile, "-w", "start",
            check=False,
        )
        if proc.returncode == 0:
            self._running = True
            return True
        self._running = self._postmaster_alive()
        if not fail_ok:
            # pg_ctl's own output rarely says why; include the server log,
            # which holds the actual startup error.
            try:
                with open(self.logfile, encoding="utf-8", errors="replace") as fh:
                    log = fh.read()
            except OSError:
                log = "(could not read log file)"
            raise RuntimeError(
                f'pg_ctl start failed for node "{self.name}":\n{proc.stdout}\n'
                f"--- {self.logfile} ---\n{log}"
            )
        return False

    def stop(self, mode="fast", fail_ok=False):
        """Stop the postmaster.  Returns True on success (or if not running)."""
        self._close_sessions()
        if not self._running:
            return True
        proc = self._run(
            "pg_ctl", "-D", self.data_dir, "-m", mode, "-w", "stop",
            check=not fail_ok,
        )
        self._running = False
        return proc.returncode == 0

    def postmaster_pid(self):
        """Return the postmaster PID from postmaster.pid, or None."""
        try:
            with open(self.pidfile) as fh:
                return int(fh.readline().strip())
        except (FileNotFoundError, ValueError):
            return None

    def _postmaster_alive(self):
        pid = self.postmaster_pid()
        if pid is None:
            return False
        return _pid_alive(pid)

    def signal_backend(self, pid, signame):
        """Send signal *signame* (e.g. "QUIT", "KILL", "TERM") to process *pid*.

        Uses ``pg_ctl kill``, which delivers the signal through the server's own
        mechanism and so works on every platform (Windows has no Unix signals).
        """
        self._run("pg_ctl", "kill", signame, str(pid))

    def kill9(self):
        """Hard-kill the postmaster (no chance to clean up).

        Postmaster children normally exit on their own once the postmaster is
        gone; a backend stuck in a CPU-bound loop is the exception this test
        relies on.
        """
        pid = self.postmaster_pid()
        self._close_sessions()
        if pid is not None:
            print(f'### Killing node "{self.name}" using signal 9')
            if WINDOWS_OS:
                # No SIGKILL on Windows; terminate the process tree forcibly.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._running = False

    def restart(self, mode="fast"):
        self._close_sessions()
        self._run("pg_ctl", "-D", self.data_dir, "-l", self.logfile, "-m", mode, "-w", "restart")
        self._running = True

    def reload(self):
        self._run("pg_ctl", "-D", self.data_dir, "reload")

    def promote(self):
        self._run("pg_ctl", "-D", self.data_dir, "-w", "promote")

    # -- backup / streaming replication --------------------------------------

    def backup(self, backup_name, backup_options=None):
        """Take a pg_basebackup into ``backup_dir/<backup_name>``.

        WAL is fetched at the end of the backup unless overridden via
        *backup_options* (e.g.
        ``["-X", "stream"]``).  The resulting backup is usable by
        :meth:`init_from_backup`.
        """
        backup_path = os.path.join(self.backup_dir, backup_name)
        print(f'# Taking pg_basebackup {backup_name} from node "{self.name}"')
        argv = [
            "pg_basebackup",
            "--no-sync",
            "--pgdata", backup_path,
            "--host", self.host,
            "--port", str(self.port),
            "--checkpoint", "fast",
        ]
        if backup_options:
            argv += list(backup_options)
        self._run(*argv)
        print("# Backup finished")

    def backup_fs_cold(self, backup_name):
        """Take a filesystem-level cold backup (the server must be stopped).

        Copies the data directory into ``backup_dir/<backup_name>``, excluding
        the log directory and postmaster.pid, producing a tree usable by
        :meth:`init_from_backup`.
        """
        dest = os.path.join(self.backup_dir, backup_name)
        os.makedirs(self.backup_dir, exist_ok=True)
        shutil.copytree(
            self.data_dir,
            dest,
            symlinks=True,
            ignore=shutil.ignore_patterns("log", "postmaster.pid"),
        )

    def init_from_backup(
        self,
        root_node,
        backup_name,
        *,
        has_streaming=False,
        has_restoring=False,
        standby=True,
    ):
        """Initialize this node's data dir from *root_node*'s named backup.

        Plain-format backups only; tar/incremental/tablespace variants are not
        supported by this framework.  Does not start the node.

        - ``has_streaming``: configure ``primary_conninfo`` pointing at
          *root_node* and place ``standby.signal`` (streaming replication).
        - ``has_restoring``: configure a ``restore_command`` reading from
          *root_node*'s archive dir.  Standby mode is used by default; pass
          ``standby=False`` for crash-recovery (recovery.signal) mode.
        """
        backup_path = os.path.join(root_node.backup_dir, backup_name)
        print(
            f'# Initializing node "{self.name}" from backup "{backup_name}" '
            f'of node "{root_node.name}"'
        )
        if not os.path.isdir(backup_path):
            raise RuntimeError(
                f'Backup "{backup_name}" does not exist at {backup_path}'
            )

        os.makedirs(self.backup_dir, exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)

        # Copy the backup tree into this node's data directory, leaving the
        # original backup unmodified.
        data_path = self.data_dir
        if os.path.isdir(data_path):
            shutil.rmtree(data_path)
        shutil.copytree(backup_path, data_path, symlinks=True)
        os.chmod(data_path, 0o700)

        # Base configuration for this node.
        self.append_conf(
            "\n".join(
                ["", f"port = {self.port}"]
                + self._listen_conf_lines()
                + [""]
            )
        )

        if has_streaming:
            self.enable_streaming(root_node)
        if has_restoring:
            self.enable_restoring(root_node, standby)

    def enable_streaming(self, root_node):
        """Configure streaming replication from *root_node* and go standby.

        Internal helper.  The standby's application_name defaults to its node
        name so that
        :meth:`wait_for_catchup` can locate it in pg_stat_replication.
        """
        print(f'### Enabling streaming replication for node "{self.name}"')
        root_connstr = (
            f"host={root_node.host} port={root_node.port} "
            f"application_name={self.name}"
        )
        self.append_conf(f"\nprimary_conninfo='{root_connstr}'\n")
        self.set_standby_mode()

    def enable_restoring(self, root_node, standby=True):
        """Configure WAL restore from *root_node*'s archive dir.

        Internal helper.
        """
        print(f'### Enabling WAL restore for node "{self.name}"')
        copy_command = self._file_copy_command(f"{root_node.archive_dir}/%f", "%p")
        self.append_conf(f"\nrestore_command = '{copy_command}'\n")
        if standby:
            self.set_standby_mode()
        else:
            self.set_recovery_mode()

    def set_standby_mode(self):
        """Place a standby.signal file."""
        self.append_conf("", filename="standby.signal")

    def set_recovery_mode(self):
        """Place a recovery.signal file."""
        self.append_conf("", filename="recovery.signal")

    # -- LSN / replication progress ------------------------------------------

    _LSN_MODES = {
        "insert": "pg_current_wal_insert_lsn()",
        "flush": "pg_current_wal_flush_lsn()",
        "write": "pg_current_wal_lsn()",
        "receive": "pg_last_wal_receive_lsn()",
        "replay": "pg_last_wal_replay_lsn()",
    }

    def lsn(self, mode):
        """Return the current LSN for *mode*.

        Valid modes: insert, flush, write, receive, replay.  Returns ``None``
        if the underlying function returns an empty result.
        """
        if mode not in self._LSN_MODES:
            raise ValueError(
                f"unknown mode for 'lsn': {mode!r}, valid modes are "
                + ", ".join(self._LSN_MODES)
            )
        result = self.safe_sql(f"SELECT {self._LSN_MODES[mode]}").strip()
        return result if result != "" else None

    # -- WAL generation / manipulation ---------------------------------------

    def _insert_lsn_bytes(self):
        """Current insert LSN as an integer byte offset (LSN - '0/0')."""
        return int(self.safe_sql("SELECT pg_current_wal_insert_lsn() - '0/0'"))

    def emit_wal(self, size):
        """Emit *size* bytes of WAL via pg_logical_emit_message; return end LSN.

        Returns the numeric LSN.
        """
        return int(
            self.safe_sql(
                f"SELECT pg_logical_emit_message(true, '', repeat('a', {size})) - '0/0'"
            )
        )

    def advance_wal(self, num):
        """Advance WAL by *num* segments."""
        for _ in range(num):
            self.safe_sql("SELECT pg_logical_emit_message(false, '', 'foo')")
            self.safe_sql("SELECT pg_switch_wal()")

    def advance_wal_out_of_record_splitting_zone(self, wal_block_size):
        """Advance WAL away from a page boundary."""
        page_threshold = wal_block_size // 4
        end_lsn = self._insert_lsn_bytes()
        page_offset = end_lsn % wal_block_size
        while page_offset >= wal_block_size - page_threshold:
            self.emit_wal(page_threshold)
            end_lsn = self._insert_lsn_bytes()
            page_offset = end_lsn % wal_block_size
        return end_lsn

    def advance_wal_to_record_splitting_zone(self, wal_block_size):
        """Advance WAL close to a page boundary."""
        record_header_size = 24
        end_lsn = self._insert_lsn_bytes()
        page_offset = end_lsn % wal_block_size
        # Get fairly close to the end of a page in big steps.
        while page_offset <= wal_block_size - 512:
            self.emit_wal(wal_block_size - page_offset - 256)
            end_lsn = self._insert_lsn_bytes()
            page_offset = end_lsn % wal_block_size
        # Calibrate the message size to approach 8 bytes at a time.
        message_size = wal_block_size - 80
        while page_offset <= wal_block_size - record_header_size:
            self.emit_wal(message_size)
            end_lsn = self._insert_lsn_bytes()
            old_offset = page_offset
            page_offset = end_lsn % wal_block_size
            delta = page_offset - old_offset
            if delta > 8:
                message_size -= 8
            elif delta <= 0:
                message_size += 8
        return end_lsn

    def write_wal(self, tli, lsn, segment_size, data):
        """Write raw *data* bytes at *lsn* in the WAL."""
        segment = lsn // segment_size
        offset = lsn % segment_size
        path = os.path.join(
            self.data_dir, "pg_wal", "%08X%08X%08X" % (tli, 0, segment)
        )
        with open(path, "r+b") as fh:
            fh.seek(offset)
            fh.write(data)
        return path

    def wait_for_catchup(self, standby_name, mode="replay", target_lsn=None):
        """Wait until *standby_name* has caught up on this (primary) node.

        Polls pg_stat_replication on self until the standby's ``<mode>_lsn``
        reaches *target_lsn* (default: this node's current write LSN, or its
        replay LSN when self is itself in recovery).

        *standby_name* may be a :class:`PostgresServer` (its name is used as
        the application_name) or an application_name string.  Valid modes:
        sent, write, flush, replay.
        """
        valid_modes = ("sent", "write", "flush", "replay")
        if mode not in valid_modes:
            raise ValueError(
                f"unknown mode {mode} for 'wait_for_catchup', valid modes are "
                + ", ".join(valid_modes)
            )

        if isinstance(standby_name, PostgresServer):
            standby_name = standby_name.name

        if target_lsn is None:
            isrecovery = self.safe_sql("SELECT pg_is_in_recovery()").strip()
            target_lsn = self.lsn("replay" if isrecovery == "t" else "write")

        print(
            f"Waiting for replication conn {standby_name}'s {mode}_lsn to pass "
            f"{target_lsn} on {self.name}"
        )
        # Match the connection whose application_name is *standby_name*.
        # Standbys with a tool-generated primary_conninfo (pg_rewind /
        # pg_basebackup --write-recovery-conf) connect without setting
        # application_name and so report 'walreceiver'; fall back to that, but
        # only when no connection with the requested name exists.  Otherwise an
        # unrelated 'walreceiver' connection (e.g. a physical standby running
        # alongside a named logical subscriber) would also match, and the
        # per-row query would return more than one row, which
        # poll_query_until's single-"t" comparison never satisfies.
        query = (
            f"SELECT '{target_lsn}' <= {mode}_lsn AND state = 'streaming' "
            "FROM pg_catalog.pg_stat_replication "
            f"WHERE application_name = '{standby_name}' "
            "   OR (application_name = 'walreceiver' "
            "       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_replication "
            f"                      WHERE application_name = '{standby_name}'))"
        )
        if not self.poll_query_until(query):
            details = self.safe_sql(
                "SELECT * FROM pg_catalog.pg_stat_replication"
            )
            raise TimeoutError(
                "timed out waiting for catchup\n"
                f"Last pg_stat_replication contents:\n{details}"
            )
        print("done")

    def wait_for_event(self, backend_type, wait_event_name):
        """Wait until a *backend_type* backend reaches *wait_event_name*.

        Polls pg_stat_activity; used with injection points and other tests
        that synchronize on a backend reaching a specific wait event.
        """
        if not self.poll_query_until(
            "SELECT count(*) > 0 FROM pg_stat_activity "
            f"WHERE backend_type = '{backend_type}' "
            f"AND wait_event = '{wait_event_name}'"
        ):
            raise TimeoutError(
                f"timed out waiting for {backend_type} to reach wait event "
                f"'{wait_event_name}'"
            )

    def wait_for_replay_catchup(self, standby_name, base_node=None):
        """Wait for *standby_name*'s replay_lsn to reach *base_node*'s flush LSN.

        *base_node* defaults to self.
        """
        if base_node is None:
            base_node = self
        self.wait_for_catchup(standby_name, "replay", base_node.lsn("flush"))

    def wait_for_subscription_sync(self, publisher=None, subname=None, dbname="postgres"):
        """Wait for logical replication initial sync to complete.

        Polls pg_subscription_rel until all tables are in 'r'/'s' state; if
        *publisher* is given, additionally waits for the publisher to catch up
        to *subname*.
        """
        print(f'Waiting for all subscriptions in "{self.name}" to synchronize data')
        query = (
            "SELECT count(1) = 0 FROM pg_subscription_rel "
            "WHERE srsubstate NOT IN ('r', 's')"
        )
        if not self.poll_query_until(query, dbname=dbname):
            details = self.safe_sql("SELECT * FROM pg_subscription_rel", dbname=dbname)
            raise TimeoutError(
                "timed out waiting for subscriber to synchronize data\n"
                f"Last pg_subscription_rel contents:\n{details}"
            )
        if publisher is not None:
            if subname is None:
                raise ValueError("subscription name must be specified")
            publisher.wait_for_catchup(subname)
        print("done")

    # -- query execution (in-process via libpq) -----------------------------

    def session(self, dbname="postgres"):
        """Return a cached libpq Session for *dbname*, reconnecting if needed."""
        sess = self._sessions.get(dbname)
        if sess is None:
            sess = Session(connstr=self.connstr(dbname), libdir=self.libdir)
            self._sessions[dbname] = sess
        elif sess.conn_status() != ConnStatusType.CONNECTION_OK:
            sess.reconnect()
        return sess

    def connect(self, dbname="postgres", user=None, password=None, options=None):
        """Open a fresh (uncached) libpq Session with extra connection params.

        Use this when a test needs to connect as a specific role, with a
        password, or with per-connection GUCs (the libpq "options" keyword,
        equivalent to PGOPTIONS).  The caller owns the returned Session and
        should close() it.
        """
        connstr = self.connstr(dbname)
        if user is not None:
            connstr += f" user='{user}'"
        if password is not None:
            connstr += f" password='{password}'"
        if options is not None:
            connstr += f" options='{options}'"
        return Session(connstr=connstr, libdir=self.libdir)

    # -- connection-attempt assertions (auth tests) -------------------------

    def _full_connstr(self, connstr):
        """Combine this node's host/port/dbname with a test *connstr*.

        Callers of :meth:`connect_ok` / :meth:`connect_fails` pass a partial
        conninfo (e.g. "user=test1 require_auth=password") carrying just the
        auth bits.  Here we prepend the node's host/port/dbname; later keywords
        win in libpq, so anything the caller specifies (dbname, user, sslmode,
        ...) overrides the defaults.
        """
        return f"{self.connstr('postgres')} {connstr}"

    def log_check(self, test_name, offset, *, log_like=None, log_unlike=None):
        """Check the log written since *offset* against regex lists.

        Every pattern in *log_like* must match; none in *log_unlike* may.
        """
        if not log_like and not log_unlike:
            return
        contents = self.log_content()[offset:]
        for regex in (log_like or []):
            assert re.search(regex, contents), (
                f"{test_name}: log matches {regex!r}"
            )
        for regex in (log_unlike or []):
            assert not re.search(regex, contents), (
                f"{test_name}: log does not match {regex!r}"
            )

    def _attempt_connection(self, connstr, sql):
        """Connect with *connstr* via a psql subprocess and run *sql*.

        Returns ``(ok, stdout, stderr)`` (psql's exit status, stdout and
        stderr).  A subprocess -- not the in-process Session -- is used so the
        child inherits the environment (PGPASSWORD and the like) and performs a
        real connection handshake: that connection-time behavior is exactly what
        the auth/SSL tests exercise, and relying on the in-process library to
        read the environment is not portable.  ``-w`` keeps psql from blocking
        on a password prompt; ``-XAt`` gives unaligned, tuples-only output.
        """
        argv = ["psql", "-w", "-X", "-A", "-t",
                "-d", self._full_connstr(connstr),
                "-c", sql if sql is not None else "SELECT 1"]
        res = self.pg_bin.result(argv)
        return res.returncode == 0, res.stdout, res.stderr

    def connect_ok(self, connstr, test_name, *, sql=None, expected_stdout=None,
                   expected_stderr=None, log_like=None, log_unlike=None):
        """Assert a connection with *connstr* succeeds.

        Connects with a psql subprocess, runs *sql* (default a trivial SELECT),
        and checks stdout/stderr and the server log.
        """
        if sql is None:
            sql = f"SELECT $$connected with {connstr}$$"
        log_location = self.log_position()

        ok, stdout, stderr = self._attempt_connection(connstr, sql)

        assert ok, f"{test_name}: connection should succeed\n{stderr}"
        if expected_stdout is not None:
            assert re.search(expected_stdout, stdout), (
                f"{test_name}: stdout matches {expected_stdout!r}, got {stdout!r}"
            )
        if expected_stderr is not None:
            assert re.search(expected_stderr, stderr), (
                f"{test_name}: stderr matches {expected_stderr!r}, got {stderr!r}"
            )
        else:
            assert stderr == "", f"{test_name}: no stderr, got {stderr!r}"
        self.log_check(test_name, log_location, log_like=log_like, log_unlike=log_unlike)

    def connect_fails(self, connstr, test_name, *, expected_stderr=None,
                      log_like=None, log_unlike=None):
        """Assert a connection with *connstr* fails.

        When log_like/log_unlike are given, first wait for the backend
        fork/exit log records so the relevant lines are present before checking.
        """
        log_location = self.log_position()

        # If the connection unexpectedly succeeds, the trivial query surfaces
        # any error; "failed" is then false and the assertion below fires.
        ok, _stdout, stderr = self._attempt_connection(connstr, "SELECT 1")
        failed = not ok

        assert failed, f"{test_name}: connection should fail"
        if expected_stderr is not None:
            assert re.search(expected_stderr, stderr), (
                f"{test_name}: stderr matches {expected_stderr!r}, got {stderr!r}"
            )
        if log_like or log_unlike:
            self.wait_for_log(
                r"(?s)DEBUG:  (?:00000: )?forked new client backend, pid=(\d+) "
                r"socket.*DEBUG:  (?:00000: )?client backend \(PID \1\) exited "
                r"with exit code \d",
                log_location,
            )
            self.log_check(test_name, log_location, log_like=log_like, log_unlike=log_unlike)

    def sql(self, query, dbname="postgres"):
        """Run *query* in-process and return its ResultData (does not raise).

        See :meth:`safe_sql` for the important caveat about how a multi-statement
        *query* is executed as a single implicit transaction.
        """
        return self.session(dbname).query(query)

    def safe_sql(self, query, dbname="postgres"):
        """Run *query* in-process; return its trimmed text output, raising on error.

        Output formatting matches ``psql -A -t`` (rows joined by newlines,
        columns by ``|``).  The query runs through the in-process libpq
        :class:`~libpq.session.Session`, not by spawning psql.

        IMPORTANT -- multiple statements run in ONE implicit transaction.
        A *query* with several semicolon-separated statements is sent as a single
        libpq command, which wraps them in one implicit transaction.  That
        differs from psql, which sends each statement separately (one autocommit
        transaction each).  So a statement that cannot run inside a transaction
        block -- CREATE/DROP DATABASE, CREATE/DROP/ALTER SUBSCRIPTION, CREATE
        TABLESPACE, VACUUM, CHECKPOINT, REINDEX CONCURRENTLY,
        PREPARE/COMMIT/ROLLBACK PREPARED, etc. -- must be issued in its own
        ``safe_sql`` call rather than combined with others.  Beware too that
        grouping unrelated statements gives them shared-transaction semantics
        they would not have under psql (e.g. statements meant to run as separate
        transactions will instead see each other's uncommitted effects).
        """
        return self.session(dbname).query_safe(query)

    def poll_query_until(self, query, expected="t", dbname="postgres", timeout=TIMEOUT_DEFAULT):
        """Run *query* repeatedly until its output equals *expected*."""

        def _ready():
            try:
                return self.safe_sql(query, dbname) == expected
            except Exception:
                return False

        return poll_until(_ready, timeout=timeout)

    # -- logical replication slots ------------------------------------------

    def slot(self, slot_name):
        """Return pg_replication_slots columns for *slot_name* as a dict.

        Missing values -- including the case of a nonexistent slot -- come back
        as empty strings.
        """
        columns = [
            "plugin", "slot_type", "datoid", "database", "active",
            "active_pid", "xmin", "catalog_xmin", "restart_lsn",
        ]
        res = self.sql(
            "SELECT " + ", ".join(columns)
            + " FROM pg_catalog.pg_replication_slots"
            f" WHERE slot_name = '{slot_name}'"
        )
        row = res.rows[0] if res.rows else [None] * len(columns)
        return {c: ("" if v is None else str(v)) for c, v in zip(columns, row)}

    def log_standby_snapshot(self, standby, slot_name):
        """Trigger the xl_running_xacts record a standby logical slot waits for.

        *self* is the primary; *standby* holds the logical *slot_name*.  Waits
        until the slot's restart_lsn is determined, then logs a standby
        snapshot.
        """
        assert standby.poll_query_until(
            "SELECT restart_lsn IS NOT NULL"
            " FROM pg_catalog.pg_replication_slots"
            f" WHERE slot_name = '{slot_name}'"
        ), "timed out waiting for logical slot to calculate its restart_lsn"
        self.safe_sql("SELECT pg_log_standby_snapshot()")

    def create_logical_slot_on_standby(self, primary, slot_name, dbname="postgres"):
        """Create logical slot *slot_name* on this standby.

        Starts ``pg_recvlogical --create-slot`` (which blocks until the needed
        xl_running_xacts record appears), has *primary* log a standby snapshot
        to produce it, then waits for slot creation to finish.
        """
        argv = [
            self._resolve("pg_recvlogical"),
            "--dbname", self.connstr(dbname),
            "--plugin", "test_decoding",
            "--slot", slot_name,
            "--create-slot",
        ]
        print("# Running (background): " + " ".join(argv))
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        # Arrange for the xl_running_xacts record pg_recvlogical is waiting for.
        primary.log_standby_snapshot(self, slot_name)
        out, err = proc.communicate()
        assert self.slot(slot_name)["slot_type"] == "logical", (
            f"could not create slot {slot_name}: stdout={out!r} stderr={err!r}"
        )

    def pg_recvlogical_upto(self, dbname, slot_name, endpos, timeout_secs=None,
                            **plugin_options):
        """Read from *slot_name* with pg_recvlogical until *endpos*.

        Returns a CommandResult; on timeout the returncode is None.
        ``--no-loop`` prevents pg_recvlogical from internally retrying on error.
        """
        argv = [
            self._resolve("pg_recvlogical"),
            "--slot", slot_name,
            "--dbname", self.connstr(dbname),
            "--endpos", str(endpos),
            "--file", "-",
            "--no-loop", "--start",
        ]
        for k, v in plugin_options.items():
            if "=" in str(k):
                raise ValueError(
                    "= is not permitted to appear in replication option name"
                )
            argv += ["--option", f"{k}={v}"]
        print("# Running: " + " ".join(argv))
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", check=False,
                timeout=timeout_secs if timeout_secs else None,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                None,
                exc.stdout if isinstance(exc.stdout, str) else "",
                exc.stderr if isinstance(exc.stderr, str) else "",
            )
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def corrupt_page_checksum(self, file, page_offset=0):
        """Invert the pd_checksum field of a page in *file* (offline cluster).

        *file* is relative to the data directory.
        """
        path = os.path.join(self.data_dir, file)
        # Inverts the pd_checksum field (bytes 8-9 of the page header) only.
        mask = b"\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff"
        with open(path, "r+b") as fh:
            fh.seek(page_offset)
            header = bytearray(fh.read(24))
            for i, byte in enumerate(mask):
                header[i] ^= byte
            fh.seek(page_offset)
            fh.write(header)

    # -- server log access ---------------------------------------------------

    def log_content(self):
        """Return the entire current server log as text."""
        if not os.path.exists(self.logfile):
            return ""
        with open(self.logfile, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def log_position(self):
        """Return the current size of the log file, for use as an offset."""
        try:
            return os.path.getsize(self.logfile)
        except FileNotFoundError:
            return 0

    def log_contains(self, pattern, offset=0):
        """Return True if *pattern* matches the log at/after byte *offset*."""
        content = self.log_content()
        return re.search(pattern, content[offset:]) is not None

    def wait_for_log(self, pattern, offset=0, timeout=TIMEOUT_DEFAULT):
        """Wait until *pattern* appears in the log at/after *offset*.

        Returns the log length when matched; raises on timeout.
        """
        regex = re.compile(pattern)

        def _found():
            return regex.search(self.log_content()[offset:]) is not None

        if not poll_until(_found, timeout=timeout):
            raise TimeoutError(f"timed out waiting for log pattern {pattern!r}")
        return len(self.log_content())

    def _close_sessions(self):
        for sess in self._sessions.values():
            sess.close()
        self._sessions.clear()

    # -- node-scoped command_* assertions ------------------------------------

    def command_ok(self, cmd, msg=None):
        return self.pg_bin.command_ok(cmd, msg)

    def command_fails(self, cmd, msg=None):
        return self.pg_bin.command_fails(cmd, msg)

    def command_like(self, cmd, pattern, msg=None):
        return self.pg_bin.command_like(cmd, pattern, msg)

    def command_fails_like(self, cmd, pattern, msg=None):
        return self.pg_bin.command_fails_like(cmd, pattern, msg)

    def command_exit_is(self, cmd, code, msg=None):
        return self.pg_bin.command_exit_is(cmd, code, msg)

    def command_checks_all(self, cmd, expected_ret, stdout_res, stderr_res, msg=None):
        return self.pg_bin.command_checks_all(cmd, expected_ret, stdout_res, stderr_res, msg)

    def issues_sql_like(self, cmd, pattern, msg=None):
        """Run *cmd* successfully and assert *pattern* appears in the server log.

        The cluster must have log_statement enabled for the SQL to be logged.
        """
        offset = self.log_position()
        self.command_ok(cmd, msg)
        log = self.log_content()[offset:]
        assert re.search(pattern, log), (
            (msg or "issues_sql_like") + f": SQL /{pattern}/ not found in server log\n{log}"
        )

    def issues_sql_unlike(self, cmd, pattern, msg=None):
        """Run *cmd* successfully and assert *pattern* does NOT appear in the log."""
        offset = self.log_position()
        self.command_ok(cmd, msg)
        log = self.log_content()[offset:]
        assert not re.search(pattern, log), (
            (msg or "issues_sql_unlike") + f": SQL /{pattern}/ unexpectedly found in server log\n{log}"
        )

    # -- teardown ------------------------------------------------------------

    def teardown(self):
        try:
            self.stop("immediate")
        except Exception:
            pass
