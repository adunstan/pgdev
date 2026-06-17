# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test amcheck's gin_index_check against corrupted GIN index pages."""

import os
import re
import struct

# to get the split fast, we want tuples to be as large as possible, but
# the same time we don't want them to be toasted.
FILLER_SIZE = 1900


def relation_filepath(node, relname):
    """Return the filesystem path for the named relation."""
    pgdata = node.data_dir
    rel = node.safe_sql(f"SELECT pg_relation_filepath('{relname}')")
    assert rel, f"path not found for relation {relname}"
    return os.path.join(pgdata, rel)


def string_replace_block(filename, find, replace, blkno, blksize):
    """Substitute pattern 'find' with 'replace' within the block 'blkno'.

    *find* is a compiled regular expression (operating on bytes) and
    *replace* is the bytes replacement (which may reference capture
    groups using the standard re backreference syntax).
    """
    offset = blkno * blksize
    with open(filename, "r+b") as fh:
        fh.seek(offset)
        buffer = fh.read(blksize)
        assert (
            len(buffer) == blksize
        ), f"read only {len(buffer)} of {blksize} bytes from {filename}"

        buffer = find.sub(replace, buffer)

        fh.seek(offset)
        fh.write(buffer)


def gin_check_error(node, indexname):
    """Run gin_index_check and return its error message (or "")."""
    res = node.sql(f"SELECT gin_index_check('{indexname}')")
    return res.error_message or ""


def invalid_entry_order_leaf_page_test(node, blksize):
    relname = "test"
    indexname = "test_gin_idx"

    node.safe_sql(
        f"""
        DROP TABLE IF EXISTS {relname};
        CREATE TABLE {relname} (a text[]);
        INSERT INTO {relname} (a) VALUES ('{{aaaaa,bbbbb}}');
        CREATE INDEX {indexname} ON {relname} USING gin (a);
    """
    )
    relpath = relation_filepath(node, indexname)

    node.stop()

    blkno = 1  # root

    # produce wrong order by replacing aaaaa with ccccc
    string_replace_block(relpath, re.compile(b"aaaaa"), b"ccccc", blkno, blksize)

    node.start()

    expected = (
        f'index "{indexname}" has wrong tuple order on entry tree page, '
        "block 1, offset 2, rightlink 4294967295"
    )
    assert re.search(re.escape(expected), gin_check_error(node, indexname))


def invalid_entry_order_inner_page_test(node, blksize):
    relname = "test"
    indexname = "test_gin_idx"

    # to break the order in the inner page we need at least 3 items
    # (rightmost key in the inner level is not checked for the order)
    # so fill table until we have 2 splits
    node.safe_sql(
        f"""
        DROP TABLE IF EXISTS {relname};
        CREATE TABLE {relname} (a text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'pppppppppp' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'qqqqqqqqqq' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'rrrrrrrrrr' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'ssssssssss' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'tttttttttt' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'uuuuuuuuuu' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'vvvvvvvvvv' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'wwwwwwwwww' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        CREATE INDEX {indexname} ON {relname} USING gin (a);
    """
    )
    relpath = relation_filepath(node, indexname)

    node.stop()

    blkno = 1  # root

    # we have rrrrrrrrr... and tttttttttt... as keys in the root, so produce
    # wrong order by replacing rrrrrrrrrr....
    string_replace_block(
        relpath, re.compile(b"rrrrrrrrrr"), b"zzzzzzzzzz", blkno, blksize
    )

    node.start()

    expected = (
        f'index "{indexname}" has wrong tuple order on entry tree page, '
        "block 1, offset 2, rightlink 4294967295"
    )
    assert re.search(re.escape(expected), gin_check_error(node, indexname))


def invalid_entry_columns_order_test(node, blksize):
    relname = "test"
    indexname = "test_gin_idx"

    node.safe_sql(
        f"""
        DROP TABLE IF EXISTS {relname};
        CREATE TABLE {relname} (a text[],b text[]);
        INSERT INTO {relname} (a,b) VALUES ('{{aaa}}','{{bbb}}');
        CREATE INDEX {indexname} ON {relname} USING gin (a,b);
    """
    )
    relpath = relation_filepath(node, indexname)

    node.stop()

    blkno = 1  # root

    # mess column numbers
    # root items order before: (1,aaa), (2,bbb)
    # root items order after:  (2,aaa), (1,bbb)
    attrno_1 = struct.pack("h", 1)
    attrno_2 = struct.pack("h", 2)

    find = re.compile(re.escape(attrno_1) + b"(.)(aaa)", re.DOTALL)
    replace = attrno_2 + rb"\1\2"
    string_replace_block(relpath, find, replace, blkno, blksize)

    find = re.compile(re.escape(attrno_2) + b"(.)(bbb)", re.DOTALL)
    replace = attrno_1 + rb"\1\2"
    string_replace_block(relpath, find, replace, blkno, blksize)

    node.start()

    expected = (
        f'index "{indexname}" has wrong tuple order on entry tree page, '
        "block 1, offset 2, rightlink 4294967295"
    )
    assert re.search(re.escape(expected), gin_check_error(node, indexname))


def inconsistent_with_parent_key__parent_key_corrupted_test(node, blksize):
    relname = "test"
    indexname = "test_gin_idx"

    # fill the table until we have a split
    node.safe_sql(
        f"""
        DROP TABLE IF EXISTS {relname};
        CREATE TABLE {relname} (a text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'llllllllll' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'mmmmmmmmmm' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'nnnnnnnnnn' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'xxxxxxxxxx' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'yyyyyyyyyy' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        CREATE INDEX {indexname} ON {relname} USING gin (a);
    """
    )
    relpath = relation_filepath(node, indexname)

    node.stop()

    blkno = 1  # root

    # we have nnnnnnnnnn... as parent key in the root, so replace it with
    # something smaller then child's keys
    string_replace_block(
        relpath, re.compile(b"nnnnnnnnnn"), b"aaaaaaaaaa", blkno, blksize
    )

    node.start()

    expected = f'index "{indexname}" has inconsistent records on page 3 offset 3'
    assert re.search(re.escape(expected), gin_check_error(node, indexname))


def inconsistent_with_parent_key__child_key_corrupted_test(node, blksize):
    relname = "test"
    indexname = "test_gin_idx"

    # fill the table until we have a split
    node.safe_sql(
        f"""
        DROP TABLE IF EXISTS {relname};
        CREATE TABLE {relname} (a text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'llllllllll' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'mmmmmmmmmm' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'nnnnnnnnnn' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'xxxxxxxxxx' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        INSERT INTO {relname} (a) VALUES (('{{' || 'yyyyyyyyyy' || random_string({FILLER_SIZE}) ||'}}')::text[]);
        CREATE INDEX {indexname} ON {relname} USING gin (a);
    """
    )
    relpath = relation_filepath(node, indexname)

    node.stop()

    blkno = 3  # leaf

    # we have nnnnnnnnnn... as parent key in the root, so replace child key
    # with something bigger
    string_replace_block(
        relpath, re.compile(b"nnnnnnnnnn"), b"pppppppppp", blkno, blksize
    )

    node.start()

    expected = f'index "{indexname}" has inconsistent records on page 3 offset 3'
    assert re.search(re.escape(expected), gin_check_error(node, indexname))


def inconsistent_with_parent_key__parent_key_corrupted_posting_tree_test(node, blksize):
    relname = "test"
    indexname = "test_gin_idx"

    node.safe_sql(
        f"""
        DROP TABLE IF EXISTS {relname};
        CREATE TABLE {relname} (a text[]);
        INSERT INTO {relname} (a) select ('{{aaaaa}}') from generate_series(1,10000);
        CREATE INDEX {indexname} ON {relname} USING gin (a);
    """
    )
    relpath = relation_filepath(node, indexname)

    node.stop()

    blkno = 2  # posting tree root

    # we have a posting tree for 'aaaaa' key with the root at 2nd block
    # and two leaf pages 3 and 4. replace 4th page's high key with (1,1)
    # so that there are tid's in leaf page that are larger then the new high key.
    find = re.compile(re.escape(struct.pack("HHH", 0, 4, 0)) + b"....", re.DOTALL)
    replace = struct.pack("HHHHH", 0, 4, 0, 1, 1)
    string_replace_block(relpath, find, replace, blkno, blksize)

    node.start()

    expected = (
        f'index "{indexname}": tid exceeds parent\'s high key in '
        "postingTree leaf on block 4"
    )
    assert re.search(re.escape(expected), gin_check_error(node, indexname))


def test_006_verify_gin(create_pg):
    # Test set-up
    node = create_pg("test", start=False, initdb_extra=["--no-data-checksums"])
    node.append_conf("autovacuum=off")
    node.start()
    blksize = int(node.safe_sql("SHOW block_size;"))
    node.safe_sql("CREATE EXTENSION amcheck")
    node.safe_sql(
        """
        CREATE OR REPLACE FUNCTION  random_string( INT ) RETURNS text AS $$
        SELECT string_agg(substring('0123456789abcdefghijklmnopqrstuvwxyz', ceil(random() * 36)::integer, 1), '') from generate_series(1, $1);
        $$ LANGUAGE SQL;"""
    )

    # Tests
    invalid_entry_order_leaf_page_test(node, blksize)
    invalid_entry_order_inner_page_test(node, blksize)
    invalid_entry_columns_order_test(node, blksize)
    inconsistent_with_parent_key__parent_key_corrupted_test(node, blksize)
    inconsistent_with_parent_key__child_key_corrupted_test(node, blksize)
    inconsistent_with_parent_key__parent_key_corrupted_posting_tree_test(node, blksize)
