--
-- Queries, prepared statements and iterators
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

CREATE TEMP TABLE items (id integer PRIMARY KEY, qty integer, label text, price numeric);
INSERT INTO items VALUES
  (1, 10, 'apple', 1.50),
  (2, 20, 'banana', 0.25),
  (3, 30, 'cherry', 7.00),
  (4, 40, 'date', 3.10),
  (5, 50, 'elder', 2.00);

-- results of spi.execute
DO LANGUAGE pl_lua $$
  local r = spi.execute('SELECT id, label FROM items ORDER BY id')
  print(#r, r[1].id, r[1].label, r[5].label)
  print(spi.execute('SELECT 1 WHERE false')[1])
  print(spi.execute('UPDATE items SET qty = qty + 1 WHERE id <= 2'))
  print(spi.execute('INSERT INTO items VALUES (6, 60, $1, $2)', 'fig', 4.5))
  print(spi.execute('INSERT INTO items VALUES (7, 70, $1, $2) RETURNING id', 'grape', 5)[1].id)
  print(spi.execute('DELETE FROM items WHERE id > 5'))
$$;

-- parameters of deduced types
DO LANGUAGE pl_lua $$
  local r = spi.execute('SELECT label FROM items WHERE id = $1', 3)
  print(r[1].label)
  r = spi.execute('SELECT $1::text AS a, $2 + 1 AS b', 'x', 41)
  print(r[1].a, r[1].b)
  r = spi.execute('SELECT label FROM items WHERE qty > $1 AND label <> $2 ORDER BY id', 20, 'date')
  print(#r, r[1].label)
  r = spi.execute('SELECT $1 IS NULL AS isnull', nil)
  print(r[1].isnull)
$$;

-- execute_count
DO LANGUAGE pl_lua $$
  print(#spi.execute_count('SELECT * FROM items ORDER BY id', 2))
  print(#spi.execute_count('SELECT * FROM items ORDER BY id', 0))
  print(#spi.execute_count('SELECT * FROM items WHERE qty > $1', 3, 25))
$$;

-- prepared statements
DO LANGUAGE pl_lua $$
  local s = spi.prepare('SELECT label FROM items WHERE id = $1', { 'integer' })
  print(s:numargs(), s:argtype(1):name())
  print(s:execute(2)[1].label)
  print(s(4)[1].label)
  print(#s:execute_count(1, 2))
  s = spi.prepare('SELECT label FROM items WHERE id = ANY ($1) ORDER BY id',
                  { pgtype.array.integer })
  local r = s:execute(pgtype.array.integer(1, 3, 5))
  print(#r, r[1].label, r[3].label)
  -- types not given are deduced
  s = spi.prepare('SELECT $1 + qty AS q FROM items WHERE id = $2')
  print(s:argtype(1):name(), s:argtype(2):name())
  print(s:execute(5, 1)[1].q)
  -- unused parameters have no type
  s = spi.prepare('SELECT $1::integer + $3')
  print(s:numargs(), type(s:argtype(2)), s:argtype(3):name())
$$;

-- iterators
DO LANGUAGE pl_lua $$
  local n = 0
  for r in spi.rows('SELECT id FROM generate_series(1, 120) id') do
    n = n + r.id
  end
  print(n)
  local s = spi.prepare('SELECT id FROM items WHERE qty >= $1 ORDER BY id',
                        { 'integer' }, { fetch_count = 2 })
  for r in s:rows(30) do print(r.id) end
  -- leaving a loop early closes the cursor
  for r in spi.rows('SELECT * FROM items ORDER BY id') do
    if r.id == 2 then break end
  end
  print(spi.execute('SELECT count(*) AS c FROM pg_cursors')[1].c)
$$;

-- errors
DO LANGUAGE pl_lua $$ spi.execute('SELECT * FROM no_such_table') $$;
DO LANGUAGE pl_lua $$ spi.execute('SELEKT 1') $$;
DO LANGUAGE pl_lua $$ spi.execute('SELECT 1', 2) $$;
DO LANGUAGE pl_lua $$ spi.execute('SELECT $1::integer + $2', 2) $$;
\echo :LAST_ERROR_SQLSTATE
DO LANGUAGE pl_lua $$ spi.execute('SELECT $1::integer', 'abc') $$;
DO LANGUAGE pl_lua $$ spi.execute('COPY items TO STDOUT') $$;
DO LANGUAGE pl_lua $$ spi.execute('COMMIT') $$;
DO LANGUAGE pl_lua $$ spi.prepare(nil) $$;
DO LANGUAGE pl_lua $$ spi.prepare('SELECT 1', { 'no_such_type' }) $$;

-- many parameters
DO LANGUAGE pl_lua $$
  local types, args = {}, {}
  for i = 1, 300 do types[i] = 'integer'; args[i] = i end
  local s = spi.prepare('SELECT $1 + $300 AS a', types)
  print(s:numargs(), s:execute(table.unpack(args))[1].a)
$$;

-- read-only mode follows the function's volatility
CREATE FUNCTION is_ro() RETURNS boolean STABLE LANGUAGE pl_lua AS $$
  return spi.readonly()
$$;
CREATE FUNCTION is_rw() RETURNS boolean VOLATILE LANGUAGE pl_lua AS $$
  return spi.readonly()
$$;
CREATE FUNCTION write_in_stable() RETURNS void STABLE LANGUAGE pl_lua AS $$
  spi.execute('INSERT INTO items VALUES (100, 1, $1, 1)', 'x')
$$;
SELECT is_ro(), is_rw();
SELECT write_in_stable();

-- queries that call other PL/Lua functions
CREATE FUNCTION inner_fn(x integer) RETURNS integer LANGUAGE pl_lua AS $$
  return spi.execute('SELECT $1 * 2 AS v', x)[1].v
$$;
CREATE FUNCTION outer_fn(x integer) RETURNS integer LANGUAGE pl_lua AS $$
  return spi.execute('SELECT inner_fn($1) + 1 AS v', x)[1].v
$$;
SELECT outer_fn(5);

-- the type of a datum object argument is the type of the parameter, unless
-- the query decides otherwise
DO LANGUAGE pl_lua $$
  local function q(sql, ...)
    local ok, r = pcall(spi.execute, sql, ...)
    print(ok and r[1].t or (tostring(r):gsub("^ERROR: %S+ ", "")))
  end
  local j = pgtype.jsonb:fromstring('{"a": 1}')
  q("select pg_typeof($1)::text as t", j)
  q("select pg_typeof($1 -> 'a')::text as t", j)
  q("select pg_typeof($1[1])::text as t", pgtype.array.integer(1, 2))
  q("select pg_typeof($1 + 1)::text as t", pgtype.numeric(5))
  q("select pg_typeof($1 || 'x'::text)::text as t", pgtype.date('2026-01-01'))
  q("select pg_typeof($1::text)::text as t", j)
  q("select pg_typeof($1)::text || ',' || pg_typeof($2)::text as t", j, pgtype.date('2026-01-01'))
  -- other Lua values do not determine the type
  q("select pg_typeof($1)::text as t", "a string")
  q("select pg_typeof($1 + 1)::text as t", 5)
  q("select pg_typeof($1)::text as t", 5)
  -- the number of arguments still has to match
  q("select pg_typeof($1)::text as t", j, j)
  q("select pg_typeof($1)::text || pg_typeof($2)::text as t", j)
$$;

-- queries that fail, with the error caught, do not leave memory behind
CREATE FUNCTION spi_leak_check() RETURNS boolean LANGUAGE pl_lua AS $$
  local function mem()
    return spi.execute("select sum(total_bytes)::bigint as b from pg_backend_memory_contexts")[1].b
  end
  for i = 1, 100 do pcall(spi.execute, "select 1/0") end
  local before = mem()
  for i = 1, 3000 do
    pcall(spi.execute, "select 1/0")
    pcall(spi.execute, "select $1::int + $2::int", "x", 1)
    pcall(spi.execute, "select $1::int", { "not a value" })
  end
  return mem() - before < 1000000
$$;
SELECT spi_leak_check();
DROP FUNCTION spi_leak_check();

-- options of prepared statements
DO LANGUAGE pl_lua $$
  print(pcall(spi.prepare, 'select 1', nil, { scroll = true, no_scroll = true }))
  local st = spi.prepare('select 1 as a', nil, { fast_start = true, custom_plan = true })
  print(st()[1].a)
  print(pcall(spi.prepare, 'select 1', nil, { custom_plan = true, generic_plan = true }))
$$;
