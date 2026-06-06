# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test amcheck's verify_heapam against deliberately corrupted heap pages."""

import os
import re
import struct


# Regexes matching the various line-pointer-corruption checks in
# verify_heapam.c, hit by corrupt_first_page on both little-endian and
# big-endian architectures.
_HEAP_CORRUPTION_RES = [
    re.compile(
        r"line pointer redirection to item at offset \d+ "
        r"precedes minimum offset \d+"
    ),
    re.compile(
        r"line pointer redirection to item at offset \d+ exceeds maximum offset \d+"
    ),
    re.compile(r"line pointer to page offset \d+ is not maximally aligned"),
    re.compile(
        r"line pointer length \d+ is less than the minimum tuple header size \d+"
    ),
    re.compile(
        r"line pointer to page offset \d+ with length \d+ "
        r"ends beyond maximum page offset \d+"
    ),
]


def relation_filepath(node, session, relname):
    """Return the filesystem path for the named relation."""
    pgdata = node.data_dir
    rel = session.query_oneval(f"SELECT pg_relation_filepath('{relname}')")
    assert rel is not None, f"path not found for relation {relname}"
    return os.path.join(pgdata, rel)


def fresh_test_table(session, relname):
    """(Re)create and populate a test table of the given name."""
    return session.do(
        f"""
        DROP TABLE IF EXISTS {relname} CASCADE;
        CREATE TABLE {relname} (a integer, b text);
        ALTER TABLE {relname} SET (autovacuum_enabled=false);
        ALTER TABLE {relname} ALTER b SET STORAGE external;
        INSERT INTO {relname} (a, b)
            (SELECT gs, repeat('b',gs*10) FROM generate_series(1,1000) gs);
        BEGIN;
        SAVEPOINT s1;
        SELECT 1 FROM {relname} WHERE a = 42 FOR UPDATE;
        UPDATE {relname} SET b = b WHERE a = 42;
        RELEASE s1;
        SAVEPOINT s1;
        SELECT 1 FROM {relname} WHERE a = 42 FOR UPDATE;
        UPDATE {relname} SET b = b WHERE a = 42;
        COMMIT;
    """
    )


def fresh_test_sequence(session, seqname):
    """Create a test sequence of the given name."""
    return session.do(
        f"""
        DROP SEQUENCE IF EXISTS {seqname} CASCADE;
        CREATE SEQUENCE {seqname}
            INCREMENT BY 13
            MINVALUE 17
            START WITH 23;
        SELECT nextval('{seqname}');
        SELECT setval('{seqname}', currval('{seqname}') + nextval('{seqname}'));
    """
    )


def advance_test_sequence(session, seqname):
    """Call SQL functions to increment the sequence."""
    return session.query_oneval(f"SELECT nextval('{seqname}')")


def set_test_sequence(session, seqname):
    """Call SQL functions to set the sequence."""
    return session.query_oneval(f"SELECT setval('{seqname}', 102)")


def reset_test_sequence(session, seqname):
    """Call SQL functions to reset the sequence."""
    return session.do(f"ALTER SEQUENCE {seqname} RESTART WITH 51")


def corrupt_first_page(node, session, relname):
    """Stop the node, corrupt the first page of the relation, restart it."""
    relpath = relation_filepath(node, session, relname)

    session.close()
    node.stop()

    with open(relpath, "r+b") as fh:
        # Corrupt some line pointers.  The values are chosen to hit the
        # various line-pointer-corruption checks in verify_heapam.c
        # on both little-endian and big-endian architectures.
        fh.seek(32)
        fh.write(
            struct.pack(
                "<6L",
                0xAAA15550,
                0xAAA0D550,
                0x00010000,
                0x00008000,
                0x0000800F,
                0x001E8000,
            )
        )

    node.start()
    session.reconnect()


def detects_corruption(session, function, *res):
    """Assert that verify_heapam output matches each corruption regex."""
    result = session.query_tuples(f"SELECT * FROM {function}")
    for regex in res:
        assert regex.search(result), f"expected /{regex.pattern}/ in:\n{result}"


def detects_heap_corruption(session, function):
    """Assert verify_heapam reports the expected heap corruption messages."""
    detects_corruption(session, function, *_HEAP_CORRUPTION_RES)


def detects_no_corruption(session, function):
    """Assert verify_heapam reports no corruption (empty output)."""
    result = session.query_tuples(f"SELECT * FROM {function}")
    assert result == "", f"expected no corruption, got:\n{result}"


def check_all_options_uncorrupted(session, relname):
    """Check various options are stable and report no corruption.

    The relname *must* be an uncorrupted table, or this will fail.
    """
    for stop in ("true", "false"):
        for check_toast in ("true", "false"):
            for skip in ("'none'", "'all-frozen'", "'all-visible'"):
                for startblock in ("NULL", "0"):
                    for endblock in ("NULL", "0"):
                        opts = (
                            f"on_error_stop := {stop}, "
                            f"check_toast := {check_toast}, "
                            f"skip := {skip}, "
                            f"startblock := {startblock}, "
                            f"endblock := {endblock}"
                        )
                        detects_no_corruption(
                            session, f"verify_heapam('{relname}', {opts})"
                        )


def test_001_verify_heapam(create_pg):
    #
    # Test set-up
    #
    node = create_pg("test", start=False, initdb_extra=["--no-data-checksums"])
    node.append_conf("autovacuum=off")
    node.start()
    session = node.session()

    session.do("CREATE EXTENSION amcheck")

    #
    # Check a table with data loaded but no corruption, freezing, etc.
    #
    fresh_test_table(session, "test")
    check_all_options_uncorrupted(session, "test")

    #
    # Check a corrupt table
    #
    fresh_test_table(session, "test")
    corrupt_first_page(node, session, "test")
    detects_heap_corruption(session, "verify_heapam('test')")
    detects_heap_corruption(session, "verify_heapam('test', skip := 'all-visible')")
    detects_heap_corruption(session, "verify_heapam('test', skip := 'all-frozen')")
    detects_heap_corruption(session, "verify_heapam('test', check_toast := false)")
    detects_heap_corruption(
        session, "verify_heapam('test', startblock := 0, endblock := 0)"
    )

    #
    # Check a corrupt table with all-frozen data
    #
    fresh_test_table(session, "test")
    session.do("VACUUM (FREEZE, DISABLE_PAGE_SKIPPING) test")
    detects_no_corruption(session, "verify_heapam('test')")
    corrupt_first_page(node, session, "test")
    detects_heap_corruption(session, "verify_heapam('test')")
    detects_no_corruption(session, "verify_heapam('test', skip := 'all-frozen')")

    #
    # Check a sequence with no corruption.  The current implementation of
    # sequences doesn't require its own test setup, since sequences are really
    # just heap tables under-the-hood.  To guard against future implementation
    # changes made without remembering to update verify_heapam, we create and
    # exercise a sequence, checking along the way that it passes corruption
    # checks.
    #
    fresh_test_sequence(session, "test_seq")
    check_all_options_uncorrupted(session, "test_seq")
    advance_test_sequence(session, "test_seq")
    check_all_options_uncorrupted(session, "test_seq")
    set_test_sequence(session, "test_seq")
    check_all_options_uncorrupted(session, "test_seq")
    reset_test_sequence(session, "test_seq")
    check_all_options_uncorrupted(session, "test_seq")
