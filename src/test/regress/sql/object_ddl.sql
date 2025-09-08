create table foo (t text not null, i serial primary key, check (t > 'a'));

create temp table bar (t text not null) on commit delete rows;


\pset format unaligned

select pg_get_table_ddl('foo');

select pg_get_table_ddl('bar');

CREATE TABLE test_part_parent (
    a integer DEFAULT 2501,
    b integer DEFAULT 142857
)
PARTITION BY LIST (a);

create table test_part_partition_123 partition of test_part_parent for values in (1,2,3);

create table test_part_partition_def partition of test_part_parent default;

select pg_get_table_ddl('test_part_parent');

select pg_get_table_ddl('test_part_partition_123');

select pg_get_table_ddl('test_part_partition_def');


drop table foo;

drop table bar;
