# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Verify WAL consistency for BRIN indexes."""


def test_02_wal_consistency(create_pg):
    # Set up primary
    whiskey = create_pg("whiskey", start=False, allows_streaming=True)
    whiskey.append_conf("wal_consistency_checking = brin")
    whiskey.start()
    whiskey.safe_sql("create extension pageinspect")
    whiskey.safe_sql("create extension pg_walinspect")
    res = whiskey.sql("SELECT pg_create_physical_replication_slot('standby_1');")
    assert res.error_message is None, "physical slot created on primary"

    # Take backup
    backup_name = "brinbkp"
    whiskey.backup(backup_name)

    # Create streaming standby linking to primary
    charlie = create_pg("charlie", start=False)
    charlie.init_from_backup(whiskey, backup_name, has_streaming=True)
    charlie.append_conf("primary_slot_name = standby_1")
    charlie.start()

    # Now write some WAL in the primary
    whiskey.safe_sql(
        "create table tbl_timestamp0 (d1 timestamp(0) without time zone) "
        "with (fillfactor=10);\n"
        "create index on tbl_timestamp0 using brin (d1) "
        "with (pages_per_range = 1, autosummarize=false);\n"
    )
    start_lsn = whiskey.lsn("insert")
    # Run a loop that will end when the second revmap page is created
    whiskey.safe_sql(
        """
do
$$
declare
  current timestamp with time zone := '2019-03-27 08:14:01.123456789 UTC';
begin
  loop
    insert into tbl_timestamp0 select i from
      generate_series(current, current + interval '1 day', '28 seconds') i;
    perform brin_summarize_new_values('tbl_timestamp0_d1_idx');
    if (brin_metapage_info(get_raw_page('tbl_timestamp0_d1_idx', 0))).lastrevmappage > 1 then
      exit;
    end if;
    current := current + interval '1 day';
  end loop;
end
$$;
"""
    )
    end_lsn = whiskey.lsn("flush")

    out = whiskey.safe_sql(
        f"select count(*) from pg_get_wal_records_info('{start_lsn}', '{end_lsn}')\n"
        "where resource_manager = 'BRIN' AND\n"
        "record_type ILIKE '%revmap%'"
    )
    assert int(out) >= 1

    whiskey.wait_for_replay_catchup(charlie)
