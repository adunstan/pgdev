# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Process-environment setup for the test suite.

Forces a C locale for stable messages and clears any inherited PG* connection
variables that would otherwise steer client programs at the wrong server.  Call
:func:`prepare_environment` once before any server is started.
"""

import os

# PG* variables that must not leak into the test environment (they would
# override host/port/user/db that the framework sets explicitly).
_PG_VARS_TO_CLEAR = (
    "PGDATABASE",
    "PGUSER",
    "PGPORT",
    "PGHOST",
    "PGHOSTADDR",
    "PGSERVICE",
    "PGSSLMODE",
    "PGREQUIRESSL",
    "PGCONNECT_TIMEOUT",
    "PGDATA",
    "PGCLIENTENCODING",
    "PGOPTIONS",
)

_prepared = False


def prepare_environment():
    """Idempotently set C locale and clear PG* variables."""
    global _prepared
    if _prepared:
        return
    os.environ["LC_ALL"] = "C"
    os.environ["LC_MESSAGES"] = "C"
    for var in _PG_VARS_TO_CLEAR:
        os.environ.pop(var, None)
    # Default the database to "postgres" so a connection string without an
    # explicit dbname (e.g. in load-balancing tests) does not fall through to
    # the OS user name.
    os.environ["PGDATABASE"] = "postgres"
    _prepared = True
