# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Tests that pg_amcheck (verify_heapam) detects specific heap page corruption."""

import os
import re
import struct

# This regression test demonstrates that the pg_amcheck binary correctly
# identifies specific kinds of corruption within pages.  To test this, we need
# a mechanism to create corrupt pages with predictable, repeatable corruption.
# The postgres backend cannot be expected to help us with this, as its design
# is not consistent with the goal of intentionally corrupting pages.
#
# Instead, we create a table to corrupt, and with careful consideration of how
# postgresql lays out heap pages, we seek to offsets within the page and
# overwrite deliberately chosen bytes with specific values calculated to
# corrupt the page in expected ways.  We then verify that pg_amcheck reports
# the corruption, and that it runs without crashing.  Note that the backend
# cannot simply be started to run queries against the corrupt table, as the
# backend will crash, at least for some of the corruption types we generate.
#
# Autovacuum potentially touching the table in the background makes the exact
# behavior of this test harder to reason about.  We turn it off to keep things
# simpler.  We use a "belt and suspenders" approach, turning it off for the
# system generally in postgresql.conf, and turning it off specifically for the
# test table.
#
# This test depends on the table being written to the heap file exactly as we
# expect it to be, so we take care to arrange the columns of the table, and
# insert rows of the table, that give predictable sizes and locations within
# the table page.
#
# The HeapTupleHeaderData has 23 bytes of fixed size fields before the variable
# length t_bits[] array.  We have exactly 3 columns in the table, so natts = 3,
# t_bits is 1 byte long, and t_hoff = MAXALIGN(23 + 1) = 24.
#
# We're not too fussy about which datatypes we use for the test, but we do care
# about some specific properties.  We'd like to test both fixed size and
# varlena types.  We'd like some varlena data inline and some toasted.  And
# we'd like the layout of the table such that the datums land at predictable
# offsets within the tuple.  We choose a structure without padding on all
# supported architectures:
#
#	a BIGINT
#	b TEXT
#	c TEXT
#
# We always insert a 7-ascii character string into field 'b', which with a
# 1-byte varlena header gives an 8 byte inline value.  We always insert a long
# text string in field 'c', long enough to force toast storage.
#
# We choose to read and write binary copies of our table's tuples, using the
# struct module.  The layout diagram below uses a shorthand notation in which:
#
#	l = "signed 32-bit Long",
#	L = "Unsigned 32-bit Long",
#	S = "Unsigned 16-bit Short",
#	C = "Unsigned 8-bit Octet",
#
# Each tuple in our table has a layout as follows:
#
#    xx xx xx xx            t_xmin: xxxx		offset = 0		L
#    xx xx xx xx            t_xmax: xxxx		offset = 4		L
#    xx xx xx xx          t_field3: xxxx		offset = 8		L
#    xx xx                   bi_hi: xx			offset = 12		S
#    xx xx                   bi_lo: xx			offset = 14		S
#    xx xx                ip_posid: xx			offset = 16		S
#    xx xx             t_infomask2: xx			offset = 18		S
#    xx xx              t_infomask: xx			offset = 20		S
#    xx                     t_hoff: x			offset = 22		C
#    xx                     t_bits: x			offset = 23		C
#    xx xx xx xx xx xx xx xx   'a': xxxxxxxx	offset = 24		LL
#    xx xx xx xx xx xx xx xx   'b': xxxxxxxx	offset = 32		CCCCCCCC
#    xx xx xx xx xx xx xx xx   'c': xxxxxxxx	offset = 40		CCllLL
#    xx xx xx xx xx xx xx xx      : xxxxxxxx	 ...continued
#    xx xx                        : xx			 ...continued
#
# We could choose to read and write columns 'b' and 'c' in other ways, but
# it is convenient enough to do it this way.  We define packing code
# constants here, where they can be compared easily against the layout.
#
# The struct format string uses native byte order ("=") so that endianness
# matches the platform under test, and "=" suppresses alignment padding.

HEAPTUPLE_PACK_CODE = "=LLLHHHHHBBLLBBBBBBBBBBllLL"
HEAPTUPLE_PACK_LENGTH = 58  # Total size

# The field names corresponding (in order) to the HEAPTUPLE_PACK_CODE entries.
_TUP_FIELDS = [
    "t_xmin",
    "t_xmax",
    "t_field3",
    "bi_hi",
    "bi_lo",
    "ip_posid",
    "t_infomask2",
    "t_infomask",
    "t_hoff",
    "t_bits",
    "a_1",
    "a_2",
    "b_header",
    "b_body1",
    "b_body2",
    "b_body3",
    "b_body4",
    "b_body5",
    "b_body6",
    "b_body7",
    "c_va_header",
    "c_va_vartag",
    "c_va_rawsize",
    "c_va_extinfo",
    "c_va_valueid",
    "c_va_toastrelid",
]


def read_tuple(fh, offset):
    """Read a tuple of our table from a heap page.

    Takes an open file handle to the heap file, and the offset of the tuple.

    Rather than returning the binary data from the file, unpacks the data into
    a dict with named fields.  These fields exactly match the ones understood
    by write_tuple(), below.
    """
    fh.seek(offset, 0)
    buffer = fh.read(HEAPTUPLE_PACK_LENGTH)
    assert len(buffer) == HEAPTUPLE_PACK_LENGTH, "read failed"
    values = struct.unpack(HEAPTUPLE_PACK_CODE, buffer)
    tup = dict(zip(_TUP_FIELDS, values))
    # Stitch together the text for column 'b'
    tup["b"] = "".join(chr(tup["b_body%d" % i]) for i in range(1, 8))
    return tup


def write_tuple(fh, offset, tup):
    """Write a tuple of our table to a heap page.

    Takes an open file handle to the heap file, the offset of the tuple, and a
    dict with the tuple values, as returned by read_tuple().  Writes the tuple
    fields from the dict into the heap file.

    The purpose of this function is to write a tuple back to disk with some
    subset of fields modified.  The function does no error checking.  Use
    cautiously.
    """
    buffer = struct.pack(
        HEAPTUPLE_PACK_CODE,
        tup["t_xmin"],
        tup["t_xmax"],
        tup["t_field3"],
        tup["bi_hi"],
        tup["bi_lo"],
        tup["ip_posid"],
        tup["t_infomask2"],
        tup["t_infomask"],
        tup["t_hoff"],
        tup["t_bits"],
        tup["a_1"],
        tup["a_2"],
        tup["b_header"],
        tup["b_body1"],
        tup["b_body2"],
        tup["b_body3"],
        tup["b_body4"],
        tup["b_body5"],
        tup["b_body6"],
        tup["b_body7"],
        tup["c_va_header"],
        tup["c_va_vartag"],
        tup["c_va_rawsize"],
        tup["c_va_extinfo"],
        tup["c_va_valueid"],
        tup["c_va_toastrelid"],
    )
    fh.seek(offset, 0)
    fh.write(buffer)


# Some #define constants from access/htup_details.h for use while corrupting.
HEAP_HASNULL = 0x0001
HEAP_XMAX_LOCK_ONLY = 0x0080
HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMIN_INVALID = 0x0200
HEAP_XMAX_COMMITTED = 0x0400
HEAP_XMAX_INVALID = 0x0800
HEAP_NATTS_MASK = 0x07FF
HEAP_XMAX_IS_MULTI = 0x1000
HEAP_KEYS_UPDATED = 0x2000
HEAP_HOT_UPDATED = 0x4000
HEAP_ONLY_TUPLE = 0x8000
HEAP_UPDATED = 0x2000

# 16-bit fields must wrap when we OR/clear bits (struct "H" requires 0..65535).
_U16_MASK = 0xFFFF


def header(blkno=None, offnum=None, attnum=None):
    """Generate a regex string matching the header we expect verify_heapam()
    to return given which fields we expect to be non-null."""
    if attnum is not None:
        return (
            r'heap table "postgres\.public\.test", block %d, offset %d, '
            r"attribute %d:\s+" % (blkno, offnum, attnum)
        )
    if offnum is not None:
        return (
            r'heap table "postgres\.public\.test", block %d, offset %d:\s+'
            % (blkno, offnum)
        )
    if blkno is not None:
        return r'heap table "postgres\.public\.test", block %d:\s+' % blkno
    return r'heap table "postgres\.public\.test":\s+'


def test_004_verify_heapam(create_pg):
    # Set up the node.  Once we create and corrupt the table,
    # autovacuum workers visiting the table could crash the backend.
    # Disable autovacuum so that won't happen.
    node = create_pg("test", initdb_extra=["--no-data-checksums"])
    node.append_conf("autovacuum=off")
    node.append_conf("max_prepared_transactions=10")

    # The node is already started; reload the configuration we appended.
    node.restart()

    # Start the node and load the extensions.  We depend on both
    # amcheck and pageinspect for this test.
    port = node.port
    pgdata = node.data_dir
    session = node.session()
    session.do("CREATE EXTENSION amcheck")
    session.do("CREATE EXTENSION pageinspect")

    # Get a non-zero datfrozenxid
    session.do("VACUUM FREEZE")

    # Create the test table with precisely the schema that our corruption
    # function expects.
    session.do(
        """
            CREATE TABLE public.test (a BIGINT, b TEXT, c TEXT);
            ALTER TABLE public.test SET (autovacuum_enabled=false);
            ALTER TABLE public.test ALTER COLUMN c SET STORAGE EXTERNAL;
            CREATE INDEX test_idx ON public.test(a, b);
        """
    )

    # We want (0 < datfrozenxid < test.relfrozenxid).  To achieve this, we
    # freeze an otherwise unused table, public.junk, prior to inserting data
    # and freezing public.test
    session.do(
        """
            CREATE TABLE public.junk AS SELECT 'junk'::TEXT AS junk_column;
            ALTER TABLE public.junk SET (autovacuum_enabled=false);
        """,
        "VACUUM FREEZE public.junk",
    )

    rel = session.query_oneval("SELECT pg_relation_filepath('public.test')")
    relpath = os.path.join(pgdata, rel)

    # Initial setup for the public.test table.
    # ROWCOUNT is the total number of rows that we expect to insert into the
    # page.  ROWCOUNT_BASIC is the number of those rows that are related to
    # basic tuple validation, rather than update chain validation.
    ROWCOUNT = 44
    ROWCOUNT_BASIC = 16

    # First insert data needed for tests unrelated to update chain validation.
    # Then freeze the page. These tuples are at offset numbers 1 to 16.
    session.do(
        """
        INSERT INTO public.test (a, b, c)
            SELECT
                x'DEADF9F9DEADF9F9'::bigint,
                'abcdefg',
                repeat('w', 10000)
        FROM generate_series(1, %d);
    """
        % ROWCOUNT_BASIC,
        "VACUUM FREEZE public.test",
    )

    # Create some simple HOT update chains for line pointer validation. After
    # the page is HOT pruned, we'll have two redirects line pointers each
    # pointing to a tuple. We'll then change the second redirect to point to
    # the same tuple as the first one and verify that we can detect corruption.
    session.do(
        """
            INSERT INTO public.test (a, b, c)
                VALUES ( x'DEADF9F9DEADF9F9'::bigint, 'abcdefg',
                         generate_series(1,2)); -- offset numbers 17 and 18
            UPDATE public.test SET c = 'a' WHERE c = '1'; -- offset number 19
            UPDATE public.test SET c = 'a' WHERE c = '2'; -- offset number 20
        """
    )

    # Create some more HOT update chains.
    session.do(
        """
            INSERT INTO public.test (a, b, c)
                VALUES ( x'DEADF9F9DEADF9F9'::bigint, 'abcdefg',
                         generate_series(3,6)); -- offset numbers 21 through 24
            UPDATE public.test SET c = 'a' WHERE c = '3'; -- offset number 25
            UPDATE public.test SET c = 'a' WHERE c = '4'; -- offset number 26
        """
    )

    # Negative test case of HOT-pruning with aborted tuple.
    session.do(
        """
            BEGIN;
            UPDATE public.test SET c = 'a' WHERE c = '5'; -- offset number 27
            ABORT;
       """,
        "VACUUM FREEZE public.test;",
    )

    # Next update on any tuple will be stored at the same place of tuple
    # inserted by aborted transaction. This should not cause the table to
    # appear corrupt.
    session.do(
        """
        BEGIN;
            UPDATE public.test SET c = 'a' WHERE c = '6'; -- offset number 27 again
        COMMIT;
        """,
        "VACUUM FREEZE public.test;",
    )

    # Data for HOT chain validation, so not calling VACUUM FREEZE.
    session.do(
        """
        BEGIN;
            INSERT INTO public.test (a, b, c)
                VALUES ( x'DEADF9F9DEADF9F9'::bigint, 'abcdefg',
                         generate_series(7,15)); -- offset numbers 28 to 36
            UPDATE public.test SET c = 'a' WHERE c = '7'; -- offset number 37
            UPDATE public.test SET c = 'a' WHERE c = '10'; -- offset number 38
            UPDATE public.test SET c = 'a' WHERE c = '11'; -- offset number 39
            UPDATE public.test SET c = 'a' WHERE c = '12'; -- offset number 40
            UPDATE public.test SET c = 'a' WHERE c = '13'; -- offset number 41
            UPDATE public.test SET c = 'a' WHERE c = '14'; -- offset number 42
            UPDATE public.test SET c = 'a' WHERE c = '15'; -- offset number 43
        COMMIT;
        """
    )

    # Need one aborted transaction to test corruption in HOT chains.
    session.do(
        """
            BEGIN;
            UPDATE public.test SET c = 'a' WHERE c = '9'; -- offset number 44
            ABORT;
        """
    )

    # Need one in-progress transaction to test few corruption in HOT chains.
    # We are creating PREPARE TRANSACTION here as these will not be aborted
    # even if we stop the node.
    session.do(
        """
            BEGIN;
            PREPARE TRANSACTION 'in_progress_tx';
        """
    )
    in_progress_xid = session.query_oneval(
        """
            SELECT transaction FROM pg_prepared_xacts;
        """
    )

    relfrozenxid = session.query_oneval(
        "select relfrozenxid from pg_class where relname = 'test'"
    )
    datfrozenxid = session.query_oneval(
        "select datfrozenxid from pg_database where datname = 'postgres'"
    )

    relfrozenxid = int(relfrozenxid)
    datfrozenxid = int(datfrozenxid)

    # Sanity check that our 'test' table has a relfrozenxid newer than the
    # datfrozenxid for the database, and that the datfrozenxid is greater than
    # the first normal xid.  We rely on these invariants in some of our tests.
    assert not (datfrozenxid <= 3 or datfrozenxid >= relfrozenxid), (
        "Xid thresholds not as expected: got datfrozenxid = %d, "
        "relfrozenxid = %d" % (datfrozenxid, relfrozenxid)
    )

    # Find where each of the tuples is located on the page. If a particular
    # line pointer is a redirect rather than a tuple, we record the offset as
    # -1.
    lp_off_res = session.query(
        """
            SELECT CASE WHEN lp_flags = 2 THEN -1 ELSE lp_off END
            FROM heap_page_items(get_raw_page('test', 'main', 0))
        """
    )
    lp_off = [int(row[0]) for row in lp_off_res.rows]

    assert len(lp_off) == ROWCOUNT, "row offset counts mismatch"

    # Sanity check that our 'test' table on disk layout matches expectations.
    # If this is not so, we will have to skip the test until somebody updates
    # the test to work on this platform.
    session.close()
    node.stop()

    ENDIANNESS = None
    with open(relpath, "r+b") as file:
        for tupidx in range(ROWCOUNT):
            offset = lp_off[tupidx]
            if offset == -1:
                continue  # ignore redirect line pointers
            tup = read_tuple(file, offset)

            # Sanity-check that the data appears on the page where we expect.
            a_1 = tup["a_1"]
            a_2 = tup["a_2"]
            b = tup["b"]
            assert a_1 == 0xDEADF9F9 and a_2 == 0xDEADF9F9 and b == "abcdefg", (
                "Page layout of index %d differs from our expectations: "
                'expected (%x, %x, "%s"), got (%x, %x, "%s")'
                % (
                    tupidx,
                    0xDEADF9F9,
                    0xDEADF9F9,
                    "abcdefg",
                    a_1,
                    a_2,
                    re.sub(
                        r"(\W)",
                        lambda m: "\\x%02x" % ord(m.group(1)),
                        b,
                    ),
                )
            )

            # Determine endianness of current platform from the 1-byte varlena
            # header
            ENDIANNESS = "little" if tup["b_header"] == 0x11 else "big"

    node.start()

    # Ok, Xids and page layout look ok.  We can run corruption tests.

    # Check that pg_amcheck runs against the uncorrupted table without error.
    node.command_ok(
        ["pg_amcheck", "--port", str(port), "postgres"],
        "pg_amcheck test table, prior to corruption",
    )

    # Check that pg_amcheck runs against the uncorrupted table and index
    # without error.
    node.command_ok(
        ["pg_amcheck", "--port", str(port), "postgres"],
        "pg_amcheck test table and index, prior to corruption",
    )

    node.stop()

    # Saved values used to corrupt later tuples relative to earlier ones.
    pred_xmax = None
    pred_posid = None
    aborted_xid = None

    # Corrupt the tuples, one type of corruption per tuple.  Some types of
    # corruption cause verify_heapam to skip to the next tuple without
    # performing any remaining checks, so we can't exercise the system properly
    # if we focus all our corruption on a single tuple.
    expected = []
    with open(relpath, "r+b") as file:
        for tupidx in range(ROWCOUNT):
            offnum = tupidx + 1  # offnum is 1-based, not zero-based
            offset = lp_off[tupidx]
            hdr = header(0, offnum)

            # Read tuple, if there is one.
            tup = None if offset == -1 else read_tuple(file, offset)

            if offnum == 1:
                # Corruptly set xmin < relfrozenxid
                xmin = relfrozenxid - 1
                tup["t_xmin"] = xmin
                tup["t_infomask"] &= ~HEAP_XMIN_COMMITTED & _U16_MASK
                tup["t_infomask"] &= ~HEAP_XMIN_INVALID & _U16_MASK

                # Expected corruption report
                expected.append(
                    re.compile(
                        hdr
                        + r"xmin %d precedes relation freeze threshold 0:\d+"
                        % xmin
                    )
                )
            elif offnum == 2:
                # Corruptly set xmin < datfrozenxid
                xmin = 3
                tup["t_xmin"] = xmin
                tup["t_infomask"] &= ~HEAP_XMIN_COMMITTED & _U16_MASK
                tup["t_infomask"] &= ~HEAP_XMIN_INVALID & _U16_MASK

                expected.append(
                    re.compile(
                        hdr
                        + r"xmin %d precedes oldest valid transaction ID 0:\d+"
                        % xmin
                    )
                )
            elif offnum == 3:
                # Corruptly set xmin < datfrozenxid, further back, noting
                # circularity of xid comparison.
                xmin = 4026531839
                tup["t_xmin"] = xmin
                tup["t_infomask"] &= ~HEAP_XMIN_COMMITTED & _U16_MASK
                tup["t_infomask"] &= ~HEAP_XMIN_INVALID & _U16_MASK

                expected.append(
                    re.compile(
                        hdr
                        + r"xmin %d precedes oldest valid transaction ID 0:\d+"
                        % xmin
                    )
                )
            elif offnum == 4:
                # Corruptly set xmax < relminmxid;
                xmax = 4026531839
                tup["t_xmax"] = xmax
                tup["t_infomask"] &= ~HEAP_XMAX_INVALID & _U16_MASK

                expected.append(
                    re.compile(
                        hdr
                        + r"xmax %d precedes oldest valid transaction ID 0:\d+"
                        % xmax
                    )
                )
            elif offnum == 5:
                # Corrupt the tuple t_hoff, but keep it aligned properly
                tup["t_hoff"] += 128

                expected.append(
                    re.compile(
                        hdr + r"data begins at offset 152 beyond the tuple length 58"
                    )
                )
                expected.append(
                    re.compile(
                        hdr
                        + r"tuple data should begin at byte 24, but actually "
                        r"begins at byte 152 \(3 attributes, no nulls\)"
                    )
                )
            elif offnum == 6:
                # Corrupt the tuple t_hoff, wrong alignment
                tup["t_hoff"] += 3

                expected.append(
                    re.compile(
                        hdr
                        + r"tuple data should begin at byte 24, but actually "
                        r"begins at byte 27 \(3 attributes, no nulls\)"
                    )
                )
            elif offnum == 7:
                # Corrupt the tuple t_hoff, underflow but correct alignment
                tup["t_hoff"] -= 8

                expected.append(
                    re.compile(
                        hdr
                        + r"tuple data should begin at byte 24, but actually "
                        r"begins at byte 16 \(3 attributes, no nulls\)"
                    )
                )
            elif offnum == 8:
                # Corrupt the tuple t_hoff, underflow and wrong alignment
                tup["t_hoff"] -= 3

                expected.append(
                    re.compile(
                        hdr
                        + r"tuple data should begin at byte 24, but actually "
                        r"begins at byte 21 \(3 attributes, no nulls\)"
                    )
                )
            elif offnum == 9:
                # Corrupt the tuple to look like it has lots of attributes, not
                # just 3
                tup["t_infomask2"] |= HEAP_NATTS_MASK

                expected.append(
                    re.compile(
                        hdr
                        + r"number of attributes 2047 exceeds maximum 3 "
                        r"expected for table"
                    )
                )
            elif offnum == 10:
                # Corrupt the tuple to look like it has lots of attributes,
                # some of them null.  This falsely creates the impression that
                # the t_bits array is longer than just one byte, but t_hoff
                # still says otherwise.
                tup["t_infomask"] |= HEAP_HASNULL
                tup["t_infomask2"] |= HEAP_NATTS_MASK
                tup["t_bits"] = 0xAA

                expected.append(
                    re.compile(
                        hdr
                        + r"tuple data should begin at byte 280, but actually "
                        r"begins at byte 24 \(2047 attributes, has nulls\)"
                    )
                )
            elif offnum == 11:
                # Same as above, but this time t_hoff plays along
                tup["t_infomask"] |= HEAP_HASNULL
                tup["t_infomask2"] |= HEAP_NATTS_MASK & 0x40
                tup["t_bits"] = 0xAA
                tup["t_hoff"] = 32

                expected.append(
                    re.compile(
                        hdr
                        + r"number of attributes 67 exceeds maximum 3 "
                        r"expected for table"
                    )
                )
            elif offnum == 12:
                # Overwrite column 'b' 1-byte varlena header and initial
                # characters to look like a long 4-byte varlena
                #
                # On little endian machines, bytes ending in two zero bits
                # (xxxxxx00 bytes) are 4-byte length word, aligned,
                # uncompressed data (up to 1G).  We set the high six bits to
                # 111111 and the lower two bits to 00, then the next three bytes
                # with 0xFF using 0xFCFFFFFF.
                #
                # On big endian machines, bytes starting in two zero bits
                # (00xxxxxx bytes) are 4-byte length word, aligned,
                # uncompressed data (up to 1G).  We set the low six bits to
                # 111111 and the high two bits to 00, then the next three bytes
                # with 0xFF using 0x3FFFFFFF.
                tup["b_header"] = 0xFC if ENDIANNESS == "little" else 0x3F
                tup["b_body1"] = 0xFF
                tup["b_body2"] = 0xFF
                tup["b_body3"] = 0xFF

                hdr = header(0, offnum, 1)
                expected.append(
                    re.compile(
                        hdr
                        + r"attribute with length \d+ ends at offset \d+ "
                        r"beyond total tuple length \d+"
                    )
                )
            elif offnum == 13:
                # Corrupt the bits in column 'c' toast pointer
                tup["c_va_valueid"] = 0xFFFFFFFF

                hdr = header(0, offnum, 2)
                expected.append(
                    re.compile(hdr + r"toast value \d+ not found in toast table")
                )
            elif offnum == 14:
                # Set both HEAP_XMAX_COMMITTED and HEAP_XMAX_IS_MULTI
                tup["t_infomask"] |= HEAP_XMAX_COMMITTED
                tup["t_infomask"] |= HEAP_XMAX_IS_MULTI
                tup["t_xmax"] = 4

                expected.append(
                    re.compile(
                        hdr
                        + r"multitransaction ID 4 equals or exceeds next valid "
                        r"multitransaction ID 1"
                    )
                )
            elif offnum == 15:
                # Set both HEAP_XMAX_COMMITTED and HEAP_XMAX_IS_MULTI
                tup["t_infomask"] |= HEAP_XMAX_COMMITTED
                tup["t_infomask"] |= HEAP_XMAX_IS_MULTI
                tup["t_xmax"] = 4000000000

                expected.append(
                    re.compile(
                        hdr
                        + r"multitransaction ID 4000000000 precedes relation "
                        r"minimum multitransaction ID threshold 1"
                    )
                )
            elif offnum == 16:  # Last offnum must equal ROWCOUNT
                # Corruptly set xmin > next_xid to be in the future.
                xmin = 123456
                tup["t_xmin"] = xmin
                tup["t_infomask"] &= ~HEAP_XMIN_COMMITTED & _U16_MASK
                tup["t_infomask"] &= ~HEAP_XMIN_INVALID & _U16_MASK

                expected.append(
                    re.compile(
                        hdr
                        + r"xmin %d equals or exceeds next valid transaction "
                        r"ID 0:\d+" % xmin
                    )
                )
            elif offnum == 17:
                # at offnum 19 we will unset HEAP_ONLY_TUPLE flag
                assert tup is None, "offnum %d should be a redirect" % offnum
                expected.append(
                    re.compile(
                        hdr
                        + r"redirected line pointer points to a non-heap-only "
                        r"tuple at offset \d+"
                    )
                )
            elif offnum == 18:
                # rewrite line pointer with lp_off = 17, lp_flags = 2,
                # lp_len = 0.
                assert tup is None, "offnum %d should be a redirect" % offnum
                file.seek(92, 0)
                file.write(
                    struct.pack(
                        "=L",
                        0x00010011 if ENDIANNESS == "little" else 0x00230000,
                    )
                )
                expected.append(
                    re.compile(
                        hdr
                        + r"redirected line pointer points to another "
                        r"redirected line pointer at offset \d+"
                    )
                )
            elif offnum == 19:
                # unset HEAP_ONLY_TUPLE flag, so that update chain validation
                # will complain about offset 17
                tup["t_infomask2"] &= ~HEAP_ONLY_TUPLE & _U16_MASK
            elif offnum == 22:
                # rewrite line pointer with lp.off = 25, lp_flags = 2,
                # lp_len = 0
                file.seek(108, 0)
                file.write(
                    struct.pack(
                        "=L",
                        0x00010019 if ENDIANNESS == "little" else 0x00330000,
                    )
                )
                expected.append(
                    re.compile(
                        hdr
                        + r"redirect line pointer points to offset \d+, but "
                        r"offset \d+ also points there"
                    )
                )
            elif offnum == 28:
                tup["t_infomask2"] &= ~HEAP_HOT_UPDATED & _U16_MASK
                expected.append(
                    re.compile(
                        hdr
                        + r"non-heap-only update produced a heap-only tuple at "
                        r"offset \d+"
                    )
                )

                # Save these values so we can insert them into the tuple at
                # offnum 29.
                pred_xmax = tup["t_xmax"]
                pred_posid = tup["ip_posid"]
            elif offnum == 29:
                # Copy these values from the tuple at offset 28.
                tup["t_xmax"] = pred_xmax
                tup["ip_posid"] = pred_posid
                expected.append(
                    re.compile(
                        hdr
                        + r"tuple points to new version at offset \d+, but "
                        r"offset \d+ also points there"
                    )
                )
            elif offnum == 30:
                # Save xid, so we can insert into into tuple at offset 31.
                aborted_xid = tup["t_xmax"]
            elif offnum == 31:
                # Set xmin to xmax of tuple at offset 30.
                tup["t_xmin"] = aborted_xid
                tup["t_infomask"] &= ~HEAP_XMIN_COMMITTED & _U16_MASK
                expected.append(
                    re.compile(
                        hdr
                        + r"tuple with aborted xmin \d+ was updated to produce "
                        r"a tuple at offset \d+ with committed xmin \d+"
                    )
                )
            elif offnum == 32:
                tup["t_infomask2"] |= HEAP_ONLY_TUPLE
                expected.append(
                    re.compile(
                        hdr
                        + r"tuple is root of chain but is marked as heap-only "
                        r"tuple"
                    )
                )
                expected.append(
                    re.compile(
                        hdr
                        + r"tuple is heap only, but not the result of an update"
                    )
                )
            elif offnum == 33:
                # Tuple at offset 40 is the successor of this one; we'll corrupt
                # it to be non-heap-only.
                expected.append(
                    re.compile(
                        hdr
                        + r"heap-only update produced a non-heap only tuple at "
                        r"offset \d+"
                    )
                )
            elif offnum == 34:
                tup["t_xmax"] = 0
                expected.append(
                    re.compile(hdr + r"tuple has been HOT updated, but xmax is 0")
                )
            elif offnum == 35:
                tup["t_xmin"] = int(in_progress_xid)
                tup["t_infomask"] &= ~HEAP_XMIN_COMMITTED & _U16_MASK
                expected.append(
                    re.compile(
                        hdr
                        + r"tuple with in-progress xmin \d+ was updated to "
                        r"produce a tuple at offset \d+ with committed xmin \d+"
                    )
                )
            elif offnum == 36:
                # Tuple at offset 43 is the successor of this one; we'll corrupt
                # it to have xmin = in_progress_xid. By setting the xmax of this
                # tuple to the same value, we make it look like an update chain
                # with an in-progress XID following a committed one.
                tup["t_xmin"] = aborted_xid
                tup["t_xmax"] = int(in_progress_xid)
                tup["t_infomask"] &= ~HEAP_XMIN_COMMITTED & _U16_MASK
                expected.append(
                    re.compile(
                        hdr
                        + r"tuple with aborted xmin \d+ was updated to produce "
                        r"a tuple at offset \d+ with in-progress xmin \d+"
                    )
                )
            elif offnum == 40:
                # Tuple at offset 33 is the predecessor of this one; the error
                # will be reported there.
                tup["t_infomask2"] &= ~HEAP_ONLY_TUPLE & _U16_MASK
            elif offnum == 43:
                # Tuple at offset 36 is the predecessor of this one; the error
                # will be reported there.
                tup["t_xmin"] = int(in_progress_xid)
                tup["t_infomask"] &= ~HEAP_XMIN_COMMITTED & _U16_MASK
            else:
                # The tests for update chain validation end up creating a bunch
                # of tuples that aren't corrupted in any way e.g. because only
                # one of the two tuples in the update chain needs to be
                # corrupted for the test, or because one update chain is being
                # made to erroneously point into the middle of another that has
                # nothing wrong with it.  In all such cases we need not write
                # the tuple back to the file.
                continue

            if tup is not None:
                write_tuple(file, offset, tup)

    node.start()
    session = node.session()

    # Run pg_amcheck against the corrupt table with epoch=0, comparing actual
    # corruption messages against the expected messages
    node.command_checks_all(
        ["pg_amcheck", "--no-dependent-indexes", "--port", str(port), "postgres"],
        2,
        expected,
        [],
        "Expected corruption message output",
    )
    session.do(
        """
                        COMMIT PREPARED 'in_progress_tx';
        """
    )

    session.close()
    node.stop()
