# Copyright (c) 2026, PostgreSQL Global Development Group

"""Exercise full-page-image / WAL behaviour around data checksum changes.

Toggles data checksums and full_page_writes across restarts and verifies that
no checksum validation errors are logged.
"""

import re


def test_009_fpi(create_pg, checksums):
    # Create and start a cluster with one node, checksums disabled.
    node = create_pg(
        "fpi_node",
        start=False,
        initdb_extra=["--no-data-checksums"],
        allows_streaming=True,
    )
    # max_connections need to be bumped in order to accommodate for pgbench
    # clients and log_statement is dialled down since it otherwise will
    # generate enormous amounts of logging.  Page verification failures are
    # still logged.
    node.append_conf(
        "\n".join(
            [
                "max_connections = 100",
                "log_statement = none",
            ]
        )
    )
    node.start()
    node.safe_sql("CREATE EXTENSION test_checksums;")
    # Create some content to have un-checksummed data in the cluster
    node.safe_sql("CREATE TABLE t AS SELECT generate_series(1, 1000000) AS a;")

    # Enable data checksums and wait for the state transition to 'on'
    checksums.enable_data_checksums(node, wait="on")

    node.safe_sql("UPDATE t SET a = a + 1;")

    checksums.disable_data_checksums(node, wait=1)

    node.append_conf("full_page_writes = off")
    node.restart()
    checksums.test_checksum_state(node, "off")

    node.safe_sql("UPDATE t SET a = a + 1;")
    node.safe_sql("DELETE FROM t WHERE a < 10000;")

    # adjust_conf(full_page_writes => on); appending wins since the last
    # setting in postgresql.conf takes effect.
    node.append_conf("full_page_writes = on")
    node.restart()
    checksums.test_checksum_state(node, "off")

    checksums.enable_data_checksums(node, wait="on")

    result = node.safe_sql("SELECT count(*) FROM t;")
    assert result == "990003", "Reading back all data from table t"

    node.stop()
    log = node.log_content()
    assert not re.search(r"page verification failed,.+\d$", log, re.MULTILINE), (
        "no checksum validation errors in server log"
    )
