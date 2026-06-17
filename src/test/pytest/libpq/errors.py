# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Exceptions raised by the libpq wrapper."""


class LibpqError(Exception):
    """Base class for libpq-related errors (connection or query failure)."""


class PqConnectionError(LibpqError):
    """Raised when a libpq connection cannot be established."""


class QueryError(LibpqError):
    """Raised by the *_safe query helpers when a statement fails.

    ``sqlstate`` carries the five-character SQLSTATE from libpq when available
    (None otherwise); ``sqlstate_class`` is its first two characters. These are
    the stable, locale-independent way to assert on a specific error condition,
    rather than matching against the human-readable message text.
    """

    def __init__(self, message, *, sqlstate=None):
        super().__init__(message)
        self.sqlstate = sqlstate

    @property
    def sqlstate_class(self):
        """The two-character SQLSTATE class, or None if no SQLSTATE is set."""
        if self.sqlstate and len(self.sqlstate) >= 2:
            return self.sqlstate[:2]
        return None


# Named QueryError subclasses for the SQLSTATEs tests most often assert on, so a
# test can write ``with pytest.raises(QueryCanceled):`` instead of catching the
# generic QueryError and then checking ``.sqlstate``. Each maps to its
# five-character SQLSTATE; query_error_for() picks the right class when raising.
class SyntaxErrorState(QueryError):
    """42601 -- syntax_error."""


class UndefinedTable(QueryError):
    """42P01 -- undefined_table."""


class UndefinedColumn(QueryError):
    """42703 -- undefined_column."""


class InsufficientPrivilege(QueryError):
    """42501 -- insufficient_privilege."""


class UniqueViolation(QueryError):
    """23505 -- unique_violation."""


class ForeignKeyViolation(QueryError):
    """23503 -- foreign_key_violation."""


class NotNullViolation(QueryError):
    """23502 -- not_null_violation."""


class CheckViolation(QueryError):
    """23514 -- check_violation."""


class SerializationFailure(QueryError):
    """40001 -- serialization_failure."""


class DeadlockDetected(QueryError):
    """40P01 -- deadlock_detected."""


class QueryCanceled(QueryError):
    """57014 -- query_canceled."""


class AdminShutdown(QueryError):
    """57P01 -- admin_shutdown."""


class CrashShutdown(QueryError):
    """57P02 -- crash_shutdown."""


class CannotConnectNow(QueryError):
    """57P03 -- cannot_connect_now."""


class ReadOnlySqlTransaction(QueryError):
    """25006 -- read_only_sql_transaction."""


class ObjectInUse(QueryError):
    """55006 -- object_in_use."""


# SQLSTATE -> exception subclass. Anything not listed raises a plain QueryError.
_SQLSTATE_EXCEPTIONS = {
    "42601": SyntaxErrorState,
    "42P01": UndefinedTable,
    "42703": UndefinedColumn,
    "42501": InsufficientPrivilege,
    "23505": UniqueViolation,
    "23503": ForeignKeyViolation,
    "23502": NotNullViolation,
    "23514": CheckViolation,
    "40001": SerializationFailure,
    "40P01": DeadlockDetected,
    "57014": QueryCanceled,
    "57P01": AdminShutdown,
    "57P02": CrashShutdown,
    "57P03": CannotConnectNow,
    "25006": ReadOnlySqlTransaction,
    "55006": ObjectInUse,
}


def query_error_for(message, sqlstate):
    """Return a QueryError (or its SQLSTATE-specific subclass) for *sqlstate*.

    Used when a statement fails so callers can match on the specific condition
    (e.g. ``pytest.raises(QueryCanceled)``) while still catching the base
    QueryError/LibpqError when they want any failure.
    """
    cls = _SQLSTATE_EXCEPTIONS.get(sqlstate or "", QueryError)
    return cls(message, sqlstate=sqlstate)
