--
-- Quoting functions
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

DO LANGUAGE pl_lua $$
  print(spi.quote_ident('foo'))
  print(spi.quote_ident('Foo'))
  print(spi.quote_ident('a b'))
  print(spi.quote_ident('say "hi"'))
  print(spi.quote_ident('select'))
  print(spi.quote_ident(42))
  print(spi.quote_literal('abc'))
  print(spi.quote_literal("it's"))
  print(spi.quote_literal([[back\slash]]))
  print(spi.quote_literal(12.5))
  print(spi.quote_nullable('abc'))
  print(spi.quote_nullable(nil))
  print(spi.quote_nullable())
  print(spi.quote_nullable(7))
$$;

-- the results are usable in dynamic SQL
CREATE FUNCTION make_table(tab text, val text) RETURNS bigint LANGUAGE pl_lua AS $$
  spi.execute('CREATE TEMP TABLE ' .. spi.quote_ident(tab) .. ' (v text)')
  spi.execute('INSERT INTO ' .. spi.quote_ident(tab) .. ' VALUES (' ..
              spi.quote_nullable(val) .. ')')
  return spi.execute('SELECT count(*) AS c FROM ' .. spi.quote_ident(tab) ..
                     ' WHERE v IS NOT DISTINCT FROM ' .. spi.quote_nullable(val))[1].c
$$;
SELECT make_table('Odd Name', 'it''s');
SELECT make_table('other', NULL);
SELECT v FROM "Odd Name";

-- errors
DO LANGUAGE pl_lua $$ spi.quote_ident('a\0b') $$;
DO LANGUAGE pl_lua $$ spi.quote_literal('a\0b') $$;
DO LANGUAGE pl_lua $$ spi.quote_nullable('a\0b') $$;
DO LANGUAGE pl_lua $$ spi.quote_literal(nil) $$;
DO LANGUAGE pl_lua $$ spi.quote_ident({}) $$;
