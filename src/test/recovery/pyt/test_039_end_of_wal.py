# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Test detecting end-of-WAL conditions.  This test suite generates
fake defective page and record headers to trigger various failure
scenarios.
"""

import os
import re
import struct
import subprocess
import sys

# Is this a big-endian system ("network" byte order)?  We can't use 'Q' in
# struct calls in a way that's portable across all interpreters, so we
# break 64 bit LSN values into two 'I' values.
# Fortunately we don't need to deal with high values, so we can just write 0
# for the high order 32 bits, but we need to know the endianness to do that.
BIG_ENDIAN = sys.byteorder == "big"


def scan_server_header(bindir, header_path, regexp):
    """Return the first regex match within the installed server header.

    The regex is anchored at the start of each line and the first match's
    capture groups are returned.
    """
    includedir = subprocess.run(
        [os.path.join(bindir, "pg_config"), "--includedir-server"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()

    pattern = re.compile("^" + regexp)
    with open(os.path.join(includedir, header_path), encoding="utf-8") as fh:
        for line in fh:
            m = pattern.match(line)
            if m:
                return list(m.groups())
    raise RuntimeError(f"could not find match in header {header_path}")


# Get GUC value, converted to an int.
def get_int_setting(node, name):
    return int(node.safe_sql(f"SELECT setting FROM pg_settings WHERE name = '{name}'"))


def start_of_page(lsn, wal_block_size):
    return lsn & ~(wal_block_size - 1)


def start_of_next_page(lsn, wal_block_size):
    return start_of_page(lsn, wal_block_size) + wal_block_size


# Build a fake WAL record header based on the data given by the caller.
# This needs to follow the format of the C structure XLogRecord.  To
# be inserted with write_wal().
def build_record_header(
    xl_tot_len, xl_xid=0, xl_prev=0, xl_info=0, xl_rmid=0, xl_crc=0
):
    # This needs to follow the structure XLogRecord:
    # I for xl_tot_len
    # I for xl_xid
    # II for xl_prev
    # C for xl_info
    # C for xl_rmid
    # BB for two bytes of padding
    # I for xl_crc
    return struct.pack(
        "<IIIIBBBBI",
        xl_tot_len,
        xl_xid,
        0 if BIG_ENDIAN else xl_prev,
        xl_prev if BIG_ENDIAN else 0,
        xl_info,
        xl_rmid,
        0,
        0,
        xl_crc,
    )


# Build a fake WAL page header, based on the data given by the caller.
# This needs to follow the format of the C structure XLogPageHeaderData.
# To be inserted with write_wal().
def build_page_header(xlp_magic, xlp_info=0, xlp_tli=0, xlp_pageaddr=0, xlp_rem_len=0):
    # This needs to follow the structure XLogPageHeaderData:
    # S for xlp_magic
    # S for xlp_info
    # I for xlp_tli
    # II for xlp_pageaddr
    # I for xlp_rem_len
    return struct.pack(
        "<HHIIII",
        xlp_magic,
        xlp_info,
        xlp_tli,
        0 if BIG_ENDIAN else xlp_pageaddr,
        xlp_pageaddr if BIG_ENDIAN else 0,
        xlp_rem_len,
    )


def test_039_end_of_wal(create_pg, bindir):
    # Fields retrieved from code headers.
    (magic,) = scan_server_header(
        bindir, "access/xlog_internal.h", r"#define\s+XLOG_PAGE_MAGIC\s+(\w+)"
    )
    xlp_page_magic = int(magic, 16)
    (contrecord,) = scan_server_header(
        bindir, "access/xlog_internal.h", r"#define\s+XLP_FIRST_IS_CONTRECORD\s+(\w+)"
    )
    xlp_first_is_contrecord = int(contrecord, 16)

    # Setup a new node.  The configuration chosen here minimizes the number
    # of arbitrary records that could get generated in a cluster.  Enlarging
    # checkpoint_timeout avoids noise with checkpoint activity.  wal_level
    # set to "minimal" avoids random standby snapshot records.  Autovacuum
    # could also trigger randomly, generating random WAL activity of its own.
    node = create_pg("node", start=False)
    node.append_conf(
        """wal_level = minimal
max_wal_senders = 0
autovacuum = off
checkpoint_timeout = '30min'
"""
    )
    node.start()
    node.safe_sql("CREATE TABLE t AS SELECT 42")

    wal_segment_size = get_int_setting(node, "wal_segment_size")
    wal_block_size = get_int_setting(node, "wal_block_size")
    tli = int(node.safe_sql("SELECT timeline_id FROM pg_control_checkpoint();"))

    # Initial LSN may vary across systems due to different catalog contents
    # set up by initdb.  Switch to a new WAL file so all systems start out in
    # the same place.  The first test depends on trailing zeroes on a page
    # with a valid header.
    node.safe_sql("SELECT pg_switch_wal();")

    ###########################################################################
    # Single-page end-of-WAL detection
    ###########################################################################

    # xl_tot_len is 0 (a common case, we hit trailing zeroes).
    node.emit_wal(0)
    end_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    node.stop("immediate")
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid record length at .*: expected at least 24, got 0", log_size
    ), "xl_tot_len zero"

    # xl_tot_len is < 24 (presumably recycled garbage).
    node.emit_wal(0)
    end_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    node.stop("immediate")
    node.write_wal(tli, end_lsn, wal_segment_size, build_record_header(23))
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid record length at .*: expected at least 24, got 23", log_size
    ), "xl_tot_len short"

    # xl_tot_len in final position, not big enough to span into a new page but
    # also not eligible for regular record header validation
    node.emit_wal(0)
    end_lsn = node.advance_wal_to_record_splitting_zone(wal_block_size)
    node.stop("immediate")
    node.write_wal(tli, end_lsn, wal_segment_size, build_record_header(1))
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid record length at .*: expected at least 24, got 1", log_size
    ), "xl_tot_len short at end-of-page"

    # Need more pages, but xl_prev check fails first.
    node.emit_wal(0)
    end_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 0, 0xDEADBEEF),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "record with incorrect prev-link 0/DEADBEEF at .*", log_size
    ), "xl_prev bad"

    # xl_crc check fails.
    node.emit_wal(0)
    node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    end_lsn = node.emit_wal(10)
    node.stop("immediate")
    # Corrupt a byte in that record, breaking its CRC.
    node.write_wal(tli, end_lsn - 8, wal_segment_size, b"!")
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "incorrect resource manager data checksum in record at .*", log_size
    ), "xl_crc bad"

    ###########################################################################
    # Multi-page end-of-WAL detection, header is not split
    ###########################################################################

    # This series of tests requires a valid xl_prev set in the record header
    # written to WAL.

    # Good xl_prev, we hit zero page next (zero magic).
    node.emit_wal(0)
    prev_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    end_lsn = node.emit_wal(0)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 0, prev_lsn),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid magic number 0000 .* LSN .*", log_size
    ), "xlp_magic zero"

    # Good xl_prev, we hit garbage page next (bad magic).
    node.emit_wal(0)
    prev_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    end_lsn = node.emit_wal(0)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 0, prev_lsn),
    )
    node.write_wal(
        tli,
        start_of_next_page(end_lsn, wal_block_size),
        wal_segment_size,
        build_page_header(0xCAFE, 0, 1, 0),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid magic number CAFE .* LSN .*", log_size
    ), "xlp_magic bad"

    # Good xl_prev, we hit typical recycled page (good xlp_magic, bad
    # xlp_pageaddr).
    node.emit_wal(0)
    prev_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    end_lsn = node.emit_wal(0)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 0, prev_lsn),
    )
    node.write_wal(
        tli,
        start_of_next_page(end_lsn, wal_block_size),
        wal_segment_size,
        build_page_header(xlp_page_magic, 0, 1, 0xBAAAAAAD),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "unexpected pageaddr 0/BAAAAAAD in .*, LSN .*,", log_size
    ), "xlp_pageaddr bad"

    # Good xl_prev, xlp_magic, xlp_pageaddr, but bogus xlp_info.
    node.emit_wal(0)
    prev_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    end_lsn = node.emit_wal(0)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 42, prev_lsn),
    )
    node.write_wal(
        tli,
        start_of_next_page(end_lsn, wal_block_size),
        wal_segment_size,
        build_page_header(
            xlp_page_magic, 0x1234, 1, start_of_next_page(end_lsn, wal_block_size)
        ),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid info bits 1234 in .*, LSN .*,", log_size
    ), "xlp_info bad"

    # Good xl_prev, xlp_magic, xlp_pageaddr, but xlp_info doesn't mention
    # continuation record.
    node.emit_wal(0)
    prev_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    end_lsn = node.emit_wal(0)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 42, prev_lsn),
    )
    node.write_wal(
        tli,
        start_of_next_page(end_lsn, wal_block_size),
        wal_segment_size,
        build_page_header(
            xlp_page_magic, 0, 1, start_of_next_page(end_lsn, wal_block_size)
        ),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "there is no contrecord flag at .*", log_size
    ), "xlp_info lacks XLP_FIRST_IS_CONTRECORD"

    # Good xl_prev, xlp_magic, xlp_pageaddr, xlp_info but xlp_rem_len doesn't
    # add up.
    node.emit_wal(0)
    prev_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    end_lsn = node.emit_wal(0)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 42, prev_lsn),
    )
    node.write_wal(
        tli,
        start_of_next_page(end_lsn, wal_block_size),
        wal_segment_size,
        build_page_header(
            xlp_page_magic,
            xlp_first_is_contrecord,
            1,
            start_of_next_page(end_lsn, wal_block_size),
            123456,
        ),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid contrecord length 123456 .* at .*", log_size
    ), "xlp_rem_len bad"

    ###########################################################################
    # Multi-page, but header is split, so page checks are done first
    ###########################################################################

    # xl_prev is bad and xl_tot_len is too big, but we'll check xlp_magic
    # first.
    node.emit_wal(0)
    end_lsn = node.advance_wal_to_record_splitting_zone(wal_block_size)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 0, 0xDEADBEEF),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid magic number 0000 .* LSN .*", log_size
    ), "xlp_magic zero (split record header)"

    # And we'll also check xlp_pageaddr before any header checks.
    node.emit_wal(0)
    end_lsn = node.advance_wal_to_record_splitting_zone(wal_block_size)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 0, 0xDEADBEEF),
    )
    node.write_wal(
        tli,
        start_of_next_page(end_lsn, wal_block_size),
        wal_segment_size,
        build_page_header(xlp_page_magic, xlp_first_is_contrecord, 1, 0xBAAAAAAD),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "unexpected pageaddr 0/BAAAAAAD in .*, LSN .*,", log_size
    ), "xlp_pageaddr bad (split record header)"

    # We'll also discover that xlp_rem_len doesn't add up before any
    # header checks,
    node.emit_wal(0)
    end_lsn = node.advance_wal_to_record_splitting_zone(wal_block_size)
    node.stop("immediate")
    node.write_wal(
        tli,
        end_lsn,
        wal_segment_size,
        build_record_header(2 * 1024 * 1024 * 1024, 0, 0xDEADBEEF),
    )
    node.write_wal(
        tli,
        start_of_next_page(end_lsn, wal_block_size),
        wal_segment_size,
        build_page_header(
            xlp_page_magic,
            xlp_first_is_contrecord,
            1,
            start_of_next_page(end_lsn, wal_block_size),
            123456,
        ),
    )
    log_size = node.log_position()
    node.start()
    assert node.log_contains(
        "invalid contrecord length 123456 .* at .*", log_size
    ), "xlp_rem_len bad (split record header)"
