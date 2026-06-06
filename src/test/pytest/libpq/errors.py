# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Exceptions raised by the libpq wrapper."""


class LibpqError(Exception):
    """Base class for libpq-related errors (connection or query failure)."""


class PqConnectionError(LibpqError):
    """Raised when a libpq connection cannot be established."""


class QueryError(LibpqError):
    """Raised by the *_safe query helpers when a statement fails."""
