# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""libpq enum constants used by the ctypes backend.

The values are the integer codes from libpq-fe.h and are exposed both as
IntEnum members (so they print their symbolic name in errors) and as
module-level names so framework code can use the bare symbols.
"""

import enum


class ConnStatusType(enum.IntEnum):
    """Connection status codes (ConnStatusType in libpq-fe.h)."""

    CONNECTION_OK = 0
    CONNECTION_BAD = 1
    CONNECTION_STARTED = 2
    CONNECTION_MADE = 3
    CONNECTION_AWAITING_RESPONSE = 4
    CONNECTION_AUTH_OK = 5
    CONNECTION_SETENV = 6
    CONNECTION_SSL_STARTUP = 7
    CONNECTION_NEEDED = 8
    CONNECTION_CHECK_WRITABLE = 9
    CONNECTION_CONSUME = 10
    CONNECTION_GSS_STARTUP = 11
    CONNECTION_CHECK_TARGET = 12
    CONNECTION_CHECK_STANDBY = 13
    CONNECTION_ALLOCATED = 14


class ExecStatusType(enum.IntEnum):
    """Result status codes returned by PQresultStatus()."""

    PGRES_EMPTY_QUERY = 0
    PGRES_COMMAND_OK = 1
    PGRES_TUPLES_OK = 2
    PGRES_COPY_OUT = 3
    PGRES_COPY_IN = 4
    PGRES_BAD_RESPONSE = 5
    PGRES_NONFATAL_ERROR = 6
    PGRES_FATAL_ERROR = 7
    PGRES_COPY_BOTH = 8
    PGRES_SINGLE_TUPLE = 9
    PGRES_PIPELINE_SYNC = 10
    PGRES_PIPELINE_ABORTED = 11
    PGRES_TUPLES_CHUNK = 12


class PostgresPollingStatusType(enum.IntEnum):
    """Async connection polling status (PQconnectPoll())."""

    PGRES_POLLING_FAILED = 0
    PGRES_POLLING_READING = 1
    PGRES_POLLING_WRITING = 2
    PGRES_POLLING_OK = 3
    PGRES_POLLING_ACTIVE = 4


class PGPing(enum.IntEnum):
    """Server status codes returned by PQping()."""

    PQPING_OK = 0
    PQPING_REJECT = 1
    PQPING_NO_RESPONSE = 2
    PQPING_NO_ATTEMPT = 3


class PGTransactionStatusType(enum.IntEnum):
    """Transaction status codes returned by PQtransactionStatus()."""

    PQTRANS_IDLE = 0
    PQTRANS_ACTIVE = 1
    PQTRANS_INTRANS = 2
    PQTRANS_INERROR = 3
    PQTRANS_UNKNOWN = 4


# Flatten every member into module-level names (CONNECTION_OK, PGRES_TUPLES_OK,
# ...) so test/framework code can use the bare names, while comparisons against
# IntEnum members still succeed.
for _grp in (
    ConnStatusType,
    ExecStatusType,
    PostgresPollingStatusType,
    PGPing,
    PGTransactionStatusType,
):
    for _member in _grp:
        globals()[_member.name] = _member
del _grp, _member
