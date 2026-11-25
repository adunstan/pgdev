--
-- Convenience functions of the spi module, and typeinfo:kind()
--
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse
SET datestyle = ISO, MDY;

CREATE TABLE hp_t (id integer, name text);
INSERT INTO hp_t VALUES (1, 'a'), (2, 'b c'), (3, NULL);
CREATE TYPE hp_pair AS (n integer, label text);

-- return_query for scalar, composite and TABLE results
CREATE FUNCTION hp_scalar() RETURNS SETOF integer LANGUAGE pl_lua AS $$
  spi.return_query("select id from hp_t order by id")
$$;
CREATE FUNCTION hp_comp() RETURNS SETOF hp_pair LANGUAGE pl_lua AS $$
  spi.return_query("select id, name from hp_t order by id")
$$;
CREATE FUNCTION hp_table(min_id integer) RETURNS TABLE (a integer, b text)
LANGUAGE pl_lua AS $$
  spi.return_query("select id, name from hp_t where id >= $1 order by id", min_id)
$$;
SELECT * FROM hp_scalar();
SELECT * FROM hp_comp();
SELECT * FROM hp_table(2);

-- it can be mixed with other yields, and stops early with LIMIT
CREATE FUNCTION hp_mixed() RETURNS SETOF integer LANGUAGE pl_lua AS $$
  coroutine.yield(0)
  spi.return_query("select generate_series(1, 1000)")
  coroutine.yield(-1)
$$;
SELECT * FROM hp_mixed() LIMIT 3;
SELECT count(*), min(x), max(x) FROM hp_mixed() x;

-- outside a set-returning function
DO LANGUAGE pl_lua $$ print(pcall(spi.return_query, 'select 1')) $$;
CREATE FUNCTION hp_not_srf() RETURNS integer LANGUAGE pl_lua AS $$
  spi.return_query("select 1")
$$;
SELECT hp_not_srf();

-- typeinfo:kind()
DO LANGUAGE pl_lua $$
  print(pgtype.integer:kind(), pgtype.hp_pair:kind(), pgtype.array.integer:kind(),
        pgtype.int4range:kind(), pgtype.jsonb:kind(), pgtype.record:kind())
$$;

-- encode_typed_literal
DO LANGUAGE pl_lua $$
  print(spi.encode_typed_literal(5, 'text'))
  print(spi.encode_typed_literal('2026-01-02', 'date'))
  print(spi.encode_typed_literal({ 1, 2 }, 'integer[]'))
  print(spi.encode_typed_literal({ n = 3, label = 'x y' }, 'hp_pair'))
  print(spi.encode_typed_literal(nil, 'integer'))
  print(pcall(spi.encode_typed_literal, 1, 'no_such_type'))
$$;

-- encode_array_literal and encode_array_constructor: their results are
-- accepted by SQL, and give back the same values
DO LANGUAGE pl_lua $$
  local vals = { 'plain', 'with space', 'comma,here', 'quote"s', 'back\\slash',
                 'NULL', '', '{braces}' }
  local lit = spi.encode_array_literal(vals)
  print(lit)
  local back = spi.execute("select $1::text[] as a", lit)[1].a
  local ok = (#back == #vals)
  for i = 1, #vals do ok = ok and back[i] == vals[i] end
  print('literal round trip', ok)

  print(spi.encode_array_literal(table.pack(1, 2, nil, 4)))
  print(spi.encode_array_literal({ 1, 2, 3, n = 5 }))
  print(spi.encode_array_literal({ { 1, 2 }, { 3, 4 } }))
  print(spi.encode_array_literal({ 'a;b', 'c' }, ';'))
  print(spi.encode_array_literal({}))

  local cons = spi.encode_array_constructor(vals)
  print(cons)
  local viacons = spi.execute("select " .. cons .. " as a")[1].a
  ok = (#viacons == #vals)
  for i = 1, #vals do ok = ok and viacons[i] == vals[i] end
  print('constructor round trip', ok)
  print(spi.encode_array_constructor({ { 1, 2 }, { 3, 4 } }))
  print(spi.encode_array_constructor(table.pack(1, nil, 3)))
  print(pcall(spi.encode_array_literal, 'not a table'))
  print(pcall(spi.encode_array_constructor, 5))
$$;

-- looks_like_number
DO LANGUAGE pl_lua $$
  local function t(v) return tostring(spi.looks_like_number(v)) end
  print(t(5), t(1.5), t('12'), t(' 12 '), t('-3.5'), t('+.5'), t('1e5'), t('1.e5'),
        t('Inf'), t('-infinity'), t('NaN'))
  print(t(''), t(' '), t('abc'), t('12abc'), t('0x10'), t('1e'), t('.'), t('--1'),
        t(nil), t(true), t({}), t(pgtype.numeric('1.5')), t(pgtype.text('1')))
$$;

-- the functions are available in the module and in untrusted code
DO LANGUAGE pl_lua $$
  local spi2 = require 'pl_lua.spi'
  print(spi2.looks_like_number == spi.looks_like_number, type(spi2.return_query))
$$;
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_luau;
RESET client_min_messages;
DO LANGUAGE pl_luau $$ print(spi.looks_like_number('7'), spi.encode_array_literal({ 1, 2 })) $$;

-- the trusted code cannot change what the functions use
DO LANGUAGE pl_lua $$
  local tostring_before = tostring
  tostring = function() return 'hijacked' end
  string = nil
  print(spi.encode_array_literal({ 1, 'a b' }), spi.looks_like_number('5'))
$$;

-- a declared length that would make a huge array is refused
DO LANGUAGE pl_lua $$
  print(pcall(spi.encode_array_literal, { 1, 2, n = 2147483647 }))
  print(pcall(spi.encode_array_constructor, { n = 1e15 }))
  print(spi.encode_array_literal({ 1, n = 3 }), spi.encode_array_literal({ 1, n = -1 }),
        spi.encode_array_literal({ 1, 2, n = 'x' }))
$$;

-- a length that is not usable, from a __len metamethod for instance
DO LANGUAGE pl_lua $$
  local t = setmetatable({ 1 }, { __len = function() return 2^40 end })
  print(pcall(spi.encode_array_literal, t))
  t = setmetatable({ 1 }, { __len = function() return 'x' end })
  print(pcall(spi.encode_array_literal, t))
  t = setmetatable({ 1, 2 }, { __len = function() return 2 end })
  print(spi.encode_array_literal(t))
$$;

-- nesting deeper than an array can be is refused, which includes a table
-- that contains itself
DO LANGUAGE pl_lua $$
  local t = { 1, 2 }
  t[3] = t
  print(pcall(spi.encode_array_literal, t))
  print(pcall(spi.encode_array_constructor, t))
  print(spi.encode_array_literal({ { { { { { 1 } } } } } }))
  print(pcall(spi.encode_array_literal, { { { { { { { 1 } } } } } } }))
$$;
