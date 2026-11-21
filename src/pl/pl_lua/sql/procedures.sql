--
-- Procedures and transaction control
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

CREATE TABLE tx_log (a integer);

CREATE PROCEDURE commit_evens()
LANGUAGE pl_lua AS $$
  for i = 0, 9 do
    spi.execute('INSERT INTO tx_log VALUES ($1)', i)
    if i % 2 == 0 then
      spi.commit()
    else
      spi.rollback()
    end
  end
$$;
CALL commit_evens();
SELECT * FROM tx_log ORDER BY a;

-- atomicity
CREATE PROCEDURE show_atomic() LANGUAGE pl_lua AS $$
  print(spi.is_atomic())
$$;
CREATE FUNCTION show_atomic_fn() RETURNS void LANGUAGE pl_lua AS $$
  print(spi.is_atomic())
$$;
CALL show_atomic();
SELECT show_atomic_fn();
DO LANGUAGE pl_lua $$ print(spi.is_atomic()) $$;
BEGIN;
DO LANGUAGE pl_lua $$ print(spi.is_atomic()) $$;
COMMIT;

-- transaction control is not possible in functions, in explicit
-- transactions, or inside pcall
CREATE FUNCTION commit_in_fn() RETURNS void LANGUAGE pl_lua AS $$
  spi.commit()
$$;
SELECT commit_in_fn();
BEGIN;
CALL commit_evens();
ROLLBACK;
CREATE PROCEDURE commit_in_pcall() LANGUAGE pl_lua AS $$
  local ok, err = pcall(spi.commit)
  print(ok)
$$;
CALL commit_in_pcall();

-- cursors do not survive a commit, unless held
CREATE PROCEDURE cursor_and_commit() LANGUAGE pl_lua AS $$
  local c = spi.newcursor('txc')
  c:open('SELECT 1')
  spi.commit()
  print(c:isopen())
$$;
CALL cursor_and_commit();

-- procedures with output parameters
CREATE PROCEDURE sum_and_diff(a integer, b integer, INOUT s integer, OUT d integer)
LANGUAGE pl_lua AS $$
  return a + b, a - b
$$;
CALL sum_and_diff(7, 3, 0, NULL);

-- a procedure can call another one that commits
CREATE PROCEDURE outer_commit() LANGUAGE pl_lua AS $$
  spi.execute('INSERT INTO tx_log VALUES (100)')
  spi.execute('CALL commit_evens()')
$$;
CALL outer_commit();
SELECT count(*) FROM tx_log;

-- the commit and rollback commands themselves are refused
CREATE PROCEDURE sql_commit() LANGUAGE pl_lua AS $$
  spi.execute('COMMIT')
$$;
CALL sql_commit();
