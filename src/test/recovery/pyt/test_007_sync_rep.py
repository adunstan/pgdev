# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Minimal test testing synchronous replication sync_state transition."""

# Query checking sync_priority and sync_state of each standby
CHECK_SQL = (
    "SELECT application_name, sync_priority, sync_state "
    "FROM pg_stat_replication ORDER BY application_name;"
)


def check_sync_state(node, expected, msg, setting=None):
    """Check that sync_state of each standby is expected (waiting till it is).

    If *setting* is given, synchronous_standby_names is set to it and the
    configuration file is reloaded before the test.
    """
    if setting is not None:
        node.safe_sql(
            f"ALTER SYSTEM SET synchronous_standby_names = '{setting}';")
        node.reload()

    assert node.poll_query_until(CHECK_SQL, expected), msg


def start_standby_and_wait(primary, standby):
    """Start a standby and check that it is registered on the primary.

    Polls the primary's pg_stat_replication until the standby is confirmed as
    registered within the WAL sender array of the given primary.
    """
    standby_name = standby.name
    query = (
        "SELECT count(1) = 1 FROM pg_stat_replication "
        f"WHERE application_name = '{standby_name}'"
    )

    standby.start()

    print(f'### Waiting for standby "{standby_name}" on "{primary.name}"')
    assert primary.poll_query_until(query)


def test_007_sync_rep(create_pg):
    # Initialize primary node
    node_primary = create_pg("primary", allows_streaming=True)
    backup_name = "primary_backup"

    # Take backup
    node_primary.backup(backup_name)

    # Create all the standbys.  Their status on the primary is checked to
    # ensure the ordering of each one of them in the WAL sender array of the
    # primary.

    # Create standby1 linking to primary
    node_standby_1 = create_pg("standby1", start=False)
    node_standby_1.init_from_backup(node_primary, backup_name, has_streaming=True)
    start_standby_and_wait(node_primary, node_standby_1)

    # Create standby2 linking to primary
    node_standby_2 = create_pg("standby2", start=False)
    node_standby_2.init_from_backup(node_primary, backup_name, has_streaming=True)
    start_standby_and_wait(node_primary, node_standby_2)

    # Create standby3 linking to primary
    node_standby_3 = create_pg("standby3", start=False)
    node_standby_3.init_from_backup(node_primary, backup_name, has_streaming=True)
    start_standby_and_wait(node_primary, node_standby_3)

    # Check that sync_state is determined correctly when
    # synchronous_standby_names is specified in old syntax.
    check_sync_state(
        node_primary,
        "standby1|1|sync\n"
        "standby2|2|potential\n"
        "standby3|0|async",
        "old syntax of synchronous_standby_names",
        "standby1,standby2")

    # Check that all the standbys are considered as either sync or
    # potential when * is specified in synchronous_standby_names.
    # Note that standby1 is chosen as sync standby because
    # it's stored in the head of WalSnd array which manages
    # all the standbys though they have the same priority.
    check_sync_state(
        node_primary,
        "standby1|1|sync\n"
        "standby2|1|potential\n"
        "standby3|1|potential",
        "asterisk in synchronous_standby_names",
        "*")

    # Stop and start standbys to rearrange the order of standbys
    # in WalSnd array. Now, if standbys have the same priority,
    # standby2 is selected preferentially and standby3 is next.
    node_standby_1.stop()
    node_standby_2.stop()
    node_standby_3.stop()

    # Make sure that each standby reports back to the primary in the wanted
    # order.
    start_standby_and_wait(node_primary, node_standby_2)
    start_standby_and_wait(node_primary, node_standby_3)

    # Specify 2 as the number of sync standbys.
    # Check that two standbys are in 'sync' state.
    check_sync_state(
        node_primary,
        "standby2|2|sync\n"
        "standby3|3|sync",
        "2 synchronous standbys",
        "2(standby1,standby2,standby3)")

    # Start standby1
    start_standby_and_wait(node_primary, node_standby_1)

    # Create standby4 linking to primary
    node_standby_4 = create_pg("standby4", start=False)
    node_standby_4.init_from_backup(node_primary, backup_name, has_streaming=True)
    node_standby_4.start()

    # Check that standby1 and standby2 whose names appear earlier in
    # synchronous_standby_names are considered as sync. Also check that
    # standby3 appearing later represents potential, and standby4 is
    # in 'async' state because it's not in the list.
    check_sync_state(
        node_primary,
        "standby1|1|sync\n"
        "standby2|2|sync\n"
        "standby3|3|potential\n"
        "standby4|0|async",
        "2 sync, 1 potential, and 1 async")

    # Check that sync_state of each standby is determined correctly
    # when num_sync exceeds the number of names of potential sync standbys
    # specified in synchronous_standby_names.
    check_sync_state(
        node_primary,
        "standby1|0|async\n"
        "standby2|4|sync\n"
        "standby3|3|sync\n"
        "standby4|1|sync",
        "num_sync exceeds the num of potential sync standbys",
        "6(standby4,standby0,standby3,standby2)")

    # The setting that * comes before another standby name is acceptable
    # but does not make sense in most cases. Check that sync_state is
    # chosen properly even in case of that setting. standby1 is selected
    # as synchronous as it has the highest priority, and is followed by a
    # second standby listed first in the WAL sender array, which is
    # standby2 in this case.
    check_sync_state(
        node_primary,
        "standby1|1|sync\n"
        "standby2|2|sync\n"
        "standby3|2|potential\n"
        "standby4|2|potential",
        "asterisk before another standby name",
        "2(standby1,*,standby2)")

    # Check that the setting of '2(*)' chooses standby2 and standby3 that are
    # stored earlier in WalSnd array as sync standbys.
    check_sync_state(
        node_primary,
        "standby1|1|potential\n"
        "standby2|1|sync\n"
        "standby3|1|sync\n"
        "standby4|1|potential",
        "multiple standbys having the same priority are chosen as sync",
        "2(*)")

    # Stop Standby3 which is considered in 'sync' state.
    node_standby_3.stop()

    # Check that the state of standby1 stored earlier in WalSnd array than
    # standby4 is transited from potential to sync.
    check_sync_state(
        node_primary,
        "standby1|1|sync\n"
        "standby2|1|sync\n"
        "standby4|1|potential",
        "potential standby found earlier in array is promoted to sync")

    # Check that standby1 and standby2 are chosen as sync standbys
    # based on their priorities.
    check_sync_state(
        node_primary,
        "standby1|1|sync\n"
        "standby2|2|sync\n"
        "standby4|0|async",
        "priority-based sync replication specified by FIRST keyword",
        "FIRST 2(standby1, standby2)")

    # Check that all the listed standbys are considered as candidates
    # for sync standbys in a quorum-based sync replication.
    check_sync_state(
        node_primary,
        "standby1|1|quorum\n"
        "standby2|1|quorum\n"
        "standby4|0|async",
        "2 quorum and 1 async",
        "ANY 2(standby1, standby2)")

    # Start Standby3 which will be considered in 'quorum' state.
    node_standby_3.start()

    # Check that the setting of 'ANY 2(*)' chooses all standbys as
    # candidates for quorum sync standbys.
    check_sync_state(
        node_primary,
        "standby1|1|quorum\n"
        "standby2|1|quorum\n"
        "standby3|1|quorum\n"
        "standby4|1|quorum",
        "all standbys are considered as candidates for quorum sync standbys",
        "ANY 2(*)")
