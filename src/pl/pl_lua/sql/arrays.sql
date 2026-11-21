--
-- Array and range values
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

-- reading elements
CREATE FUNCTION ar_get(a int[], i int) RETURNS int LANGUAGE pl_lua AS $$
  return a[i]
$$;
SELECT ar_get('{10,20,30}', 2), ar_get('{10,20,30}', 4) IS NULL, ar_get('{10,NULL,30}', 2) IS NULL;
SELECT ar_get('[0:2]={10,20,30}', 0), ar_get('[0:2]={10,20,30}', 2);

-- length, iteration
CREATE FUNCTION ar_iter(a text[]) RETURNS text LANGUAGE pl_lua AS $$
  local r = { "#" .. #a }
  for i, v in pairs(a) do r[#r + 1] = i .. "=" .. tostring(v) end
  for i, v in ipairs(a) do r[#r + 1] = "i" .. i .. "=" .. v end
  return table.concat(r, " ")
$$;
SELECT ar_iter('{a,b,c}'), ar_iter('{}'), ar_iter('{a,NULL,c}');

-- multidimensional access
CREATE FUNCTION ar_md(a int[]) RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(a[2][3])
  r[#r + 1] = tostring(a[1][1])
  r[#r + 1] = tostring(a[3][1])
  r[#r + 1] = tostring(#a) .. "x" .. tostring(#a[1])
  for i, row in pairs(a) do
    for j, v in pairs(row) do r[#r + 1] = i .. "," .. j .. "=" .. v end
  end
  return table.concat(r, " ")
$$;
SELECT ar_md('{{1,2,3},{4,5,6}}');

-- assignment, including extending a one-dimensional array
CREATE FUNCTION ar_set(a int[]) RETURNS int[] LANGUAGE pl_lua AS $$
  a[2] = 99
  a[5] = 5
  return a
$$;
SELECT ar_set('{1,2,3}'), ar_set('{}');
CREATE FUNCTION ar_set_md(a int[]) RETURNS int[] LANGUAGE pl_lua AS $$
  a[2][1] = 40
  a[1][2] = nil
  return a
$$;
SELECT ar_set_md('{{1,2},{3,4}}');
CREATE FUNCTION ar_set_bad(a int[]) RETURNS int[] LANGUAGE pl_lua AS $$
  a[1] = "x"
  return a
$$;
SELECT ar_set_bad('{1,2}');
CREATE FUNCTION ar_set_few(a int[]) RETURNS int[] LANGUAGE pl_lua AS $$
  a[1] = 1
  return a
$$;
SELECT ar_set_few('{{1,2},{3,4}}');

-- conversion to Lua tables
CREATE FUNCTION ar_table(a int[]) RETURNS text LANGUAGE pl_lua AS $$
  local t = a()
  local r = {}
  for i = 1, 4 do r[#r + 1] = tostring(t[i]) end
  local t2 = a{ null = -1 }
  for i = 1, 4 do r[#r + 1] = tostring(t2[i]) end
  local t3 = a(function(v, arr, i) return v and v * 10 end)
  for i = 1, 4 do r[#r + 1] = tostring(t3[i]) end
  local t4 = a(0)
  for i = 1, 4 do r[#r + 1] = tostring(t4[i]) end
  return table.concat(r, " ")
$$;
SELECT ar_table('{1,NULL,3,4}');
CREATE FUNCTION ar_sum(a int[]) RETURNS int LANGUAGE pl_lua AS $$
  local total = 0
  a{ null = 0, map = function(v) total = total + v end, discard = true }
  return total
$$;
SELECT ar_sum('{1,2,NULL,4}'), ar_sum('{{1,2},{3,4}}'), ar_sum('{}');
CREATE FUNCTION ar_nested(a int[]) RETURNS text LANGUAGE pl_lua AS $$
  local t = a()
  return #t .. ":" .. #t[1] .. ":" .. t[2][2]
$$;
SELECT ar_nested('{{1,2,3},{4,5,6}}');
CREATE FUNCTION ar_mapidx(a int[]) RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  a{ map = function(v, arr, i, j) r[#r + 1] = i .. j .. "=" .. v end, discard = true }
  return table.concat(r, " ")
$$;
SELECT ar_mapidx('{{1,2},{3,4}}');

-- constructing arrays
CREATE FUNCTION ar_make() RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(pgtype.array.integer())
  r[#r + 1] = tostring(pgtype.array.integer(1, 2, 3))
  r[#r + 1] = tostring(pgtype.array.integer({ 1, 2, nil, 4 }))
  r[#r + 1] = tostring(pgtype.array.integer({ { 1, 2 }, { 3, 4 } }, 2, 2))
  r[#r + 1] = tostring(pgtype.array.text({ "a", "b" }, 2))
  r[#r + 1] = tostring(pgtype.array.integer({}))
  r[#r + 1] = tostring(pgtype.array.integer({ { 1, 2 }, { nil, 4 }, { 5, 6 } }, 3, 2))
  r[#r + 1] = tostring(pgtype.array.integer({ { 1, 2 } }, 2, 2))
  return table.concat(r, " ")
$$;
SELECT ar_make();
CREATE FUNCTION ar_make_bad() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.array.integer({ { 1, 2 }, 3 }, 2, 2))
$$;
SELECT ar_make_bad();

-- returning tables as arrays
CREATE FUNCTION ar_ret() RETURNS int[] LANGUAGE pl_lua AS $$
  return { 1, 2, 3 }
$$;
SELECT ar_ret();
CREATE FUNCTION ar_ret_text() RETURNS text[] LANGUAGE pl_lua AS $$
  return { "x", nil, "z" }
$$;
SELECT ar_ret_text();

-- arrays of composite types
CREATE TYPE ar_pair AS (k text, v int);
CREATE FUNCTION ar_pairs() RETURNS ar_pair[] LANGUAGE pl_lua AS $$
  return { { k = "a", v = 1 }, { k = "b", v = 2 } }
$$;
SELECT ar_pairs();
CREATE FUNCTION ar_pairs_in(p ar_pair[]) RETURNS text LANGUAGE pl_lua AS $$
  return p[2].k .. p[2].v
$$;
SELECT ar_pairs_in(ar_pairs());

-- ranges
CREATE FUNCTION rg_fields(r int4range) RETURNS text LANGUAGE pl_lua AS $$
  return table.concat({ tostring(r.lower), tostring(r.upper), tostring(r.lower_inc),
    tostring(r.upper_inc), tostring(r.lower_inf), tostring(r.upper_inf),
    tostring(r.isempty) }, " ")
$$;
SELECT rg_fields('[1,5)'), rg_fields('(,7]'), rg_fields('empty'), rg_fields('[3,)');
CREATE FUNCTION rg_make() RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(pgtype.int4range())
  r[#r + 1] = tostring(pgtype.int4range("[1,3]"))
  r[#r + 1] = tostring(pgtype.int4range(1, 5))
  r[#r + 1] = tostring(pgtype.int4range(1, 5, "[]"))
  r[#r + 1] = tostring(pgtype.int4range(1, 5, "()"))
  r[#r + 1] = tostring(pgtype.int4range(nil, 5))
  r[#r + 1] = tostring(pgtype.int4range(1, nil, "(]"))
  r[#r + 1] = tostring(pgtype.numrange(1.5, 2.5, "[]"))
  return table.concat(r, " ")
$$;
SELECT rg_make();
CREATE FUNCTION rg_bad() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.int4range(5, 1))
$$;
SELECT rg_bad();
CREATE FUNCTION rg_badflags() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.int4range(1, 5, "x"))
$$;
SELECT rg_badflags();
CREATE FUNCTION rg_ret() RETURNS int4range LANGUAGE pl_lua AS $$
  return pgtype.int4range(2, 4)
$$;
SELECT rg_ret();
CREATE FUNCTION rg_set(r int4range) RETURNS text LANGUAGE pl_lua AS $$
  r.lower = 1
$$;
SELECT rg_set('[1,2)');

-- arrays of variable-length elements: reads, nulls, and reads after writes
CREATE FUNCTION arr_text_walk(a text[]) RETURNS text
LANGUAGE pl_lua AS $$
  local out = {}
  for i = 1, #a do out[#out + 1] = tostring(a[i]) end
  print(a[0], a[#a + 1])
  a[2] = 'changed, and longer'
  a[#a + 1] = 'appended'
  for i = 1, #a do out[#out + 1] = tostring(a[i]) end
  return table.concat(out, ',')
$$;
SELECT arr_text_walk(ARRAY['a', NULL, 'ccc', 'dddd', NULL]);

-- a nonstandard lower bound
CREATE FUNCTION arr_text_bounds(a text[]) RETURNS text
LANGUAGE pl_lua AS $$
  return tostring(a[3]) .. tostring(a[4]) .. tostring(a[5]) .. tostring(a[6])
$$;
SELECT arr_text_bounds('[4:5]={x,yy}'::text[]);

-- a long array is walked in reasonable time
CREATE FUNCTION arr_text_many(a text[]) RETURNS bigint
LANGUAGE pl_lua AS $$
  local n = 0
  for i = 1, #a do n = n + #a[i] end
  for _, v in pairs(a) do n = n + #v end
  return n
$$;
SELECT arr_text_many(array_agg(i::text)) FROM generate_series(1, 30000) i;

-- errors of subscripts and sizes
DO LANGUAGE pl_lua $$
  local a = pgtype.array.integer(1, 2, 3)
  print(pcall(function() a[2^40] = 1 end))
  print(pcall(function() return a[1][1] end))
  print(pcall(function() a[1][1] = 5 end))
  print(pcall(function() return a[2^40] end))
  print(pcall(pgtype.array.integer, { 1 }, 1, 2, 3, 4, 5, 6, 7))
  print(pcall(pgtype.array.integer, { 1 }, 2^31, 2^31))
  print(pcall(pgtype.array.integer, { 1 }, 1e9, 1e9))
  local m = pgtype.array.integer({ { 1, 2 }, { 3, 4 } }, 2, 2)
  print(pcall(function() m[1] = 5 end))
  print(pcall(function() return m[1][2][3] end))
$$;

-- the iterator from pairs() can be called with anything
DO LANGUAGE pl_lua $$
  local f = pairs(pgtype['integer[]']({1, 2}))
  print(pcall(f, pgtype.integer(5)))
  print(pcall(f, pgtype.text('abc')))
  print(pcall(f, pgtype.numeric('1.5')))
  print(pcall(f, nil))
  print(pcall(f, {}))
$$;
