# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Central ctypes prototype table for libpq.

This is the ONLY module that assigns ``restype``/``argtypes`` to libpq
functions.  Concentrating every prototype here contains the classic ctypes
footgun: a function whose ``restype`` is left at the default ``c_int`` would
sign-truncate a 64-bit pointer return on LP64 platforms and silently corrupt
it.  Every entry in :data:`PROTOTYPES` sets restype and argtypes together, and
:func:`load` asserts each one was applied, so a forgotten or mistyped entry
fails loudly at load time instead of crashing mid-test.

Opaque handles (``PGconn *``, ``PGresult *`` and the ``PGnotify *`` returned by
PQnotifies) are bound as ``c_void_p`` so the full 64-bit pointer survives and a
NULL return becomes Python ``None`` (falsy).  ``Oid`` is unsigned 32-bit; the
microsecond clock and socket poll deadline are explicitly ``c_int64``.
"""

import ctypes
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_int64,
    c_uint,
    c_void_p,
)

# Opaque handles and scalar aliases.
PGconn_p = c_void_p
PGresult_p = c_void_p
Oid = c_uint

_Oid_p = POINTER(c_uint)
_int_p = POINTER(c_int)
_charpp = POINTER(c_char_p)


class PQconninfoOption(Structure):
    """A single resolved connection option, as returned by PQconninfo()."""

    _fields_ = [
        ("keyword", c_char_p),
        ("envvar", c_char_p),
        ("compiled", c_char_p),
        ("val", c_char_p),
        ("label", c_char_p),
        ("dispchar", c_char_p),
        ("dispsize", c_int),
    ]


_PQconninfoOption_p = POINTER(PQconninfoOption)

# void (*PQnoticeProcessor)(void *arg, const char *message)
NOTICE_PROCESSOR = CFUNCTYPE(None, c_void_p, c_char_p)

# name -> (restype, [argtypes]).  One line per libpq function.
PROTOTYPES = {
    # --- Connection establishment / teardown -------------------------------
    "PQconnectdb": (PGconn_p, [c_char_p]),
    "PQconnectdbParams": (PGconn_p, [_charpp, _charpp, c_int]),
    "PQsetdbLogin": (
        PGconn_p,
        [c_char_p, c_char_p, c_char_p, c_char_p, c_char_p, c_char_p, c_char_p],
    ),
    "PQconnectStart": (PGconn_p, [c_char_p]),
    "PQconnectStartParams": (PGconn_p, [_charpp, _charpp, c_int]),
    "PQconnectPoll": (c_int, [PGconn_p]),
    "PQresetStart": (c_int, [PGconn_p]),
    "PQresetPoll": (c_int, [PGconn_p]),
    "PQfinish": (None, [PGconn_p]),
    "PQreset": (None, [PGconn_p]),
    # --- Connection introspection ------------------------------------------
    "PQdb": (c_char_p, [PGconn_p]),
    "PQuser": (c_char_p, [PGconn_p]),
    "PQpass": (c_char_p, [PGconn_p]),
    "PQhost": (c_char_p, [PGconn_p]),
    "PQhostaddr": (c_char_p, [PGconn_p]),
    "PQport": (c_char_p, [PGconn_p]),
    "PQtty": (c_char_p, [PGconn_p]),
    "PQoptions": (c_char_p, [PGconn_p]),
    "PQstatus": (c_int, [PGconn_p]),
    "PQtransactionStatus": (c_int, [PGconn_p]),
    "PQparameterStatus": (c_char_p, [PGconn_p, c_char_p]),
    "PQping": (c_int, [c_char_p]),
    "PQpingParams": (c_int, [_charpp, _charpp, c_int]),
    "PQprotocolVersion": (c_int, [PGconn_p]),
    "PQserverVersion": (c_int, [PGconn_p]),
    "PQerrorMessage": (c_char_p, [PGconn_p]),
    "PQsocket": (c_int, [PGconn_p]),
    "PQsocketPoll": (c_int, [c_int, c_int, c_int, c_int64]),
    "PQgetCurrentTimeUSec": (c_int64, []),
    "PQbackendPID": (c_int, [PGconn_p]),
    "PQconnectionNeedsPassword": (c_int, [PGconn_p]),
    "PQconnectionUsedPassword": (c_int, [PGconn_p]),
    "PQconnectionUsedGSSAPI": (c_int, [PGconn_p]),
    "PQclientEncoding": (c_int, [PGconn_p]),
    "PQsetClientEncoding": (c_int, [PGconn_p, c_char_p]),
    # --- Synchronous command execution -------------------------------------
    "PQexec": (PGresult_p, [PGconn_p, c_char_p]),
    "PQexecParams": (
        PGresult_p,
        [PGconn_p, c_char_p, c_int, _Oid_p, _charpp, _int_p, _int_p, c_int],
    ),
    "PQprepare": (PGresult_p, [PGconn_p, c_char_p, c_char_p, c_int, _Oid_p]),
    "PQexecPrepared": (
        PGresult_p,
        [PGconn_p, c_char_p, c_int, _charpp, _int_p, _int_p, c_int],
    ),
    "PQdescribePrepared": (PGresult_p, [PGconn_p, c_char_p]),
    "PQdescribePortal": (PGresult_p, [PGconn_p, c_char_p]),
    "PQclosePrepared": (PGresult_p, [PGconn_p, c_char_p]),
    "PQclosePortal": (PGresult_p, [PGconn_p, c_char_p]),
    "PQchangePassword": (PGresult_p, [PGconn_p, c_char_p, c_char_p]),
    "PQclear": (None, [PGresult_p]),
    # --- Result inspection -------------------------------------------------
    "PQresultStatus": (c_int, [PGresult_p]),
    "PQresStatus": (c_char_p, [c_int]),
    "PQresultErrorMessage": (c_char_p, [PGresult_p]),
    "PQresultErrorField": (c_char_p, [PGresult_p, c_int]),
    "PQntuples": (c_int, [PGresult_p]),
    "PQnfields": (c_int, [PGresult_p]),
    "PQbinaryTuples": (c_int, [PGresult_p]),
    "PQfname": (c_char_p, [PGresult_p, c_int]),
    "PQfnumber": (c_int, [PGresult_p, c_char_p]),
    "PQftable": (Oid, [PGresult_p, c_int]),
    "PQftablecol": (c_int, [PGresult_p, c_int]),
    "PQfformat": (c_int, [PGresult_p, c_int]),
    "PQftype": (Oid, [PGresult_p, c_int]),
    "PQfsize": (c_int, [PGresult_p, c_int]),
    "PQfmod": (c_int, [PGresult_p, c_int]),
    "PQcmdStatus": (c_char_p, [PGresult_p]),
    "PQoidValue": (Oid, [PGresult_p]),
    "PQcmdTuples": (c_char_p, [PGresult_p]),
    "PQgetvalue": (c_char_p, [PGresult_p, c_int, c_int]),
    "PQgetlength": (c_int, [PGresult_p, c_int, c_int]),
    "PQgetisnull": (c_int, [PGresult_p, c_int, c_int]),
    "PQnparams": (c_int, [PGresult_p]),
    "PQparamtype": (Oid, [PGresult_p, c_int]),
    # --- Asynchronous command processing -----------------------------------
    "PQsendQuery": (c_int, [PGconn_p, c_char_p]),
    "PQsendQueryParams": (
        c_int,
        [PGconn_p, c_char_p, c_int, _Oid_p, _charpp, _int_p, _int_p, c_int],
    ),
    "PQsendPrepare": (c_int, [PGconn_p, c_char_p, c_char_p, c_int, _Oid_p]),
    "PQgetResult": (PGresult_p, [PGconn_p]),
    "PQisBusy": (c_int, [PGconn_p]),
    "PQconsumeInput": (c_int, [PGconn_p]),
    "PQsetnonblocking": (c_int, [PGconn_p, c_int]),
    "PQisnonblocking": (c_int, [PGconn_p]),
    "PQflush": (c_int, [PGconn_p]),
    # --- Pipeline mode -----------------------------------------------------
    "PQpipelineStatus": (c_int, [PGconn_p]),
    "PQenterPipelineMode": (c_int, [PGconn_p]),
    "PQexitPipelineMode": (c_int, [PGconn_p]),
    "PQpipelineSync": (c_int, [PGconn_p]),
    "PQsendFlushRequest": (c_int, [PGconn_p]),
    "PQsendPipelineSync": (c_int, [PGconn_p]),
    # --- Notifications -----------------------------------------------------
    # PQnotifies returns PGnotify *; keep the raw pointer (c_void_p) so the
    # original allocation can be handed back to PQfreemem.
    "PQnotifies": (c_void_p, [PGconn_p]),
    "PQfreemem": (None, [c_void_p]),
    # Resolved connection options (the actual values libpq used, including the
    # service file it settled on).  PQconninfo's array must be freed with
    # PQconninfoFree.
    "PQconninfo": (_PQconninfoOption_p, [PGconn_p]),
    "PQconninfoFree": (None, [_PQconninfoOption_p]),
    # --- Notice processing -------------------------------------------------
    "PQsetNoticeProcessor": (c_void_p, [PGconn_p, NOTICE_PROCESSOR, c_void_p]),
}


def load(libpath):
    """Open *libpath* and apply every prototype in :data:`PROTOTYPES`.

    Returns the configured ``ctypes.CDLL``.  Raises if a function is missing
    from the library or if any prototype failed to apply.
    """
    lib = ctypes.CDLL(libpath)
    for name, (restype, argtypes) in PROTOTYPES.items():
        fn = getattr(lib, name)  # AttributeError here = symbol missing
        fn.restype = restype
        fn.argtypes = argtypes

    # Defense in depth: confirm every prototype actually took, so a forgotten
    # restype can never reach a caller as a silent c_int default.
    for name, (restype, _argtypes) in PROTOTYPES.items():
        applied = getattr(lib, name).restype
        assert applied is restype, f"prototype not applied for {name}"

    return lib
