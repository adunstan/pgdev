create table foo (t text not null, i serial primary key, check (t > 'a'));

create temp table bar (t text not null) on commit delete rows;


\pset tuples_only
\pset format unaligned

select pg_get_table_ddl('foo');

select pg_get_table_ddl('bar');


drop table foo;

drop table bar;
