# Copyright (c) 2026, PostgreSQL Global Development Group

"""Test the negotiation of combined SSL and GSS requests.  This test relies on
both SSL and GSS requests to be rejected first, followed by more requests.
"""

import struct

import pytest


def test_negotiate(create_pg):
    node = create_pg("main", start=False)
    node.append_conf("log_min_messages = debug2")
    node.append_conf(
        "log_connections = 'receipt,authentication,authorization'"
    )
    node.append_conf("trace_connection_negotiation=on")
    node.start()

    if not node.raw_connect_works():
        pytest.skip("this test requires working raw_connect()")

    sock = node.raw_connect()

    # SSLRequest: packet length followed by NEGOTIATE_SSL_CODE.
    ssl_request = struct.pack("!Ihh", 8, 1234, 5679)

    # GSSENCRequest: packet length followed by NEGOTIATE_GSS_CODE.
    gss_request = struct.pack("!Ihh", 8, 1234, 5680)

    # Send SSLRequest, reject or bypass.
    sock.send(ssl_request)
    reply = sock.recv(1)
    if reply != b"N":
        sock.close()
        pytest.skip("server accepted SSL; test requires SSL to be rejected")

    # Send GSSENCRequest, reject or bypass test.
    sock.send(gss_request)
    reply = sock.recv(1)
    if reply != b"N":
        sock.close()
        pytest.skip("server accepted GSS; test requires GSS to be rejected")

    log_offset = node.log_position()

    # Send a second SSLRequest, now that we know that both SSL and GSS have
    # been rejected for this connection.  We are done with both requests, so
    # extra requests will be rejected and fail with an invalid protocol
    # version, and the connection should be closed by the server.
    sock.send(ssl_request)

    # Try to read a response, there should be nothing, and certainly not an
    # extra 'N' message indicating a rejection.
    reply = sock.recv(1024)
    assert reply != b"N", (
        "server does not re-enter SSL negotiation after SSL+GSS were both tried"
    )

    sock.close()
    node.wait_for_log(
        r"FATAL: .* unsupported frontend protocol 1234.5679", log_offset
    )

    # Check extra connection with a simple query.
    result = node.safe_sql("select 1;")
    assert result == "1", "server able to accept connection"

    node.stop()
