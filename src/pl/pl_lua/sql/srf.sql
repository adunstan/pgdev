--
-- Set-returning functions
--
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;

\set VERBOSITY terse

CREATE FUNCTION srf_ints(n integer) RETURNS SETOF integer
LANGUAGE pl_lua AS $$
  for i = 1, n do coroutine.yield(i) end
$$;
SELECT * FROM srf_ints(5);
SELECT * FROM srf_ints(0);
SELECT count(*) FROM srf_ints(1000);

-- LIMIT stops the function early, and closes pending to-be-closed variables
CREATE FUNCTION srf_closing() RETURNS SETOF integer
LANGUAGE pl_lua AS $$
  local guard <close> = setmetatable({}, { __close = function() print("closed") end })
  for i = 1, 100 do coroutine.yield(i) end
$$;
SELECT * FROM srf_closing() LIMIT 3;
SELECT * FROM srf_closing() LIMIT 1000 OFFSET 98;

-- composite and table results
CREATE FUNCTION srf_squares(n integer) RETURNS TABLE (i integer, sq integer)
LANGUAGE pl_lua AS $$
  for i = 1, n do coroutine.yield(i, i * i) end
$$;
SELECT * FROM srf_squares(4);

CREATE TYPE srf_pair AS (a integer, b text);
CREATE FUNCTION srf_pairs() RETURNS SETOF srf_pair
LANGUAGE pl_lua AS $$
  coroutine.yield({ a = 1, b = "one" })
  coroutine.yield(2, "two")
  coroutine.yield({ a = 3 })
  coroutine.yield()
$$;
SELECT * FROM srf_pairs();

-- no yields: the returned values form a single row, or no rows
CREATE FUNCTION srf_single() RETURNS SETOF integer
LANGUAGE pl_lua AS $$ return 42 $$;
CREATE FUNCTION srf_empty() RETURNS SETOF integer
LANGUAGE pl_lua AS $$ local x = 1 $$;
SELECT * FROM srf_single();
SELECT * FROM srf_empty();

-- records
CREATE FUNCTION srf_records() RETURNS SETOF record
LANGUAGE pl_lua AS $$
  coroutine.yield(1, 'a')
  coroutine.yield(2, 'b')
$$;
SELECT * FROM srf_records() AS t(x integer, y text);

-- errors in the middle of a set
CREATE FUNCTION srf_error() RETURNS SETOF integer
LANGUAGE pl_lua AS $$
  coroutine.yield(1)
  error("broke at row 2")
$$;
SELECT * FROM srf_error();

-- several calls active at once, and use in joins and subqueries
SELECT a.i, b.i FROM srf_ints(3) a(i), srf_ints(2) b(i) ORDER BY 1, 2;
SELECT (SELECT sum(i) FROM srf_ints(x) s(i)) FROM generate_series(1, 4) x;

-- called in the select list
SELECT srf_ints(3);

-- arguments are kept across rows
CREATE FUNCTION srf_args(s text, n integer) RETURNS SETOF text
LANGUAGE pl_lua AS $$
  for i = 1, n do coroutine.yield(s .. i) end
$$;
SELECT * FROM srf_args('row', 3);

-- polymorphic
CREATE FUNCTION srf_poly(a anyelement, n integer) RETURNS SETOF anyelement
LANGUAGE pl_lua AS $$
  for i = 1, n do coroutine.yield(a) end
$$;
SELECT * FROM srf_poly('x'::text, 2);
SELECT * FROM srf_poly(7, 2);

-- recursion through SPI
CREATE FUNCTION srf_recursive(n integer) RETURNS SETOF integer
LANGUAGE pl_lua AS $$
  coroutine.yield(n)
  if n > 0 then
    for r in spi.rows("SELECT * FROM srf_recursive($1)", n - 1) do
      coroutine.yield(r[1])
    end
  end
$$;
SELECT * FROM srf_recursive(3);

-- a set-returning function in the select list is stopped by a LIMIT at once,
-- and its to-be-closed variables are closed (in FROM, the whole function is
-- run, whatever the LIMIT)
CREATE FUNCTION srf_closing_list() RETURNS SETOF integer
LANGUAGE pl_lua AS $$
  local guard <close> = setmetatable({}, { __close = function() print("closed") end })
  for i = 1, 100 do
    print("yield " .. i)
    coroutine.yield(i)
  end
$$;
SELECT srf_closing_list() LIMIT 3;
SELECT 'after the limit';

-- a query that ends with an error while a function is half done leaves nothing
-- behind
SELECT srf_closing_list(), 1 / (3 - srf_closing_list());
SELECT 'after the error';
SELECT count(*) FROM srf_ints(10);
