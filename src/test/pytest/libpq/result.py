# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Result extraction from a libpq PGresult.

A query result is represented by :class:`ResultData`, which exposes status,
error_message, names, types, rows, and psqlout.  All values come back as text
(result format 0), so ``psqlout`` matches ``psql -A -t`` output.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .constants import ExecStatusType

# PG_DIAG_SQLSTATE field id from postgres_ext.h, for PQresultErrorField().
_PG_DIAG_SQLSTATE = ord("C")


def _decode(raw):
    """Decode a libpq C string (bytes or None) to str/None."""
    if raw is None:
        return None
    return raw.decode("utf-8", "replace")


@dataclass
class ResultData:
    """Structured form of the data returned by Session query methods."""

    status: int
    error_message: Optional[str] = None
    sqlstate: Optional[str] = None
    names: List[str] = field(default_factory=list)
    types: List[int] = field(default_factory=list)
    rows: List[List[Optional[str]]] = field(default_factory=list)
    psqlout: str = ""


def extract_result_data(lib, result, conn):
    """Build a :class:`ResultData` from a PGresult pointer.

    On a failed status the error comes from this result
    (PQresultErrorMessage), falling back to the connection-level message
    (*conn*) only when the result carries no error text.
    """
    status = lib.PQresultStatus(result)
    res = ResultData(status=status)

    if status not in (ExecStatusType.PGRES_TUPLES_OK, ExecStatusType.PGRES_COMMAND_OK):
        res.error_message = _decode(lib.PQresultErrorMessage(result)) or _decode(
            lib.PQerrorMessage(conn)
        )
        res.sqlstate = _decode(lib.PQresultErrorField(result, _PG_DIAG_SQLSTATE))
        return res
    if status == ExecStatusType.PGRES_COMMAND_OK:
        return res

    ntuples = lib.PQntuples(result)
    nfields = lib.PQnfields(result)
    for fld in range(nfields):
        res.names.append(_decode(lib.PQfname(result, fld)))
        res.types.append(lib.PQftype(result, fld))

    textrows = []
    for nrow in range(ntuples):
        row = []
        for fld in range(nfields):
            val = _decode(lib.PQgetvalue(result, nrow, fld))
            if (val or "") == "" and lib.PQgetisnull(result, nrow, fld):
                val = None
            row.append(val)
        res.rows.append(row)
        # join renders NULL (None) as the empty string.
        textrows.append("|".join("" if v is None else v for v in row))

    if ntuples:
        res.psqlout = "\n".join(textrows)
    return res
