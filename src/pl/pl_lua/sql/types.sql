--
-- Conversion of scalar values between SQL and Lua
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
SET datestyle = iso, mdy;
\set VERBOSITY terse

-- integers and floats become Lua numbers
CREATE FUNCTION ty_describe(v anyelement) RETURNS text LANGUAGE pl_lua AS $$
  return type(v) .. ":" .. tostring(v) .. ":" ..
         (math.type(v) or "none") .. ":" .. tostring(pgtype(v, 1) and pgtype(v, 1):name())
$$;
SELECT ty_describe(42::int2), ty_describe(42::int4), ty_describe(42::int8),
       ty_describe(42::oid), ty_describe(1.5::float4), ty_describe(2.25::float8);
SELECT ty_describe(true), ty_describe(false);
SELECT ty_describe('abc'::text), ty_describe('abc'::varchar(10)), ty_describe('abc'::name),
       ty_describe('ab '::char(5));
SELECT ty_describe('{"a": 1}'::json), ty_describe(NULL::int);

-- datum objects for other types
SELECT ty_describe(1.5::numeric), ty_describe('2024-05-06'::date),
       ty_describe('{1,2}'::int[]), ty_describe(int4range(1, 5));

-- bytea is raw bytes
CREATE FUNCTION ty_bytes(b bytea) RETURNS int LANGUAGE pl_lua AS $$
  return #b
$$;
SELECT ty_bytes('\x00010203'::bytea), ty_bytes(''::bytea);
CREATE FUNCTION ty_mkbytes() RETURNS bytea LANGUAGE pl_lua AS $$
  return "\0\1\255abc"
$$;
SELECT ty_mkbytes();

-- from Lua to SQL
CREATE FUNCTION ty_ret_int2(v anyelement) RETURNS int2 LANGUAGE pl_lua AS $$ return v $$;
CREATE FUNCTION ty_ret_int4(v anyelement) RETURNS int4 LANGUAGE pl_lua AS $$ return v $$;
CREATE FUNCTION ty_ret_int8(v anyelement) RETURNS int8 LANGUAGE pl_lua AS $$ return v $$;
CREATE FUNCTION ty_ret_float4(v anyelement) RETURNS float4 LANGUAGE pl_lua AS $$ return v $$;
CREATE FUNCTION ty_ret_bool(v anyelement) RETURNS bool LANGUAGE pl_lua AS $$ return v $$;
CREATE FUNCTION ty_ret_text(v anyelement) RETURNS text LANGUAGE pl_lua AS $$ return v $$;
CREATE FUNCTION ty_ret_numeric(v anyelement) RETURNS numeric LANGUAGE pl_lua AS $$ return v $$;
SELECT ty_ret_int4(7), ty_ret_int4(7.0::float8), ty_ret_int2(32767), ty_ret_int8(9007199254740993);
SELECT ty_ret_int4(7.5::float8);
SELECT ty_ret_int2(32768);
SELECT ty_ret_int4(2147483648);
SELECT ty_ret_float4(1.25::float8), ty_ret_bool(true), ty_ret_bool(1), ty_ret_bool(0);
SELECT ty_ret_bool(2);
SELECT ty_ret_bool('t'::text), ty_ret_bool('false'::text), ty_ret_bool('1'::text);
SELECT ty_ret_bool('yes'::text);
SELECT ty_ret_text('x'::text), ty_ret_numeric(12), ty_ret_numeric(0.5::float8),
       ty_ret_numeric('1.10'::text);
SELECT ty_ret_numeric('abc'::text);

-- nil is null
CREATE FUNCTION ty_nil() RETURNS int LANGUAGE pl_lua AS $$ return nil $$;
CREATE FUNCTION ty_none() RETURNS int LANGUAGE pl_lua AS $$ $$;
SELECT ty_nil() IS NULL, ty_none() IS NULL;

-- strings are converted with the input function of the type
CREATE FUNCTION ty_date() RETURNS date LANGUAGE pl_lua AS $$ return "2024-02-29" $$;
CREATE FUNCTION ty_baddate() RETURNS date LANGUAGE pl_lua AS $$ return "2024-02-30" $$;
CREATE FUNCTION ty_embedded_zero() RETURNS date LANGUAGE pl_lua AS $$ return "2024-02-29\0" $$;
SELECT ty_date();
SELECT ty_baddate();
SELECT ty_embedded_zero();
CREATE FUNCTION ty_tab() RETURNS date LANGUAGE pl_lua AS $$ return {} $$;
SELECT ty_tab();
CREATE FUNCTION ty_func() RETURNS int LANGUAGE pl_lua AS $$ return print $$;
SELECT ty_func();

-- enums
CREATE TYPE ty_color AS ENUM ('red', 'green');
CREATE FUNCTION ty_enum(c ty_color) RETURNS ty_color LANGUAGE pl_lua AS $$
  return c == 'red' and 'green' or 'red'
$$;
SELECT ty_enum('red'), ty_enum('green');
CREATE FUNCTION ty_badenum() RETURNS ty_color LANGUAGE pl_lua AS $$ return 'blue' $$;
SELECT ty_badenum();

-- domains are converted as their base types, with constraints checked
CREATE DOMAIN ty_posint AS integer CHECK (VALUE > 0);
CREATE DOMAIN ty_nn AS text NOT NULL;
CREATE FUNCTION ty_dom(v ty_posint) RETURNS ty_posint LANGUAGE pl_lua AS $$
  return v - 1
$$;
SELECT ty_dom(5);
SELECT ty_dom(1);
CREATE FUNCTION ty_domnull() RETURNS text LANGUAGE pl_lua AS $$
  return pcall(pgtype.ty_nn, nil) and "accepted" or "rejected"
$$;
SELECT ty_domnull();

-- type modifiers are applied
CREATE FUNCTION ty_vc() RETURNS varchar(3) LANGUAGE pl_lua AS $$
  return pgtype["varchar(3)"]("abcdef")
$$;
SELECT ty_vc();
CREATE TYPE ty_vcrow AS (a varchar(3), n numeric(5,2));
CREATE FUNCTION ty_vcrow(s text, n numeric) RETURNS ty_vcrow LANGUAGE pl_lua AS $$
  return { a = s, n = n }
$$;
SELECT * FROM ty_vcrow('abc', 1.234);
SELECT * FROM ty_vcrow('abcd', 1.234);
SELECT * FROM ty_vcrow('abc', 123456);

-- pgtype
CREATE FUNCTION ty_pgtype() RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = pgtype.integer:name()
  r[#r + 1] = pgtype["character varying(7)"]:name()
  r[#r + 1] = pgtype["varchar"]:name(9)
  r[#r + 1] = pgtype.array.integer:name()
  r[#r + 1] = pgtype.array["text"]:name()
  r[#r + 1] = tostring(pgtype.no_such_type)
  r[#r + 1] = tostring(pgtype.array.no_such_type)
  r[#r + 1] = tostring(pgtype(1))
  r[#r + 1] = pgtype.int4range:element():name()
  r[#r + 1] = pgtype.array.integer:element():name()
  r[#r + 1] = tostring(pgtype.integer)
  return table.concat(r, ",")
$$;
SELECT ty_pgtype();

CREATE FUNCTION ty_construct() RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(pgtype.integer(5))
  r[#r + 1] = tostring(pgtype.integer("6"))
  r[#r + 1] = tostring(pgtype.integer:fromstring("7"))
  r[#r + 1] = tostring(pgtype.date("2024-01-02"))
  r[#r + 1] = tostring(pgtype.text(pgtype.integer(5)))
  r[#r + 1] = tostring(pgtype.int8(pgtype.integer(5)))
  return table.concat(r, ",")
$$;
SELECT ty_construct();

CREATE FUNCTION ty_binary() RETURNS text LANGUAGE pl_lua AS $$
  local x = pgtype.int4:frombinary("\0\0\1\0")
  return tostring(x)
$$;
SELECT ty_binary();
CREATE FUNCTION ty_binary_bad() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.int4:frombinary("\0\0\1"))
$$;
SELECT ty_binary_bad();
CREATE FUNCTION ty_fromstring_bad() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.int4:fromstring("zz"))
$$;
SELECT ty_fromstring_bad();

-- bytea constructor and fromstring differ
CREATE FUNCTION ty_bytea_ctor() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.bytea("\\x41")) .. " " .. tostring(pgtype.bytea:fromstring("\\x41"))
$$;
SELECT ty_bytea_ctor();

-- argument types of polymorphic and ordinary arguments
CREATE FUNCTION ty_argtype(a anyelement, b int) RETURNS text LANGUAGE pl_lua AS $$
  return pgtype(a, 1):name() .. " " .. pgtype(b, 2):name() .. " " .. pgtype(nil, 0):name()
$$;
SELECT ty_argtype('x'::text, 1);
SELECT ty_argtype(1.5::numeric, 1);

-- casts: assignment casts are used, and need privileges
CREATE FUNCTION ty_cast() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.int8(pgtype.int4(5))) .. " " .. tostring(pgtype.text(pgtype.date("2024-01-02")))
$$;
SELECT ty_cast();
CREATE FUNCTION ty_badcast() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.int4(pgtype.date("2024-01-02")))
$$;
SELECT ty_badcast();

CREATE TYPE ty_cast_a AS (x int);
CREATE TYPE ty_cast_b AS (y int);
CREATE FUNCTION ty_cast_fn(ty_cast_a) RETURNS ty_cast_b LANGUAGE sql AS 'SELECT ROW($1.x * 2)::ty_cast_b';
CREATE CAST (ty_cast_a AS ty_cast_b) WITH FUNCTION ty_cast_fn(ty_cast_a) AS ASSIGNMENT;
CREATE FUNCTION ty_usecast() RETURNS text LANGUAGE pl_lua AS $$
  local a = pgtype.ty_cast_a({ x = 21 })
  return tostring(pgtype.ty_cast_b(a))
$$;
SELECT ty_usecast();
CREATE ROLE regress_ty_nopriv;
REVOKE EXECUTE ON FUNCTION ty_cast_fn(ty_cast_a) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ty_usecast() TO regress_ty_nopriv;
SET ROLE regress_ty_nopriv;
SELECT ty_usecast();
RESET ROLE;
REVOKE ALL ON FUNCTION ty_usecast() FROM regress_ty_nopriv;
DROP ROLE regress_ty_nopriv;

-- pgtype with a type name as the second argument
CREATE FUNCTION ty_pgtype_name() RETURNS text LANGUAGE pl_lua AS $$
  return pgtype(nil, "date"):name() .. " " .. tostring(pgtype(nil, "no_such_type"))
$$;
SELECT ty_pgtype_name();

-- datum objects compare with the equality and ordering of their type
CREATE FUNCTION ty_compare() RETURNS text LANGUAGE pl_lua AS $$
  local d1 = pgtype.uuid("00000000-0000-0000-0000-000000000001")
  local d2 = pgtype.uuid("00000000-0000-0000-0000-000000000002")
  local r = {}
  r[#r + 1] = tostring(d1 == pgtype.uuid("00000000-0000-0000-0000-000000000001"))
  r[#r + 1] = tostring(d1 == d2)
  r[#r + 1] = tostring(d1 ~= d2)
  r[#r + 1] = tostring(d1 < d2)
  r[#r + 1] = tostring(d2 <= d1)
  r[#r + 1] = tostring(d1 == 5)
  r[#r + 1] = tostring(pgtype.array.integer(1, 2) == pgtype.array.integer(1, 2))
  r[#r + 1] = tostring(pgtype.int4range(1, 3) == pgtype.int4range(1, 3))
  return table.concat(r, " ")
$$;
SELECT ty_compare();
CREATE FUNCTION ty_compare_bad() RETURNS boolean LANGUAGE pl_lua AS $$
  return pgtype.uuid("00000000-0000-0000-0000-000000000001") < pgtype.integer(1)
$$;
SELECT ty_compare_bad();

-- array coercions: relabeling, cast functions and I/O conversion
CREATE FUNCTION ty_arrcast() RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(pgtype.array.text(pgtype.array.varchar("foo", "bar")))
  r[#r + 1] = tostring(pgtype.array.text(pgtype.array.boolean(false, true)))
  r[#r + 1] = tostring(pgtype.array.text(pgtype.array.integer(10, 20)))
  r[#r + 1] = tostring(pgtype.array.bigint(pgtype.array.integer(10, 20)))
  return table.concat(r, " ")
$$;
SELECT ty_arrcast();

-- type modifiers of array columns apply to the elements
CREATE TABLE ty_atc (a varchar(10)[], b char(10)[]);
CREATE FUNCTION ty_atc1() RETURNS text LANGUAGE pl_lua AS $$
  local a = pgtype.array.varchar('foo', 'bar', 'value_too_long_for_type')
  return tostring(pgtype.ty_atc(a, nil))
$$;
SELECT ty_atc1();
CREATE FUNCTION ty_atc2() RETURNS text LANGUAGE pl_lua AS $$
  local a = pgtype.array.bpchar('foo', 'bar')
  return tostring(pgtype.ty_atc(nil, a))
$$;
SELECT ty_atc2();
CREATE DOMAIN ty_atc_dom AS varchar(3)[];
CREATE FUNCTION ty_atc3() RETURNS text LANGUAGE pl_lua AS $$
  local r = { tostring(pgtype.ty_atc_dom(pgtype.array.text('ab', 'cd'))) }
  r[#r + 1] = tostring(pcall(pgtype.ty_atc_dom, pgtype.array.text('ab', 'cdef')))
  return table.concat(r, " ")
$$;
SELECT ty_atc3();

-- a value of the element type, or a number, becomes a one-element array
CREATE FUNCTION ty_promote() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.array.integer(pgtype.integer(1))) .. " " ..
         tostring(pgtype.array.integer(5))
$$;
SELECT ty_promote();
CREATE FUNCTION ty_ret_elem() RETURNS int[] LANGUAGE pl_lua AS $$
  return pgtype.integer(1)
$$;
SELECT ty_ret_elem();

-- length coercion functions need EXECUTE privilege for the current user
CREATE ROLE regress_ty_nopriv2;
CREATE TYPE ty_lenrow AS (a varchar(3));
CREATE FUNCTION ty_lencheck() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.ty_lenrow({ a = "abc" }))
$$;
CREATE FUNCTION ty_lencheck_arr() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype["varchar(3)[]"]({ "abc" }))
$$;
CREATE DOMAIN ty_lendom AS varchar(3);
CREATE FUNCTION ty_lencheck_dom() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.ty_lendom("abc"))
$$;
GRANT EXECUTE ON FUNCTION ty_lencheck(), ty_lencheck_arr(), ty_lencheck_dom()
  TO regress_ty_nopriv2;
SELECT ty_lencheck();
REVOKE EXECUTE ON FUNCTION pg_catalog.varchar(varchar, integer, boolean) FROM PUBLIC;
SET ROLE regress_ty_nopriv2;
SELECT ty_lencheck();
SELECT ty_lencheck_arr();
SELECT ty_lencheck_dom();
RESET ROLE;
GRANT EXECUTE ON FUNCTION pg_catalog.varchar(varchar, integer, boolean) TO PUBLIC;
REVOKE ALL ON FUNCTION ty_lencheck(), ty_lencheck_arr(), ty_lencheck_dom()
  FROM regress_ty_nopriv2;
DROP ROLE regress_ty_nopriv2;

-- comparison of datum objects: same type only, using the type's ordering
DO LANGUAGE pl_lua $$
  local int4 = pgtype.array.integer
  print(int4(1, 2) == int4(1, 2), int4(1, 2) == int4(1, 3), int4(1, 2) < int4(1, 3))
  print(pgtype.numeric(1) < pgtype.numeric(2), pgtype.numeric(2) == pgtype.numeric('2.0'))
  print(pgtype.numeric(1) == pgtype.array.integer(1), pcall(function() return pgtype.numeric(1) < int4(1) end))
$$;
