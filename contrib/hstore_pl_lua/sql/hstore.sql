--
-- The hstore transform for pl_lua
--
\set VERBOSITY terse

-- without the transform, hstore values are datum objects
CREATE FUNCTION hs_plain(h hstore) RETURNS text LANGUAGE pl_lua AS $$
  return type(h) .. " " .. tostring(h)
$$;
SELECT hs_plain('a=>1, b=>2');

CREATE FUNCTION hs_type(h hstore) RETURNS text TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  return type(h)
$$;
SELECT hs_type('a=>1');

-- hstore to table
CREATE FUNCTION hs_describe(h hstore) RETURNS text TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  local items = {}
  for k, v in pairs(h) do items[#items + 1] = k .. "=" .. v end
  table.sort(items)
  return table.concat(items, ",")
$$;
SELECT hs_describe('a=>1, b=>2, c=>three');
SELECT hs_describe('');
SELECT hs_describe('x=>NULL, y=>2');

-- table to hstore
CREATE FUNCTION hs_make() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  return { foo = "bar", num = 42, flag = true, [7] = "seven", frac = 1.5 }
$$;
SELECT hs_make();

CREATE FUNCTION hs_empty() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$ return {} $$;
SELECT hs_empty();

-- round trip
CREATE FUNCTION hs_roundtrip(h hstore) RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$ return h $$;
SELECT hs_roundtrip('a=>1, b=>2, "key with spaces"=>"value, with => stuff"');
SELECT hs_roundtrip('') = ''::hstore;

-- unusual contents
SELECT hs_roundtrip('"" => "empty key"');
SELECT hs_roundtrip('k => ""');
SELECT hs_roundtrip(format('nl => "a%sb"', chr(10))::hstore);

-- null values cannot be represented in a table, so they are dropped
SELECT hs_roundtrip('a=>1, b=>NULL');

-- values that cannot be converted
CREATE FUNCTION hs_bad_value() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$ return { a = {} } $$;
SELECT hs_bad_value();
CREATE FUNCTION hs_bad_key() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$ return { [true] = "x" } $$;
SELECT hs_bad_key();
CREATE FUNCTION hs_not_table(n integer) RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  if n == 1 then return 123 end
  return function() end
$$;
SELECT hs_not_table(1);
SELECT hs_not_table(2);

-- the transform applies to query parameters and results, arrays and columns
CREATE FUNCTION hs_spi() RETURNS text TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  local r = spi.execute("SELECT $1 || 'z=>26'::hstore AS h, $1 -> 'a' AS a", { a = "1" })[1]
  return type(r.h) .. " " .. r.h.z .. " " .. r.a
$$;
SELECT hs_spi();

CREATE FUNCTION hs_array(hs hstore[]) RETURNS text TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  return type(hs[1]) .. " " .. hs[2].k
$$;
SELECT hs_array(ARRAY['a=>1', 'k=>v']::hstore[]);

CREATE TYPE hs_comp AS (id integer, data hstore);
CREATE FUNCTION hs_comp_fn(c hs_comp) RETURNS hs_comp TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  c.data = { changed = "yes" }
  return c
$$;
SELECT hs_comp_fn(ROW(1, 'orig=>1')::hs_comp);

-- set-returning functions
CREATE FUNCTION hs_srf() RETURNS SETOF hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  coroutine.yield({ n = 1 })
  coroutine.yield({ n = 2 })
$$;
SELECT * FROM hs_srf();

-- DO blocks have no transform
DO LANGUAGE pl_lua $$
  local r = spi.execute("SELECT 'a=>1'::hstore AS h")[1]
  print(type(r.h))
$$;

-- many entries
CREATE FUNCTION hs_many(n integer) RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_lua AS $$
  local t = {}
  for i = 1, n do t["key" .. i] = tostring(i) end
  return t
$$;
SELECT count(*), sum(value::integer) FROM each(hs_many(500));
