# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Demonstrate that logical can follow timeline switches.

Logical replication slots can follow timeline switches but it's normally not
possible to have a logical slot on a replica where promotion and a timeline
switch can occur.  The only ways we can create that circumstance are:

* By doing a filesystem-level copy of the DB, since pg_basebackup excludes
  pg_replslot but we can copy it directly; or

* by creating a slot directly at the C level on the replica and advancing it
  as we go using the low level APIs.  It can't be done from SQL since logical
  decoding isn't allowed on replicas.

This module uses the first approach to show that timeline following on a
logical slot works.

(For convenience, it also tests some recovery-related operations on logical
slots).
"""

import re

from pypg.util import TIMEOUT_DEFAULT


def test_010_logical_decoding_timelines(create_pg):
    # Initialize primary node
    node_primary = create_pg(
        "primary", allows_streaming="logical", has_archiving=True, start=False
    )
    node_primary.append_conf(
        """
wal_level = 'logical'
max_replication_slots = 3
max_wal_senders = 2
log_min_messages = 'debug2'
hot_standby_feedback = on
wal_receiver_status_interval = 1
"""
    )
    node_primary.start()

    print("# testing logical timeline following with a filesystem-level copy")

    node_primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('before_basebackup', "
        "'test_decoding');"
    )
    node_primary.safe_sql("CREATE TABLE decoding(blah text);")
    node_primary.safe_sql("INSERT INTO decoding(blah) VALUES ('beforebb');")

    # We also want to verify that DROP DATABASE on a standby with a logical
    # slot works.  This isn't strictly related to timeline following, but the
    # only way to get a logical slot on a standby right now is to use the same
    # physical copy trick, so:
    node_primary.safe_sql("CREATE DATABASE dropme;")
    node_primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('dropme_slot', 'test_decoding');",
        dbname="dropme",
    )

    node_primary.safe_sql("CHECKPOINT;")

    backup_name = "b1"
    node_primary.stop()
    node_primary.backup_fs_cold(backup_name)
    node_primary.start()

    node_primary.safe_sql("SELECT pg_create_physical_replication_slot('phys_slot');")

    node_replica = create_pg("replica", start=False)
    node_replica.init_from_backup(
        node_primary, backup_name, has_streaming=True, has_restoring=True
    )
    node_replica.append_conf("primary_slot_name = 'phys_slot'")

    node_replica.start()

    # If we drop 'dropme' on the primary, the standby should drop the db and
    # associated slot.
    res = node_primary.sql("DROP DATABASE dropme")
    assert res.error_message is None, "dropped DB with logical slot OK on primary"
    node_primary.wait_for_catchup(node_replica)
    assert (
        node_replica.safe_sql("SELECT 1 FROM pg_database WHERE datname = 'dropme'")
        == ""
    ), "dropped DB dropme on standby"
    assert (
        node_replica.slot("dropme_slot")["plugin"] == ""
    ), "logical slot was actually dropped on standby"

    # Back to testing failover...
    node_primary.safe_sql(
        "SELECT pg_create_logical_replication_slot('after_basebackup', "
        "'test_decoding');"
    )
    node_primary.safe_sql("INSERT INTO decoding(blah) VALUES ('afterbb');")
    node_primary.safe_sql("CHECKPOINT;")

    # Verify that only the before base_backup slot is on the replica
    stdout = node_replica.safe_sql(
        "SELECT slot_name FROM pg_replication_slots ORDER BY slot_name"
    )
    assert (
        stdout == "before_basebackup"
    ), "Expected to find only slot before_basebackup on replica"

    # Examine the physical slot the replica uses to stream changes from the
    # primary to make sure its hot_standby_feedback has locked in a
    # catalog_xmin on the physical slot, and that any xmin is >= the
    # catalog_xmin
    assert node_primary.poll_query_until(
        """
        SELECT catalog_xmin IS NOT NULL
        FROM pg_replication_slots
        WHERE slot_name = 'phys_slot'
        """
    ), "slot's catalog_xmin never became set"

    phys_slot = node_primary.slot("phys_slot")
    assert phys_slot["xmin"] != "", "xmin assigned on physical slot of primary"
    assert (
        phys_slot["catalog_xmin"] != ""
    ), "catalog_xmin assigned on physical slot of primary"

    # Ignore wrap-around here, we're on a new cluster:
    assert int(phys_slot["xmin"]) >= int(
        phys_slot["catalog_xmin"]
    ), "xmin on physical slot must not be lower than catalog_xmin"

    node_primary.safe_sql("CHECKPOINT")
    node_primary.wait_for_catchup(node_replica, "write")

    # Boom, crash
    node_primary.stop("immediate")

    node_replica.promote()

    node_replica.safe_sql("INSERT INTO decoding(blah) VALUES ('after failover');")

    # Shouldn't be able to read from slot created after base backup
    res = node_replica.sql(
        "SELECT data FROM pg_logical_slot_peek_changes('after_basebackup', "
        "NULL, NULL, 'include-xids', '0', 'skip-empty-xacts', '1');"
    )
    assert res.error_message is not None, "replaying from after_basebackup slot fails"
    assert re.search(
        r'replication slot "after_basebackup" does not exist', res.error_message
    ), "after_basebackup slot missing"

    # Should be able to read from slot created before base backup
    res = node_replica.sql(
        "SELECT data FROM pg_logical_slot_peek_changes('before_basebackup', "
        "NULL, NULL, 'include-xids', '0', 'skip-empty-xacts', '1');"
    )
    assert res.error_message is None, "replay from slot before_basebackup succeeds"

    final_expected_output_bb = (
        "BEGIN\n"
        "table public.decoding: INSERT: blah[text]:'beforebb'\n"
        "COMMIT\n"
        "BEGIN\n"
        "table public.decoding: INSERT: blah[text]:'afterbb'\n"
        "COMMIT\n"
        "BEGIN\n"
        "table public.decoding: INSERT: blah[text]:'after failover'\n"
        "COMMIT"
    )
    stdout = "\n".join(row[0] for row in res.rows)
    assert (
        stdout == final_expected_output_bb
    ), "decoded expected data from slot before_basebackup"

    # So far we've peeked the slots, so when we fetch the same info over
    # pg_recvlogical we should get complete results.  First, find out the
    # commit lsn of the last transaction.  There's no max(pg_lsn), so:
    endpos = node_replica.safe_sql(
        "SELECT lsn FROM pg_logical_slot_peek_changes('before_basebackup', "
        "NULL, NULL) ORDER BY lsn DESC LIMIT 1;"
    )

    # now use the walsender protocol to peek the slot changes and make sure we
    # see the same results.
    result = node_replica.pg_recvlogical_upto(
        "postgres",
        "before_basebackup",
        endpos,
        TIMEOUT_DEFAULT,
        **{"include-xids": "0", "skip-empty-xacts": "1"}
    )

    # walsender likes to add a newline
    stdout = result.stdout.rstrip("\n")
    assert (
        stdout == final_expected_output_bb
    ), "got same output from walsender via pg_recvlogical on before_basebackup"
