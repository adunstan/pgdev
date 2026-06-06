# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Test multixact wraparound."""

import os
import re


def test_002_multixact_wraparound(create_pg):
    node = create_pg("main", start=False)
    node.append_conf("shared_preload_libraries = 'test_slru'")

    # Set the cluster's next multitransaction close to wraparound
    node_pgdata = node.data_dir
    node.command_ok(
        [
            "pg_resetwal",
            "--multixact-ids",
            "0xFFFFFFF8,0xFFFFFFF8",
            node_pgdata,
        ],
        "set the cluster's next multitransaction to 0xFFFFFFF8",
    )

    # Extract a few values from pg_resetwal --dry-run output that we need for
    # the calculations below
    out = node.pg_bin.result(["pg_resetwal", "--dry-run", node.data_dir]).stdout
    m = re.search(r"^Database block size: *(\d+)$", out, re.MULTILINE)
    assert m, out
    blcksz = int(m.group(1))
    m = re.search(r"^Pages per SLRU segment: *(\d+)$", out, re.MULTILINE)
    assert m, out
    slru_pages_per_segment = int(m.group(1))

    # Fixup the SLRU files to match the state we reset to.

    # initialize the 'offsets' SLRU file containing the new next multixid
    # with zeros
    multixact_offsets_per_page = blcksz // 8  # sizeof(MultiXactOffset) == 8
    segno = int(0xFFFFFFF8 / multixact_offsets_per_page / slru_pages_per_segment)
    slru_file = os.path.join(node_pgdata, "pg_multixact", "offsets", "%04X" % segno)
    bytes_per_seg = slru_pages_per_segment * blcksz
    with open(slru_file, "wb") as fh:
        written = fh.write(b"\0" * bytes_per_seg)
    assert written == bytes_per_seg, f'could not write to "{slru_file}"'

    # remove old file
    os.unlink(os.path.join(node_pgdata, "pg_multixact", "offsets", "0000"))

    # Consume multixids to wrap around.  We start at 0xFFFFFFF8, so after
    # creating 16 multixacts we should definitely have wrapped around.
    node.start()
    node.safe_sql("CREATE EXTENSION test_slru")

    multixact_ids = []
    for _ in range(1, 17):
        multi = node.safe_sql("SELECT test_create_multixact();")
        multixact_ids.append(multi)

    # Verify that wraparound occurred (last_multi should be numerically
    # smaller than first_multi)
    first_multi = multixact_ids[0]
    last_multi = multixact_ids[-1]
    assert int(last_multi) < int(
        first_multi
    ), f"multixact wraparound occurred (first: {first_multi}, last: {last_multi})"

    # Verify that all the multixacts we created are readable
    for i, multi in enumerate(multixact_ids):
        assert (
            node.safe_sql(f"SELECT test_read_multixact('{multi}');") == ""
        ), f"multixact {i} (ID: {multi}) is readable after wraparound"
