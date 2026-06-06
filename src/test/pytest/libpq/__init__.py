# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""ctypes-based libpq bindings for PostgreSQL tests.

This is a complete in-process client: :class:`~libpq.session.Session` supports
synchronous, asynchronous and pipeline execution, LISTEN/NOTIFY, and notice
capture, so tests need neither psql subprocesses nor a third-party driver.
"""

from . import constants, errors, oids
from .constants import (
    ConnStatusType,
    ExecStatusType,
    PGPing,
    PGTransactionStatusType,
    PostgresPollingStatusType,
)
from .errors import LibpqError, QueryError
from .result import ResultData
from .session import Session, connect

__all__ = [
    "constants",
    "errors",
    "oids",
    "ConnStatusType",
    "ExecStatusType",
    "PGPing",
    "PGTransactionStatusType",
    "PostgresPollingStatusType",
    "LibpqError",
    "QueryError",
    "ResultData",
    "Session",
    "connect",
]
