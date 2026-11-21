--
-- Basic function, procedure and DO block behavior
--
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
CREATE EXTENSION IF NOT EXISTS pl_luau;
RESET client_min_messages;

\set VERBOSITY terse

DO LANGUAGE pl_lua $$ print("hello from the trusted language") $$;
DO LANGUAGE pl_luau $$ print("hello from the untrusted language") $$;

-- named arguments, unnamed arguments, and mixtures
CREATE FUNCTION add2(a integer, b integer) RETURNS integer
LANGUAGE pl_lua AS $$ return a + b $$;

CREATE FUNCTION mul2(integer, integer) RETURNS integer
LANGUAGE pl_lua AS $$
  local a, b = ...
  return a * b
$$;

CREATE FUNCTION named_then_unnamed(x integer, integer, integer) RETURNS text
LANGUAGE pl_lua AS $$
  local y, z = ...
  return x .. "," .. y .. "," .. z
$$;

SELECT add2(1, 2), mul2(6, 7), named_then_unnamed(1, 2, 3);

-- a named argument may not follow an unnamed one
CREATE FUNCTION bad_args(integer, b integer) RETURNS integer
LANGUAGE pl_lua AS $$ return b $$;

-- argument names must be usable as Lua names
CREATE FUNCTION bad_name("end" integer) RETURNS integer
LANGUAGE pl_lua AS $$ return 1 $$;
CREATE FUNCTION bad_name2("a b" integer) RETURNS integer
LANGUAGE pl_lua AS $$ return 1 $$;

-- nulls
CREATE FUNCTION null_in_out(a integer) RETURNS integer
LANGUAGE pl_lua AS $$
  if a == nil then return -1 end
  return nil
$$;
SELECT null_in_out(NULL), null_in_out(1);

CREATE FUNCTION no_result() RETURNS integer
LANGUAGE pl_lua AS $$ local x = 1 $$;
SELECT no_result() IS NULL;

-- STRICT functions are not called for null input
CREATE FUNCTION strict_fn(a integer) RETURNS text STRICT
LANGUAGE pl_lua AS $$ return "called with " .. a $$;
SELECT strict_fn(NULL), strict_fn(5);

-- void functions
CREATE FUNCTION void_fn() RETURNS void
LANGUAGE pl_lua AS $$ return 42 $$;
SELECT void_fn();

-- composite results: by name, by value, and by output parameters
CREATE TYPE basic_pair AS (a integer, b text);

CREATE FUNCTION pair_by_name() RETURNS basic_pair
LANGUAGE pl_lua AS $$ return { a = 1, b = "one" } $$;
CREATE FUNCTION pair_by_value() RETURNS basic_pair
LANGUAGE pl_lua AS $$ return 2, "two" $$;
CREATE FUNCTION pair_short() RETURNS basic_pair
LANGUAGE pl_lua AS $$ return 3 $$;
CREATE FUNCTION pair_long() RETURNS basic_pair
LANGUAGE pl_lua AS $$ return 3, "three", "extra" $$;
CREATE FUNCTION pair_null() RETURNS basic_pair
LANGUAGE pl_lua AS $$ return nil $$;
CREATE FUNCTION out_params(OUT n integer, OUT s text)
LANGUAGE pl_lua AS $$ return 10, "ten" $$;

SELECT * FROM pair_by_name();
SELECT * FROM pair_by_value();
SELECT * FROM pair_short();
SELECT * FROM pair_long();
SELECT pair_null() IS NULL;
SELECT * FROM out_params();

-- records
CREATE FUNCTION rec_fn() RETURNS record
LANGUAGE pl_lua AS $$ return 1, "x" $$;
SELECT * FROM rec_fn() AS t(a integer, b text);

-- polymorphic functions
CREATE FUNCTION poly_type(anyelement) RETURNS text
LANGUAGE pl_lua AS $$ return pgtype(..., 1):name() $$;
SELECT poly_type(1), poly_type('x'::text), poly_type(1.5::numeric);

CREATE FUNCTION poly_ret(a anyelement) RETURNS anyelement
LANGUAGE pl_lua AS $$ return a $$;
SELECT poly_ret(42), poly_ret('abc'::text);

-- variadic functions
CREATE FUNCTION variadic_sum(VARIADIC nums integer[]) RETURNS integer
LANGUAGE pl_lua AS $$
  local t = 0
  for i, v in ipairs(nums) do t = t + v end
  return t
$$;
SELECT variadic_sum(1, 2, 3), variadic_sum(VARIADIC ARRAY[]::integer[]);

CREATE FUNCTION variadic_any(VARIADIC "any") RETURNS text
LANGUAGE pl_lua AS $$
  return select('#', ...) .. " args"
$$;
SELECT variadic_any(1, 'a', true);

-- each function has its own environment for undeclared variables
CREATE FUNCTION counter1() RETURNS integer
LANGUAGE pl_lua AS $$
  n = (n or 0) + 1
  return n
$$;
CREATE FUNCTION counter2() RETURNS integer
LANGUAGE pl_lua AS $$
  n = (n or 100) + 1
  return n
$$;
SELECT counter1(), counter1(), counter2(), counter1(), counter2();

-- sharing data through _G
CREATE FUNCTION set_shared(v text) RETURNS void
LANGUAGE pl_lua AS $$ _G.shared_value = v $$;
CREATE FUNCTION get_shared() RETURNS text
LANGUAGE pl_lua AS $$ return shared_value $$;
SELECT set_shared('hello'), get_shared();

-- one-time initialization
CREATE FUNCTION init_once() RETURNS integer
LANGUAGE pl_lua AS $$
  calls = calls + 1
  return calls
end
do
  calls = 0
$$;
SELECT init_once(), init_once(), init_once();

-- redefinition is noticed
CREATE FUNCTION redefine() RETURNS text LANGUAGE pl_lua AS $$ return 'one' $$;
SELECT redefine();
CREATE OR REPLACE FUNCTION redefine() RETURNS text LANGUAGE pl_lua AS $$ return 'two' $$;
SELECT redefine();

-- syntax errors are reported when the function is created
CREATE FUNCTION syntax_error() RETURNS integer
LANGUAGE pl_lua AS $$ return return $$;
SET check_function_bodies = off;
CREATE FUNCTION syntax_error() RETURNS integer
LANGUAGE pl_lua AS $$ return return $$;
RESET check_function_bodies;
SELECT syntax_error();

-- runtime errors
CREATE FUNCTION runtime_error() RETURNS integer
LANGUAGE pl_lua AS $$
  local x = nil
  return x.field
$$;
SELECT runtime_error();

CREATE FUNCTION error_with_string() RETURNS integer
LANGUAGE pl_lua AS $$ error("custom message") $$;
SELECT error_with_string();

CREATE FUNCTION error_with_table() RETURNS integer
LANGUAGE pl_lua AS $$ error({ code = 1 }) $$;
SELECT error_with_table();

-- unsupported types
CREATE FUNCTION bad_ret() RETURNS internal
LANGUAGE pl_lua AS $$ return nil $$;
CREATE FUNCTION bad_arg(a internal) RETURNS integer
LANGUAGE pl_lua AS $$ return 1 $$;

-- trigger and event trigger functions cannot be called directly
CREATE FUNCTION trg() RETURNS trigger LANGUAGE pl_lua AS $$ return nil $$;
SELECT trg();
CREATE FUNCTION evtrg() RETURNS event_trigger LANGUAGE pl_lua AS $$ return nil $$;
SELECT evtrg();

-- procedures
CREATE TABLE basic_log (msg text);

CREATE PROCEDURE log_it(m text) LANGUAGE pl_lua AS $$
  spi.execute("INSERT INTO basic_log VALUES ($1)", m)
$$;
CALL log_it('first');
CALL log_it('second');
SELECT * FROM basic_log ORDER BY msg;

CREATE PROCEDURE proc_inout(INOUT a integer, INOUT b text, OUT c integer)
LANGUAGE pl_lua AS $$ return a + 1, b .. "!", a * 2 $$;
CALL proc_inout(1, 'x', NULL);

-- DO blocks run in a fresh environment
DO LANGUAGE pl_lua $$ local_var_in_do = 1; print(local_var_in_do) $$;
DO LANGUAGE pl_lua $$ print(local_var_in_do) $$;

-- a DO block with a syntax error
DO LANGUAGE pl_lua $$ this is not lua $$;
DO LANGUAGE pl_lua $$ error("failed in DO") $$;

-- function without a trailing newline in a comment
CREATE FUNCTION trailing_comment() RETURNS integer
LANGUAGE pl_lua AS $$ return 7 -- done $$;
SELECT trailing_comment();

-- line numbers in messages match the body
CREATE FUNCTION line_numbers() RETURNS integer
LANGUAGE pl_lua AS $$
  local a = 1

  error("on line 4")
$$;
SELECT line_numbers();

-- nested calls through SPI
CREATE FUNCTION nested_inner(n integer) RETURNS integer
LANGUAGE pl_lua AS $$ return n * 2 $$;
CREATE FUNCTION nested_outer(n integer) RETURNS integer
LANGUAGE pl_lua AS $$
  return spi.execute("SELECT nested_inner($1) AS v", n)[1].v + 1
$$;
SELECT nested_outer(5);

-- _G, _VERSION and friends
DO LANGUAGE pl_lua $$
  print(_VERSION:match("^Lua 5%.[45]$") ~= nil)
  print(type(_PLVERSION), type(_PG_VERSION), type(_PG_VERSION_NUM))
  print(_G == _ENV, type(_PL_LOAD_TIME), _PL_IDENT)
$$;

-- several values returned for an array result make an array
CREATE FUNCTION arr_from_values() RETURNS integer[] LANGUAGE pl_lua AS $$ return 1, 2, 3 $$;
CREATE FUNCTION arr_from_table() RETURNS integer[] LANGUAGE pl_lua AS $$ return { 4, 5 } $$;
CREATE FUNCTION arr_from_none() RETURNS integer[] LANGUAGE pl_lua AS $$ return $$;
SELECT arr_from_values(), arr_from_table(), arr_from_none() IS NULL;

-- function names that are not names in Lua
CREATE FUNCTION "weird name"() RETURNS text LANGUAGE pl_lua AS $$ return 'a' $$;
CREATE FUNCTION "1starts_with_a_digit"() RETURNS text LANGUAGE pl_lua AS $$ return 'b' $$;
CREATE FUNCTION "end"() RETURNS text LANGUAGE pl_lua AS $$ return 'c' $$;
CREATE FUNCTION "a-b.c"() RETURNS text LANGUAGE pl_lua AS $$ return 'd' $$;
CREATE FUNCTION "ünïcode"() RETURNS text LANGUAGE pl_lua AS $$ return 'e' $$;
CREATE FUNCTION "weird name"(integer) RETURNS text LANGUAGE pl_lua AS $$ error("in a weird name") $$;
SELECT "weird name"(), "1starts_with_a_digit"(), "end"(), "a-b.c"();
SELECT "ünïcode"() AS unicode_name;
SELECT "weird name"(1);

-- a record result needs a column definition list
SELECT rec_fn();

-- the settings for the extra garbage collection, for the memory that lives
-- outside Lua, do not change what functions do
SET pl_lua.extra_gc_threshold = 1;
SET pl_lua.extra_gc_multiplier = 1;
CREATE FUNCTION gc_settings(n integer) RETURNS integer LANGUAGE pl_lua AS $$
  for i = 1, n do
    local st = spi.prepare("select $1::int + " .. i, { 'integer' })
    assert(st(1)[1]['?column?'] == 1 + i)
  end
  return n
$$;
SELECT gc_settings(50);
SET pl_lua.extra_gc_multiplier = 1000000;
SELECT gc_settings(20);
SET pl_lua.extra_gc_multiplier = 0;
SELECT gc_settings(5);
RESET pl_lua.extra_gc_threshold;
RESET pl_lua.extra_gc_multiplier;
