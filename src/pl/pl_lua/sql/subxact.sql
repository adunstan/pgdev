--
-- pcall and subtransactions
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

CREATE TABLE sx(i integer PRIMARY KEY, note text);

-- a failed call is rolled back as a whole, a successful one is kept
DO LANGUAGE pl_lua $$
  spi.execute("INSERT INTO sx VALUES (1, 'outer')")
  local ok, e = pcall(function()
    spi.execute("INSERT INTO sx VALUES (2, 'in failed pcall')")
    spi.execute("INSERT INTO sx VALUES (1, 'duplicate')")
  end)
  print(ok, e.errcode, e.table, e.constraint)
  ok = pcall(function()
    spi.execute("INSERT INTO sx VALUES (3, 'in good pcall')")
  end)
  print(ok)
  ok, e = pcall(function()
    spi.execute("INSERT INTO sx VALUES (4, 'before a Lua error')")
    error('lua error', 0)
  end)
  print(ok, e)
$$;
SELECT * FROM sx ORDER BY i;

-- nesting
TRUNCATE sx;
DO LANGUAGE pl_lua $$
  spi.execute("INSERT INTO sx VALUES (1, 'level 0')")
  print(pcall(function()
    spi.execute("INSERT INTO sx VALUES (2, 'level 1')")
    print(pcall(function()
      spi.execute("INSERT INTO sx VALUES (3, 'level 2')")
      spi.error('P0001', 'inner')
    end))
    spi.execute("INSERT INTO sx VALUES (4, 'level 1 again')")
  end))
  print(pcall(function()
    spi.execute("INSERT INTO sx VALUES (5, 'level 1, fails')")
    print(pcall(function() spi.execute("INSERT INTO sx VALUES (6, 'level 2, kept?')") end))
    error('outer fails', 0)
  end))
$$;
SELECT * FROM sx ORDER BY i;

-- xpcall: the handler runs after the subtransaction is gone
TRUNCATE sx;
DO LANGUAGE pl_lua $$
  print(xpcall(function()
    spi.execute("INSERT INTO sx VALUES (1, 'rolled back')")
    spi.execute("INSERT INTO sx VALUES (1, 'duplicate')")
  end, function(e)
    spi.execute("INSERT INTO sx VALUES (2, 'from the handler')")
    return 'handled ' .. e.errcode
  end))
$$;
SELECT * FROM sx ORDER BY i;

-- lpcall doesn't start a subtransaction
TRUNCATE sx;
DO LANGUAGE pl_lua $$
  print(lpcall(function()
    spi.execute("INSERT INTO sx VALUES (1, 'kept')")
    error('lua error', 0)
  end))
$$;
SELECT * FROM sx ORDER BY i;

-- an SQL error raised through lpcall aborts the whole thing
TRUNCATE sx;
DO LANGUAGE pl_lua $$
  spi.execute("INSERT INTO sx VALUES (1, 'lost')")
  lpcall(spi.execute, "INSERT INTO sx VALUES (1, 'duplicate')")
$$;
SELECT * FROM sx ORDER BY i;

-- errors after the catch: the session state is intact
DO LANGUAGE pl_lua $$
  for i = 1, 50 do
    local ok = pcall(spi.execute, 'SELECT 1/0')
    assert(not ok)
  end
  print(pcall(spi.execute, 'SELECT 1/0 FROM nowhere'))
  spi.execute("INSERT INTO sx VALUES (100, 'still working')")
$$;
SELECT * FROM sx ORDER BY i;

-- a PL/pgSQL exception block around Lua code that uses pcall
TRUNCATE sx;
DO $$
BEGIN
  INSERT INTO sx VALUES (1, 'plpgsql');
  BEGIN
    DO LANGUAGE pl_lua $x$
      spi.execute("INSERT INTO sx VALUES (2, 'lua')")
      pcall(spi.execute, "INSERT INTO sx VALUES (2, 'lua duplicate')")
      spi.execute("INSERT INTO sx VALUES (3, 'lua, before the error')")
      spi.error('P0001', 'and now fail')
    $x$;
  EXCEPTION WHEN raise_exception THEN
    RAISE NOTICE 'caught: %', SQLERRM;
  END;
END
$$;
SELECT * FROM sx ORDER BY i;

-- query cancel can't be caught, and the transaction is left in a good state
SET statement_timeout = 200;
DO LANGUAGE pl_lua $$
  print(pcall(spi.execute, 'SELECT pg_sleep(10)'))
  print('not reached')
$$;
RESET statement_timeout;
SELECT count(*) FROM sx;

DROP TABLE sx;
