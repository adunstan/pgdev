# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test for replication slot limit.

Ensure that max_slot_wal_keep_size limits the number of WAL files to
be kept by replication slots.
"""

import os
import re
import signal
import time

from pypg.util import TIMEOUT_DEFAULT, WINDOWS_OS


def wait_for_slot_catchup(node, slot_name, mode, target_lsn):
    """Poll pg_replication_slots until the slot's <mode>_lsn reaches
    *target_lsn*.
    """
    assert mode in ("restart", "confirmed_flush"), \
        "valid modes are restart, confirmed_flush"
    assert target_lsn is not None, "target lsn must be specified"
    print(
        f"Waiting for replication slot {slot_name}'s {mode}_lsn to pass "
        f"{target_lsn} on {node.name}"
    )
    query = (
        f"SELECT '{target_lsn}' <= {mode}_lsn "
        "FROM pg_catalog.pg_replication_slots "
        f"WHERE slot_name = '{slot_name}'"
    )
    if not node.poll_query_until(query):
        details = node.safe_sql("SELECT * FROM pg_catalog.pg_replication_slots")
        raise TimeoutError(
            "timed out waiting for catchup\n"
            f"Last pg_replication_slots contents:\n{details}"
        )
    print("done")


def validate_slot_inactive_since(node, slot_name, reference_time):
    """Return the slot's inactive_since, asserting it is sane (later than the
    epoch and than *reference_time*).
    """
    inactive_since = node.safe_sql(
        "SELECT inactive_since FROM pg_replication_slots "
        f"WHERE slot_name = '{slot_name}' AND inactive_since IS NOT NULL"
    )
    assert node.safe_sql(
        f"SELECT '{inactive_since}'::timestamptz > to_timestamp(0) AND "
        f"'{inactive_since}'::timestamptz > '{reference_time}'::timestamptz"
    ) == "t", \
        f"last inactive time for slot {slot_name} is valid on node {node.name}"
    return inactive_since


def test_019_replslot_limit(create_pg):
    # Initialize primary node, setting wal-segsize to 1MB
    node_primary = create_pg(
        "primary", allows_streaming=True, initdb_extra=["--wal-segsize=1"])
    node_primary.append_conf("""
min_wal_size = 2MB
max_wal_size = 4MB
log_checkpoints = yes
""")
    node_primary.restart()
    node_primary.safe_sql(
        "SELECT pg_create_physical_replication_slot('rep1')")

    # The slot state and remain should be null before the first connection
    result = node_primary.safe_sql(
        "SELECT restart_lsn IS NULL, wal_status is NULL, "
        "safe_wal_size is NULL FROM pg_replication_slots "
        "WHERE slot_name = 'rep1'")
    assert result == "t|t|t", 'check the state of non-reserved slot is "unknown"'

    # Take backup
    backup_name = "my_backup"
    node_primary.backup(backup_name)

    # Create a standby linking to it using the replication slot
    node_standby = create_pg("standby_1", start=False)
    node_standby.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby.append_conf("primary_slot_name = 'rep1'")

    node_standby.start()

    # Wait until the primary has processed standby feedback and advanced the
    # slot's restart_lsn.  For a physical slot, restart_lsn is updated from
    # the standby's reported flush position, so this waits for the primary-side
    # slot state that the following wal_status checks depend on.
    wait_for_slot_catchup(node_primary, "rep1", "restart",
                          node_primary.lsn("write"))

    # Stop standby
    node_standby.stop()

    # Preparation done, the slot is the state "reserved" now
    result = node_primary.safe_sql(
        "SELECT wal_status, safe_wal_size IS NULL FROM pg_replication_slots "
        "WHERE slot_name = 'rep1'")
    assert result == "reserved|t", "check the catching-up state"

    # Advance WAL by one segment (= 1MB) on primary
    node_primary.advance_wal(1)
    node_primary.safe_sql("CHECKPOINT;")

    # The slot is always "safe" when fitting max_wal_size
    result = node_primary.safe_sql(
        "SELECT wal_status, safe_wal_size IS NULL FROM pg_replication_slots "
        "WHERE slot_name = 'rep1'")
    assert result == "reserved|t", \
        "check that it is safe if WAL fits in max_wal_size"

    node_primary.advance_wal(4)
    node_primary.safe_sql("CHECKPOINT;")

    # The slot is always "safe" when max_slot_wal_keep_size is not set
    result = node_primary.safe_sql(
        "SELECT wal_status, safe_wal_size IS NULL FROM pg_replication_slots "
        "WHERE slot_name = 'rep1'")
    assert result == "reserved|t", "check that slot is working"

    # The standby can reconnect to primary
    node_standby.start()

    wait_for_slot_catchup(node_primary, "rep1", "restart",
                          node_primary.lsn("write"))

    node_standby.stop()

    # Set max_slot_wal_keep_size on primary
    max_slot_wal_keep_size_mb = 6
    node_primary.append_conf(f"""
max_slot_wal_keep_size = {max_slot_wal_keep_size_mb}MB
""")
    node_primary.reload()

    # The slot is in safe state.
    result = node_primary.safe_sql(
        "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'rep1'")
    assert result == "reserved", "check that max_slot_wal_keep_size is working"

    # Advance WAL again then checkpoint, reducing remain by 2 MB.
    node_primary.advance_wal(2)
    node_primary.safe_sql("CHECKPOINT;")

    # The slot is still working
    result = node_primary.safe_sql(
        "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'rep1'")
    assert result == "reserved", \
        "check that slot remains reserved after advancing WAL"

    # The standby can reconnect to primary
    node_standby.start()
    wait_for_slot_catchup(node_primary, "rep1", "restart",
                          node_primary.lsn("write"))
    node_standby.stop()

    # wal_keep_size overrides max_slot_wal_keep_size
    node_primary.safe_sql("ALTER SYSTEM SET wal_keep_size to '8MB'")
    node_primary.safe_sql("SELECT pg_reload_conf()")
    # Advance WAL again, reducing remain by 6 MB.
    node_primary.advance_wal(6)
    result = node_primary.safe_sql(
        "SELECT wal_status as remain FROM pg_replication_slots "
        "WHERE slot_name = 'rep1'")
    assert result == "extended", \
        "check that wal_keep_size overrides max_slot_wal_keep_size"
    # restore wal_keep_size
    node_primary.safe_sql("ALTER SYSTEM SET wal_keep_size to 0")
    node_primary.safe_sql("SELECT pg_reload_conf()")

    # The standby can reconnect to primary
    node_standby.start()
    wait_for_slot_catchup(node_primary, "rep1", "restart",
                          node_primary.lsn("write"))
    node_standby.stop()

    # Advance WAL again without checkpoint, reducing remain by 6 MB.
    node_primary.advance_wal(6)

    # Slot gets into 'extended' state
    result = node_primary.safe_sql(
        "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'rep1'")
    assert result == "extended", 'check that the slot state changes to "extended"'

    # do checkpoint so that the next checkpoint runs too early
    node_primary.safe_sql("CHECKPOINT;")

    # Advance WAL again without checkpoint; remain goes to 0.
    node_primary.advance_wal(1)

    # Slot gets into 'unreserved' state and safe_wal_size is negative
    result = node_primary.safe_sql(
        "SELECT wal_status, safe_wal_size <= 0 FROM pg_replication_slots "
        "WHERE slot_name = 'rep1'")
    assert result == "unreserved|t", \
        'check that the slot state changes to "unreserved"'

    # The standby still can connect to primary before a checkpoint
    node_standby.start()

    wait_for_slot_catchup(node_primary, "rep1", "restart",
                          node_primary.lsn("write"))

    node_standby.stop()

    assert not node_standby.log_contains(
        "requested WAL segment [0-9A-F]+ has already been removed"), \
        "check that required WAL segments are still available"

    # Create one checkpoint, to improve stability of the next steps
    node_primary.safe_sql("CHECKPOINT;")

    # Prevent other checkpoints from occurring while advancing WAL segments
    node_primary.safe_sql("ALTER SYSTEM SET max_wal_size='40MB'")
    node_primary.safe_sql("SELECT pg_reload_conf()")

    # Advance WAL again. The slot loses the oldest segment by the next checkpoint
    logstart = node_primary.log_position()
    node_primary.advance_wal(7)

    # Now create another checkpoint and wait until the WARNING is issued
    node_primary.safe_sql("ALTER SYSTEM RESET max_wal_size")
    node_primary.safe_sql("SELECT pg_reload_conf()")
    node_primary.safe_sql("CHECKPOINT;")
    invalidated = False
    for _ in range(10 * TIMEOUT_DEFAULT):
        if node_primary.log_contains(
                'invalidating obsolete replication slot "rep1"', logstart):
            invalidated = True
            break
        time.sleep(0.1)
    assert invalidated, "check that slot invalidation has been logged"

    result = node_primary.safe_sql(
        "SELECT slot_name, active, restart_lsn IS NULL, wal_status, "
        "safe_wal_size FROM pg_replication_slots WHERE slot_name = 'rep1'")
    assert result == "rep1|f|t|lost|", \
        'check that the slot became inactive and the state "lost" persists'

    # Wait until current checkpoint ends
    checkpoint_ended = False
    for _ in range(10 * TIMEOUT_DEFAULT):
        if node_primary.log_contains("checkpoint complete: ", logstart):
            checkpoint_ended = True
            break
        time.sleep(0.1)
    assert checkpoint_ended, "waited for checkpoint to end"

    # The invalidated slot shouldn't keep the old-segment horizon back;
    # see bug #17103: https://postgr.es/m/17103-004130e8f27782c9@postgresql.org
    # Test for this by creating a new slot and comparing its restart LSN
    # to the oldest existing file.
    redoseg = node_primary.safe_sql(
        "SELECT pg_walfile_name(lsn) FROM "
        "pg_create_physical_replication_slot('s2', true)")
    oldestseg = node_primary.safe_sql(
        "SELECT pg_ls_dir AS f FROM pg_ls_dir('pg_wal') "
        "WHERE pg_ls_dir ~ '^[0-9A-F]{24}$' ORDER BY 1 LIMIT 1")
    node_primary.safe_sql("SELECT pg_drop_replication_slot('s2')")
    assert oldestseg == redoseg, "check that segments have been removed"

    # The standby no longer can connect to the primary
    logstart = node_standby.log_position()
    node_standby.start()

    failed = False
    for _ in range(10 * TIMEOUT_DEFAULT):
        if node_standby.log_contains(
                'This replication slot has been invalidated due to '
                '"wal_removed".', logstart):
            failed = True
            break
        time.sleep(0.1)
    assert failed, "check that replication has been broken"

    node_primary.stop()
    node_standby.stop()

    node_primary2 = create_pg("primary2", allows_streaming=True)
    node_primary2.append_conf("""
min_wal_size = 32MB
max_wal_size = 32MB
log_checkpoints = yes
""")
    node_primary2.restart()
    node_primary2.safe_sql(
        "SELECT pg_create_physical_replication_slot('rep1')")
    backup_name = "my_backup2"
    node_primary2.backup(backup_name)

    node_primary2.stop()
    node_primary2.append_conf("""
max_slot_wal_keep_size = 0
""")
    node_primary2.start()

    node_standby = create_pg("standby_2", start=False)
    node_standby.init_from_backup(node_primary2, backup_name, has_streaming=True)
    node_standby.append_conf("primary_slot_name = 'rep1'")
    node_standby.start()
    node_primary2.advance_wal(1)
    node_primary2.safe_sql("CHECKPOINT;")
    result = node_primary2.safe_sql("SELECT 'finished';")
    assert result == "finished", "check if checkpoint command is not blocked"

    node_primary2.stop()
    node_standby.stop()

    # The remaining cases freeze the walsender/walreceiver with SIGSTOP/SIGCONT,
    # which is not portable to Windows; end the test here on that platform.
    if WINDOWS_OS:
        return

    # Get a slot terminated while the walsender is active
    # We do this by sending SIGSTOP to the walsender.
    node_primary3 = create_pg(
        "primary3", allows_streaming=True, initdb_extra=["--wal-segsize=1"])
    node_primary3.append_conf("""
min_wal_size = 2MB
max_wal_size = 2MB
log_checkpoints = yes
max_slot_wal_keep_size = 1MB
""")
    node_primary3.restart()
    node_primary3.safe_sql(
        "SELECT pg_create_physical_replication_slot('rep3')")
    # Take backup
    backup_name = "my_backup"
    node_primary3.backup(backup_name)
    # Create standby
    node_standby3 = create_pg("standby_3", start=False)
    node_standby3.init_from_backup(node_primary3, backup_name, has_streaming=True)
    node_standby3.append_conf("primary_slot_name = 'rep3'")
    node_standby3.start()
    node_primary3.wait_for_catchup(node_standby3)

    senderpid = None

    # We've seen occasional cases where multiple walsender pids are still
    # active at this point, apparently just due to process shutdown being slow.
    # To avoid spurious failures, retry a couple times.
    i = 0
    while True:
        senderpid = node_primary3.safe_sql(
            "SELECT pid FROM pg_stat_activity "
            "WHERE backend_type = 'walsender'")

        if re.fullmatch(r"[0-9]+", senderpid):
            break

        print(f"multiple walsenders active in iteration {i}")

        # show information about all active connections
        stdout = node_primary3.safe_sql("SELECT * FROM pg_stat_activity")
        print(stdout)

        if i == 10 * TIMEOUT_DEFAULT:
            # An immediate shutdown may hide evidence of a locking bug. If
            # retrying didn't resolve the issue, shut down in fast mode.
            node_primary3.stop("fast")
            node_standby3.stop("fast")
            raise RuntimeError(
                "could not determine walsender pid, can't continue")
        i += 1

        time.sleep(0.1)

    assert re.fullmatch(r"[0-9]+", senderpid), \
        f"have walsender pid {senderpid}"

    receiverpid = node_standby3.safe_sql(
        "SELECT pid FROM pg_stat_activity WHERE backend_type = 'walreceiver'")
    assert re.fullmatch(r"[0-9]+", receiverpid), \
        f"have walreceiver pid {receiverpid}"

    logstart = node_primary3.log_position()
    # freeze walsender and walreceiver. Slot will still be active, but
    # walreceiver won't get anything anymore.
    os.kill(int(senderpid), signal.SIGSTOP)
    os.kill(int(receiverpid), signal.SIGSTOP)
    node_primary3.advance_wal(2)

    msg_logged = False
    max_attempts = TIMEOUT_DEFAULT
    while max_attempts >= 0:
        if node_primary3.log_contains(
                f"terminating process {senderpid} to release "
                'replication slot "rep3"', logstart):
            msg_logged = True
            break
        time.sleep(1)
        max_attempts -= 1
    assert msg_logged, "walsender termination logged"

    # Now let the walsender continue; slot should be killed now.
    # (Must not let walreceiver run yet; otherwise the standby could start
    # another one before the slot can be killed)
    os.kill(int(senderpid), signal.SIGCONT)
    assert node_primary3.poll_query_until(
        "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'rep3'",
        "lost"), "timed out waiting for slot to be lost"

    msg_logged = False
    max_attempts = TIMEOUT_DEFAULT
    while max_attempts >= 0:
        if node_primary3.log_contains(
                'invalidating obsolete replication slot "rep3"', logstart):
            msg_logged = True
            break
        time.sleep(1)
        max_attempts -= 1
    assert msg_logged, "slot invalidation logged"

    # Now let the walreceiver continue, so that the node can be stopped cleanly
    os.kill(int(receiverpid), signal.SIGCONT)

    node_primary3.stop()
    node_standby3.stop()

    # =========================================================================
    # Testcase start: Check inactive_since property of the streaming standby's
    # slot
    #

    # Initialize primary node
    primary4 = create_pg("primary4", allows_streaming="logical")

    # Take backup
    backup_name = "my_backup4"
    primary4.backup(backup_name)

    # Create a standby linking to the primary using the replication slot
    standby4 = create_pg("standby4", start=False)
    standby4.init_from_backup(primary4, backup_name, has_streaming=True)

    sb4_slot = "sb4_slot"
    standby4.append_conf(f"primary_slot_name = '{sb4_slot}'")

    slot_creation_time = primary4.safe_sql("SELECT current_timestamp")

    primary4.safe_sql(
        f"SELECT pg_create_physical_replication_slot(slot_name := '{sb4_slot}')")

    # Get inactive_since value after the slot's creation. Note that the slot is
    # still inactive till it's used by the standby below.
    inactive_since = validate_slot_inactive_since(
        primary4, sb4_slot, slot_creation_time)

    standby4.start()

    # Wait until standby has replayed enough data
    primary4.wait_for_catchup(standby4)

    # Now the slot is active so inactive_since value must be NULL
    assert primary4.safe_sql(
        "SELECT inactive_since IS NULL FROM pg_replication_slots "
        f"WHERE slot_name = '{sb4_slot}'") == "t", \
        "last inactive time for an active physical slot is NULL"

    # Stop the standby to check its inactive_since value is updated
    standby4.stop()

    # Let's restart the primary so that the inactive_since is set upon loading
    # the slot from the disk.
    primary4.restart()

    assert primary4.safe_sql(
        f"SELECT inactive_since > '{inactive_since}'::timestamptz "
        "FROM pg_replication_slots "
        f"WHERE slot_name = '{sb4_slot}' AND inactive_since IS NOT NULL") == "t", \
        "last inactive time for an inactive physical slot is updated correctly"

    # Testcase end: Check inactive_since property of the streaming standby's slot
    # =========================================================================

    # =========================================================================
    # Testcase start: Check inactive_since property of the logical
    # subscriber's slot
    publisher4 = primary4

    # Create subscriber node
    subscriber4 = create_pg("subscriber4", start=False)

    # Setup logical replication
    publisher4_connstr = (
        f"host={publisher4.host} port={publisher4.port} dbname=postgres")
    publisher4.safe_sql("CREATE PUBLICATION pub FOR ALL TABLES")

    slot_creation_time = publisher4.safe_sql("SELECT current_timestamp")

    lsub4_slot = "lsub4_slot"
    publisher4.safe_sql(
        f"SELECT pg_create_logical_replication_slot(slot_name := '{lsub4_slot}', "
        "plugin := 'pgoutput')")

    # Get inactive_since value after the slot's creation. Note that the slot is
    # still inactive till it's used by the subscriber below.
    inactive_since = validate_slot_inactive_since(
        publisher4, lsub4_slot, slot_creation_time)

    subscriber4.start()
    subscriber4.safe_sql(
        f"CREATE SUBSCRIPTION sub CONNECTION '{publisher4_connstr}' "
        f"PUBLICATION pub WITH (slot_name = '{lsub4_slot}', create_slot = false)")

    # Wait until subscriber has caught up
    subscriber4.wait_for_subscription_sync(publisher4, "sub")

    # Now the slot is active so inactive_since value must be NULL
    assert publisher4.safe_sql(
        "SELECT inactive_since IS NULL FROM pg_replication_slots "
        f"WHERE slot_name = '{lsub4_slot}'") == "t", \
        "last inactive time for an active logical slot is NULL"

    # Stop the subscriber to check its inactive_since value is updated
    subscriber4.stop()

    # Let's restart the publisher so that the inactive_since is set upon
    # loading the slot from the disk.
    publisher4.restart()

    assert publisher4.safe_sql(
        f"SELECT inactive_since > '{inactive_since}'::timestamptz "
        "FROM pg_replication_slots "
        f"WHERE slot_name = '{lsub4_slot}' "
        "AND inactive_since IS NOT NULL") == "t", \
        "last inactive time for an inactive logical slot is updated correctly"

    # Testcase end: Check inactive_since property of the logical subscriber's
    # slot
    # =========================================================================

    publisher4.stop()
    subscriber4.stop()
