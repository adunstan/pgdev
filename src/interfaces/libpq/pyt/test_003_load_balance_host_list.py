# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test load balancing across the list of different hosts in the host
parameter of the connection string.

This framework uses the in-process libpq Session, so each node's "host" is its
own socket directory (or the loopback address on TCP) and the nodes are still
distinguished by listening on different ports.  We observe which node answered
by checking the executed statement in each node's server log
(log_statement = all) and counting log occurrences.
"""

import re

import pytest

from libpq import Session
from libpq.errors import PqConnectionError


def connect_with(node, connstr, sql=None):
    """Open a libpq Session with *connstr*, optionally running *sql*.

    Returns the Session on success; the caller owns it and must close it.
    Raises PqConnectionError on connection failure (mirrors connect_fails).
    """
    sess = Session(connstr=connstr, libdir=node.libdir)
    if sql is not None:
        sess.query_safe(sql)
    return sess


def count_statements(node, sql):
    """Count occurrences of "statement: <sql>" in *node*'s server log."""
    pattern = "statement: " + re.escape(sql)
    return len(re.findall(pattern, node.log_content()))


def test_003_load_balance_host_list(create_pg):
    # Cluster setup which is shared for testing both load balancing methods.
    # Each node listens on its own socket directory and port; logging every
    # statement lets us tell which node served a given connection.
    node1 = create_pg("node1", start=False)
    node2 = create_pg("node2", start=False)
    node3 = create_pg("node3", start=False)

    for node in (node1, node2, node3):
        node.append_conf("log_statement = all\n")
        node.start()

    # Build the shared host/port lists.  In this unix-socket-only framework
    # each node's host is its socket directory.
    hostlist = ",".join(n.host for n in (node1, node2, node3))
    portlist = ",".join(str(n.port) for n in (node1, node2, node3))

    # load_balance_hosts doesn't accept unknown values.
    with pytest.raises(PqConnectionError) as excinfo:
        connect_with(
            node1,
            f"host={hostlist} port={portlist} dbname=postgres load_balance_hosts=doesnotexist",
        )
    assert re.search(
        r'invalid load_balance_hosts value: "doesnotexist"', str(excinfo.value)
    ), "load_balance_hosts doesn't accept unknown values"

    # load_balance_hosts=disable should always choose the first one.
    sess = connect_with(
        node1,
        f"host={hostlist} port={portlist} dbname=postgres load_balance_hosts=disable",
        sql="SELECT 'connect1'",
    )
    sess.close()
    assert (
        count_statements(node1, "SELECT 'connect1'") >= 1
    ), "load_balance_hosts=disable connects to the first node"

    # Statistically the following loop with load_balance_hosts=random will
    # almost certainly connect at least once to each of the nodes.  The chance
    # of that not happening is so small that it's negligible:
    # (2/3)^50 = 1.56832855e-9
    for _ in range(1, 51):
        sess = connect_with(
            node1,
            f"host={hostlist} port={portlist} dbname=postgres load_balance_hosts=random",
            sql="SELECT 'connect2'",
        )
        sess.close()

    node1_occurrences = count_statements(node1, "SELECT 'connect2'")
    node2_occurrences = count_statements(node2, "SELECT 'connect2'")
    node3_occurrences = count_statements(node3, "SELECT 'connect2'")

    total_occurrences = node1_occurrences + node2_occurrences + node3_occurrences

    assert node1_occurrences > 1, "received at least one connection on node1"
    assert node2_occurrences > 1, "received at least one connection on node2"
    assert node3_occurrences > 1, "received at least one connection on node3"
    assert total_occurrences == 50, "received 50 connections across all nodes"

    node1.stop()
    node2.stop()

    # load_balance_hosts=disable should continue trying hosts until it finds a
    # working one.
    sess = connect_with(
        node3,
        f"host={hostlist} port={portlist} dbname=postgres load_balance_hosts=disable",
        sql="SELECT 'connect3'",
    )
    sess.close()
    assert (
        count_statements(node3, "SELECT 'connect3'") >= 1
    ), "load_balance_hosts=disable continues until it connects to a working node"

    # Also with load_balance_hosts=random we continue to the next nodes if
    # previous ones are down.  Connect a few times to make sure it's not just
    # lucky.
    for _ in range(1, 6):
        sess = connect_with(
            node3,
            f"host={hostlist} port={portlist} dbname=postgres load_balance_hosts=random",
            sql="SELECT 'connect4'",
        )
        sess.close()
    assert (
        count_statements(node3, "SELECT 'connect4'") >= 5
    ), "load_balance_hosts=random continues until it connects to a working node"
