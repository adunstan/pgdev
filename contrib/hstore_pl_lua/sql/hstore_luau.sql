--
-- The hstore transform for pl_luau
--
\set VERBOSITY terse

-- without the transform, hstore values are datum objects
CREATE FUNCTION hsu_plain(h hstore) RETURNS text LANGUAGE pl_luau AS $$
  return type(h) .. " " .. tostring(h)
$$;
SELECT hsu_plain('a=>1, b=>2');

CREATE FUNCTION hsu_type(h hstore) RETURNS text TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  return type(h)
$$;
SELECT hsu_type('a=>1');

-- hstore to table
CREATE FUNCTION hsu_describe(h hstore) RETURNS text TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  local items = {}
  for k, v in pairs(h) do items[#items + 1] = k .. "=" .. v end
  table.sort(items)
  return table.concat(items, ",")
$$;
SELECT hsu_describe('a=>1, b=>2, c=>three');
SELECT hsu_describe('');
SELECT hsu_describe('x=>NULL, y=>2');

-- table to hstore
CREATE FUNCTION hsu_make() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  return { foo = "bar", num = 42, flag = true, [7] = "seven", frac = 1.5 }
$$;
SELECT hsu_make();

CREATE FUNCTION hsu_empty() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$ return {} $$;
SELECT hsu_empty();

-- round trip
CREATE FUNCTION hsu_roundtrip(h hstore) RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$ return h $$;
SELECT hsu_roundtrip('a=>1, b=>2, "key with spaces"=>"value, with => stuff"');
SELECT hsu_roundtrip('') = ''::hstore;

-- unusual contents
SELECT hsu_roundtrip('"" => "empty key"');
SELECT hsu_roundtrip('k => ""');
SELECT hsu_roundtrip(format('nl => "a%sb"', chr(10))::hstore);

-- null values cannot be represented in a table, so they are dropped
SELECT hsu_roundtrip('a=>1, b=>NULL');

-- values that cannot be converted
CREATE FUNCTION hsu_bad_value() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$ return { a = {} } $$;
SELECT hsu_bad_value();
CREATE FUNCTION hsu_bad_key() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$ return { [true] = "x" } $$;
SELECT hsu_bad_key();
CREATE FUNCTION hsu_not_table(n integer) RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  if n == 1 then return 123 end
  return function() end
$$;
SELECT hsu_not_table(1);
SELECT hsu_not_table(2);

-- the transform applies to query parameters and results, arrays and columns
CREATE FUNCTION hsu_spi() RETURNS text TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  local r = spi.execute("SELECT $1 || 'z=>26'::hstore AS h, $1 -> 'a' AS a", { a = "1" })[1]
  return type(r.h) .. " " .. r.h.z .. " " .. r.a
$$;
SELECT hsu_spi();

CREATE FUNCTION hsu_array(hs hstore[]) RETURNS text TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  return type(hs[1]) .. " " .. hs[2].k
$$;
SELECT hsu_array(ARRAY['a=>1', 'k=>v']::hstore[]);

CREATE TYPE hsu_comp AS (id integer, data hstore);
CREATE FUNCTION hsu_comp_fn(c hsu_comp) RETURNS hsu_comp TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  c.data = { changed = "yes" }
  return c
$$;
SELECT hsu_comp_fn(ROW(1, 'orig=>1')::hsu_comp);

-- set-returning functions
CREATE FUNCTION hsu_srf() RETURNS SETOF hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  coroutine.yield({ n = 1 })
  coroutine.yield({ n = 2 })
$$;
SELECT * FROM hsu_srf();

-- DO blocks have no transform
DO LANGUAGE pl_luau $$
  local r = spi.execute("SELECT 'a=>1'::hstore AS h")[1]
  print(type(r.h))
$$;

-- many entries
CREATE FUNCTION hsu_many(n integer) RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  local t = {}
  for i = 1, n do t["key" .. i] = tostring(i) end
  return t
$$;
SELECT count(*), sum(value::integer) FROM each(hsu_many(500));

-- a __tostring metamethod that adds entries while the table is converted
CREATE FUNCTION hsu_grow() RETURNS hstore TRANSFORM FOR TYPE hstore
LANGUAGE pl_luau AS $$
  local d = pgtype.integer(1)
  local mt = debug.getmetatable(d)
  local orig = mt.__tostring
  local t = { a = d, b = 'x' }
  mt.__tostring = function(v)
    for i = 1, 100 do t["new" .. i] = i end
    return orig(v)
  end
  return t
$$;
SELECT hsu_grow();
