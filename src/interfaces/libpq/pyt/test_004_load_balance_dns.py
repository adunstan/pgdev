# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test load balancing based on DNS records with multiple IPs.

This tests load balancing based on a DNS entry that contains multiple records
for different IPs.  Since setting up a DNS server is more effort than we
consider reasonable to run this test, this situation is instead imitated by
using a hosts file where a single hostname maps to multiple different IP
addresses.  This test requires the administrator to add the following lines to
the hosts file (if we detect that this hasn't happened we skip the test):

    127.0.0.1 pg-loadbalancetest
    127.0.0.2 pg-loadbalancetest
    127.0.0.3 pg-loadbalancetest

Windows or Linux are required to run this test because these OSes allow binding
to 127.0.0.2 and 127.0.0.3 addresses by default, but other OSes don't.  We need
to bind to different IP addresses, so that we can use these different IP
addresses in the hosts file.

The hosts file needs to be prepared before running this test.  We don't do it
on the fly, because it requires root permissions to change the hosts file.  In
CI we set up the previously mentioned rules in the hosts file, so that this load
balancing method is tested.

NOTE (framework gap): this test is gated behind PG_TEST_EXTRA=load_balance, the
special /etc/hosts entries above, and a Linux/Windows host, so it always skips
in this environment.  More importantly, the pytest framework's PostgresServer
is unix-socket-only: init() always writes listen_addresses = '' and serves over
a unix socket, with no machinery to bind each node to a distinct 127.0.0.x
address.  Real execution of this test therefore requires framework support for
(a) TCP listen_addresses and (b) per-node binding to 127.0.0.1/2/3 -- see the
inline comments in the test body.
"""

import os
import re
import sys

import pytest

from libpq import Session
from libpq.errors import ConnectionError as PqConnectionError

# The hostname that the prepared hosts file maps to 127.0.0.1/2/3.
LOADBALANCE_HOST = "pg-loadbalancetest"
HOSTS_PATTERN = re.compile(r"127\.0\.0\.[1-3] pg-loadbalancetest")


# -- skip gating -------------------------------------------------------------

def _skip_reason():
    """Return a skip reason if this test cannot run here, else None."""
    # Potentially unsafe test load_balance not enabled in PG_TEST_EXTRA.
    extra = os.environ.get("PG_TEST_EXTRA", "")
    if not re.search(r"\bload_balance\b", extra):
        return "Potentially unsafe test load_balance not enabled in PG_TEST_EXTRA"

    # Windows or Linux are required: these OSes allow binding to 127.0.0.2 and
    # 127.0.0.3 by default, but other OSes don't.
    can_bind_to_127_0_0_2 = sys.platform.startswith("linux") or sys.platform == "win32"
    if not can_bind_to_127_0_0_2:
        return "load_balance test only supported on Linux and Windows"

    # The hosts file must contain the three pg-loadbalancetest mappings.
    if sys.platform == "win32":
        hosts_path = r"c:\Windows\System32\Drivers\etc\hosts"
    else:
        hosts_path = "/etc/hosts"
    try:
        with open(hosts_path, "r", encoding="utf-8", errors="replace") as fh:
            hosts_content = fh.read()
    except OSError:
        hosts_content = ""
    if len(HOSTS_PATTERN.findall(hosts_content)) != 3:
        return "hosts file was not prepared for DNS load balance test"

    # Even with the hosts file in place, this test needs all three nodes bound
    # to distinct loopback addresses (127.0.0.1/2/3) on the *same* TCP port so
    # the single DNS name selects between them.  The pytest PostgresServer
    # framework serves each node on its own unix socket and its own free port,
    # with no machinery to bind each node to a distinct loopback IP, so the
    # scenario cannot be reproduced here.
    return ("DNS load balancing needs per-node TCP binding to distinct "
            "loopback IPs on a shared port, which the pytest framework's "
            "PostgresServer does not support")


# Module-level skip: in the conversion environment load_balance is not in
# PG_TEST_EXTRA, so the whole module skips cleanly and never starts a server.
_skip = _skip_reason()
if _skip is not None:
    pytest.skip(_skip, allow_module_level=True)


# -- helpers (NOT named test_*) ----------------------------------------------

def _connect_ok(node, connstr, msg, *, sql=None, log_like=None):
    """Connect with *connstr*, optionally run *sql*, assert success and logs.

    Opens a fresh libpq Session (no psql subprocess), runs *sql* if given, and
    -- when *log_like* patterns are supplied -- asserts each appears in
    *node*'s server log.
    """
    offset = node.log_position()
    sess = None
    try:
        sess = Session(connstr=connstr, libdir=node.libdir)
        if sql is not None:
            sess.query_safe(sql)
    except PqConnectionError as exc:  # pragma: no cover - only runs when enabled
        raise AssertionError(f"{msg}: connection failed: {exc}") from exc
    finally:
        if sess is not None:
            sess.close()

    for pattern in log_like or []:
        log = node.log_content()[offset:]
        assert re.search(pattern, log), (
            f"{msg}: pattern /{pattern}/ not found in server log\n{log}"
        )


def _occurrences(node, pattern):
    return len(re.findall(pattern, node.log_content()))


# -- the test ----------------------------------------------------------------

def test_004_load_balance_dns(create_pg):
    port = None  # noqa: F841 - see framework-gap note below

    # Framework-gap note: this scenario needs all three nodes bound to distinct
    # loopback addresses (127.0.0.1/2/3) on the *same* TCP port so that the
    # single DNS name pg-loadbalancetest (-> 127.0.0.1/2/3) selects between
    # them.  The PostgresServer fixture here is unix-socket-only and assigns
    # each node its own free port, so it cannot reproduce the shared-port /
    # per-IP binding the DNS load-balancing behaviour depends on.  Making this
    # test actually run requires extending the framework with TCP
    # listen_addresses and per-node loopback binding support.

    node1 = create_pg("node1", start=False)
    node2 = create_pg("node2", start=False)
    node3 = create_pg("node3", start=False)

    for node in (node1, node2, node3):
        # log_statement = all so connect_ok's log_like checks can see the SQL.
        node.append_conf("log_statement = all\n")
        node.start()

    # load_balance_hosts=disable should always choose the first one.
    _connect_ok(
        node1,
        f"host={LOADBALANCE_HOST} port={node1.port} load_balance_hosts=disable",
        "load_balance_hosts=disable connects to the first node",
        sql="SELECT 'connect1'",
        log_like=[r"statement: SELECT 'connect1'"],
    )

    # Statistically the following loop with load_balance_hosts=random will
    # almost certainly connect at least once to each of the nodes.  The chance
    # of that not happening is negligible: (2/3)^50 = 1.56832855e-9.
    for _ in range(50):
        _connect_ok(
            node1,
            f"host={LOADBALANCE_HOST} port={node1.port} load_balance_hosts=random",
            "repeated connections with random load balancing",
            sql="SELECT 'connect2'",
        )

    node1_occurrences = _occurrences(node1, r"statement: SELECT 'connect2'")
    node2_occurrences = _occurrences(node2, r"statement: SELECT 'connect2'")
    node3_occurrences = _occurrences(node3, r"statement: SELECT 'connect2'")

    total_occurrences = node1_occurrences + node2_occurrences + node3_occurrences

    assert node1_occurrences > 1, "received at least one connection on node1"
    assert node2_occurrences > 1, "received at least one connection on node2"
    assert node3_occurrences > 1, "received at least one connection on node3"
    assert total_occurrences == 50, "received 50 connections across all nodes"

    node1.stop()
    node2.stop()

    # load_balance_hosts=disable should continue trying hosts until it finds a
    # working one.
    _connect_ok(
        node3,
        f"host={LOADBALANCE_HOST} port={node3.port} load_balance_hosts=disable",
        "load_balance_hosts=disable continues until it connects to a working node",
        sql="SELECT 'connect3'",
        log_like=[r"statement: SELECT 'connect3'"],
    )

    # Also with load_balance_hosts=random we continue to the next nodes if
    # previous ones are down.  Connect a few times to make sure it's not luck.
    for _ in range(5):
        _connect_ok(
            node3,
            f"host={LOADBALANCE_HOST} port={node3.port} load_balance_hosts=random",
            "load_balance_hosts=random continues until it connects to a working node",
            sql="SELECT 'connect4'",
            log_like=[r"statement: SELECT 'connect4'"],
        )
