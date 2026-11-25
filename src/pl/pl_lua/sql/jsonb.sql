--
-- jsonb values: conversion to and from Lua
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

-- Lua table to jsonb: arrays, objects, nulls and the conversion options,
-- which a function returning jsonb gives as a second result
CREATE FUNCTION jconv(opts text) RETURNS jsonb LANGUAGE pl_lua AS $$
  local src = {
    s = "a string",
    n = 42,
    f = 1.5,
    t = true,
    list = { "x", "y", "z" },
    holes = { nil, nil, 3, 4, nil, 6, [12] = "twelve" },
    empty = {},
    nested = { { 1, { 2 } }, { k = { 3 } } },
  }
  local options = load("return " .. opts)()
  return src, options
$$;
SELECT jconv('{}');
SELECT jconv('{ empty_object = true }');
SELECT jconv('{ array_thresh = 1 }');
SELECT jconv('{ array_frac = 1 }');

CREATE FUNCTION jconv2(val text, opts text) RETURNS jsonb LANGUAGE pl_lua AS $$
  return load("return " .. val)(), load("return " .. opts)()
$$;
SELECT jconv2('{ a = false, b = true }', '{ null = false }');
SELECT jconv2('{ 1, 2, 3 }', '{ map = function(v) if type(v) == "number" then return v * 10 end return v end }');
SELECT jconv2('{ 1, "two", { 3 } }', '{ map = function(v) if type(v) == "string" then return v:upper() end return v end }');

DO LANGUAGE pl_lua $$
  print(pgtype.jsonb({ s = "x", n = 1, l = { 1, 2 } }))
  print(pgtype.jsonb([["a string"]]), pgtype.jsonb(12), pgtype.jsonb(true), pgtype.jsonb({}))
$$;

-- jsonb to Lua, with pairs() and the mapping call
CREATE TEMP TABLE jt (id serial, a jsonb);
INSERT INTO jt (a) VALUES ('1'), ('"str"'), ('true'), ('null'),
  ('{"a": 1, "b": [true, null, "x"], "c": {"d": 2.5}}'),
  ('[10, 20, {"k": "v"}]'), ('{}'), ('[]');

DO LANGUAGE pl_lua $$
  local jsonb = require 'pl_lua.jsonb'
  for r in spi.rows("SELECT a FROM jt ORDER BY id") do
    print(r.a, jsonb.type(r.a))
    local keys = {}
    for k, v in pairs(r.a) do keys[#keys + 1] = tostring(k) .. "=" .. tostring(v) end
    table.sort(keys)
    print(table.concat(keys, " "))
  end
$$;

DO LANGUAGE pl_lua $$
  local jsonb = require 'pl_lua.jsonb'
  local v = pgtype.jsonb:fromstring('{"a": 1, "b": [true, null, "x"], "c": {"d": 2.5}}')
  local t = v()
  print(type(t), t.a, t.b[1], t.b[2], t.b[3], t.c.d)
  print(jsonb.is_object(t), jsonb.is_array(t.b), jsonb.is_array(t))

  -- null and pg_numeric options
  local nul = {}
  t = v{ null = nul, pg_numeric = true }
  print(t.b[2] == nul, pgtype(t.a):name(), tostring(t.c.d))

  -- norecurse leaves containers as jsonb values
  t = v{ norecurse = true }
  print(jsonb.type(t.b), jsonb.type(t.c), tostring(t.c))

  -- the map function sees keys, values and the path to the container
  v{ discard = true, map = function(k, val, ...)
       local path = table.concat({ ... }, "/")
       print("map", tostring(k), type(val), path)
       return k, val
     end }

  -- shorthand forms
  print(v(function(k, val) return k, val end).a, v(false).b[2])
$$;

-- round trip, with a unique value for null
CREATE FUNCTION jround(j jsonb) RETURNS jsonb LANGUAGE pl_lua AS $$
  local nul = {}
  return j{ null = nul }, { null = nul }
$$;
SELECT jround('{"foo":[1,null,false,{"a":null,"b":[]},{},[]]}');

-- marking tables as objects or arrays
DO LANGUAGE pl_lua $$
  local jsonb = require 'pl_lua.jsonb'
  local t = jsonb.set_as_object({})
  print(jsonb.is_object(t), jsonb.is_array(t), jsonb.is_object({}))
  print(pgtype.jsonb(t), pgtype.jsonb(jsonb.set_as_array({})))
  local u = jsonb.set_as_array({ [1] = "a", x = "b" })
  print(pgtype.jsonb({ u }))
  jsonb.set_as_unknown(u)
  print(jsonb.is_array(u))
  print(pcall(jsonb.set_as_object, setmetatable({}, {})))
$$;

-- other datum types are converted as to_jsonb would do it
DO LANGUAGE pl_lua $$
  local ts = pgtype.timestamp('2020-02-03 04:05:06')
  local inner = pgtype.jsonb:fromstring('{"x": [1, 2]}')
  print(pgtype.jsonb({ ts = ts, inner = inner, num = pgtype.numeric('1.50') }))
  print(pgtype.jsonb(pgtype.numeric('123456789012345678901234567890')))
$$;

-- values that cannot be converted
DO LANGUAGE pl_lua $$ print(pgtype.jsonb({ f = function() end })) $$;
DO LANGUAGE pl_lua $$ print(pgtype.jsonb({ [{}] = 1 })) $$;
DO LANGUAGE pl_lua $$ print(pgtype.jsonb(0/0)) $$;
DO LANGUAGE pl_lua $$ print(pgtype.jsonb({ "a\0b" })) $$;

-- jsonb arguments and results
CREATE FUNCTION jsonb_add(val jsonb) RETURNS jsonb LANGUAGE pl_lua AS $$
  local nullval = {}
  local t = val{ null = nullval, pg_numeric = true }
  t.newkey = { { foo = 1 }, { bar = 2 } }
  return t, { null = nullval }
$$;
SELECT jsonb_add('{"oldkey": [147573952589676412928, null]}');

CREATE FUNCTION jsonb_scalar(v jsonb) RETURNS text LANGUAGE pl_lua AS $$
  return require('pl_lua.jsonb').type(v) .. ":" .. tostring(v)
$$;
SELECT jsonb_scalar('1'), jsonb_scalar('"s"'), jsonb_scalar('null'),
       jsonb_scalar('[1]'), jsonb_scalar('{"a":1}');

-- large values, including some that are compressed or stored out of line
CREATE TEMP TABLE jbig (id integer, a jsonb);
INSERT INTO jbig SELECT i, ('[' || repeat('"foo",', 10 * (10 ^ i)::integer) || i || ']')::jsonb
  FROM generate_series(1, 5) i;
DO LANGUAGE pl_lua $$
  for r in spi.rows("SELECT a FROM jbig ORDER BY id") do
    local t = r.a()
    print(#t, t[#t])
  end
$$;

-- comparison of jsonb values
DO LANGUAGE pl_lua $$
  local a, b = pgtype.jsonb:fromstring('{"a": 1}'), pgtype.jsonb:fromstring('{"a": 2}')
  print(a == pgtype.jsonb:fromstring('{ "a" : 1 }'), a == b)
  print(a < b, a <= b, b < a)
  print(pgtype.jsonb:fromstring('[1,2]') < pgtype.jsonb:fromstring('[1,3]'))
$$;

-- extra arguments to the functions that mark a table do not change what
-- the marking is applied to: with one that was a string, number, boolean
-- or function, the metatable of every value of that type was replaced
DO LANGUAGE pl_lua $$
  local j = require 'pl_lua.jsonb'
  local t = {}
  for _, extra in ipairs { 'a string', 5, true, print, {} } do
    j.set_as_array(t, extra)
    j.set_as_object(t, extra)
    j.set_as_unknown(t, extra)
  end
  j.set_as_array(t, 'x')
  print(j.is_array(t), j.is_object(t))
  print(('x'):upper(), getmetatable(5), getmetatable(true), getmetatable(print))
$$;

-- the type of each kind of jsonb value, and of other values
DO LANGUAGE pl_lua $$
  local j = require 'pl_lua.jsonb'
  for _, s in ipairs { '{"a": 1}', '[1]', '"s"', '1.5', 'true', 'null' } do
    print(s, j.type(pgtype.jsonb:fromstring(s)))
  end
  print(j.type(5), j.type(5, true), j.type('x', true), j.type(true, true), j.type(nil, true),
        j.type({}, true), j.type(pgtype.numeric(1), true))
$$;

-- keys that are numbers, and values that are converted by tostring
DO LANGUAGE pl_lua $$
  print(pgtype.jsonb({ [1.5] = 'a', [2.5] = 'b' }))
  print(pgtype.jsonb({ k = setmetatable({}, { __tostring = function() return 'ts' end }) }))
  print(pgtype.jsonb({ [setmetatable({}, { __tostring = function() return 'key' end })] = 1 }))
$$;
