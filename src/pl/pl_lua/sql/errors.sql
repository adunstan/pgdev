--
-- Error objects, message functions, pcall and friends
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

-- the forms of elog, and what a caught error looks like
DO LANGUAGE pl_lua $$
  local ok, e = pcall(spi.elog, 'error', 'division_by_zero', 'oops',
                      'some detail', 'a hint')
  print(ok, e.sqlstate, e.errcode, e.category, e.severity)
  print(e.message, e.detail, e.hint)
  print(tostring(e))

  ok, e = pcall(spi.elog, 'error', '22P02', 'bad input')
  print(ok, e.sqlstate, e.errcode, e.category, e.detail)

  ok, e = pcall(spi.elog, 'error', 'plain message')
  print(ok, e.sqlstate, e.errcode, e.category, e.message)

  ok, e = pcall(spi.error, 'P0001', 'raised')
  print(ok, e.sqlstate, e.errcode, e.category)

  ok, e = pcall(spi.elog, 'error', { sqlstate = 'unique_violation',
                                     message = 'dup', schema = 's',
                                     table = 't', column = 'c',
                                     datatype = 'integer',
                                     constraint = 'k', hint = 'h' })
  print(ok, e.sqlstate, e.message, e.schema, e.table, e.column, e.datatype,
        e.constraint, e.hint)

  -- no such condition
  ok, e = pcall(spi.elog, 'error', 'no_such_condition', 'x')
  print(ok, e.sqlstate, e.message)
  ok, e = pcall(spi.elog, 'frobnicate', 'x')
  print(ok, e)
$$;

-- messages of all levels
SET client_min_messages = debug1;
DO LANGUAGE pl_lua $$
  spi.debug('debug message')
  spi.log('log message')
  spi.info('info message')
  spi.notice('notice message')
  spi.warning('warning message')
  spi.notice('22012', 'with sqlstate', 'and detail', 'and hint')
  spi.elog('notice', { message = 'from a table', detail = 'dd' })
  print('printed', 1, 2.5, true, nil)
$$;
RESET client_min_messages;
\set VERBOSITY default
DO LANGUAGE pl_lua $$
  spi.notice('22012', 'with sqlstate', 'and detail', 'and hint')
$$;
\set VERBOSITY terse

-- the error module
DO LANGUAGE pl_lua $$
  local err = require 'pl_lua.error'
  local ok, e = pcall(spi.error, 'numeric_value_out_of_range', 'big')
  print(err.type(e), err.errcode(e), err.category(e))
  print(err.type('str'), err.errcode('str'), err.category('str'))
  print(err.type(nil), err.type({}), err.type(setmetatable({}, {})))
  print(select('#', err.type('str')), select('#', err.errcode({})))
  print(getmetatable(e), pcall(setmetatable, e, {}))
$$;

-- errors that are not caught keep their SQLSTATE and fields
DO LANGUAGE pl_lua $$
  spi.error('unique_violation', 'uncaught', 'the detail', 'the hint')
$$;
\set VERBOSITY default
DO LANGUAGE pl_lua $$
  spi.error('unique_violation', 'uncaught', 'the detail', 'the hint')
$$;
\set VERBOSITY terse
DO LANGUAGE pl_lua $$
  local ok, e = pcall(spi.error, 'P0001', 'rethrown')
  error(e)
$$;
DO LANGUAGE pl_lua $$ spi.error('plain') $$;

-- Lua errors, of various kinds of value
DO LANGUAGE pl_lua $$ error('lua message') $$;
DO LANGUAGE pl_lua $$ error('lua message', 0) $$;
DO LANGUAGE pl_lua $$ error({}) $$;
DO LANGUAGE pl_lua $$ error(42) $$;
DO LANGUAGE pl_lua $$ error(setmetatable({}, { __tostring = function() return 'custom text' end })) $$;
DO LANGUAGE pl_lua $$ local x = nil; return x.field $$;
DO LANGUAGE pl_lua $$ return ( $$;
-- an error from Lua code can be caught by PL/pgSQL
DO $$
BEGIN
  EXECUTE 'DO LANGUAGE pl_lua $x$ error("from lua") $x$';
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'caught % %', SQLSTATE, SQLERRM;
END
$$;
DO $$
BEGIN
  EXECUTE 'DO LANGUAGE pl_lua $x$ spi.error("22012", "from sql") $x$';
EXCEPTION WHEN division_by_zero THEN
  RAISE NOTICE 'caught % %', SQLSTATE, SQLERRM;
END
$$;

-- pcall and friends
DO LANGUAGE pl_lua $$
  print(pcall(function(...) return select('#', ...), ... end, 1, nil, 3))
  print((pcall(error)))
  print((pcall(error, nil)))
  print(pcall(error, 'msg', 0))
$$;
DO LANGUAGE pl_lua $$
  print(xpcall(function(a, b) return a + b end, print, 3, 4))
  print(xpcall(function() error('inner', 0) end,
               function(m) return 'handled ' .. m end))
  print(xpcall(function() spi.error('P0001', 'sql') end,
               function(e) return 'handled ' .. e.errcode end))
  print(xpcall(function() error('inner', 0) end,
               function(m) error('in handler') end))
  print(pcall(xpcall, 1))
$$;

-- lpcall catches Lua errors, and lets SQL errors through
DO LANGUAGE pl_lua $$
  print(lpcall(error, 'lua error', 0))
  print(lpcall(function() return 1, 2 end))
  print(pcall(lpcall, spi.error, 'P0001', 'sql error'))
  lpcall(spi.error, 'P0001', 'sql error not caught')
  print('not reached')
$$;

-- coroutines
DO LANGUAGE pl_lua $$
  local co = coroutine.create(function(a)
    local b = coroutine.yield(a + 1)
    error('in coroutine', 0)
  end)
  print(coroutine.resume(co, 1))
  print(coroutine.resume(co, 2))
  print(coroutine.resume(co))
  print(coroutine.status(co))
  print(coroutine.resume(coroutine.running()))
  local w = coroutine.wrap(function() coroutine.yield(1) error('wrapped', 0) end)
  print(w())
  print(pcall(w))
$$;
DO LANGUAGE pl_lua $$
  -- an SQL error in a coroutine is not returned by resume
  local co = coroutine.create(function() spi.error('P0001', 'from coroutine') end)
  local ok, e = pcall(coroutine.resume, co)
  print(ok, e.errcode)
  co = coroutine.create(function() spi.error('P0001', 'not caught') end)
  coroutine.resume(co)
  print('not reached')
$$;
DO LANGUAGE pl_lua $$
  local w = coroutine.wrap(function() spi.error('P0001', 'wrap error') end)
  print(pcall(w))
$$;

-- yielding out of pcall is not possible
DO LANGUAGE pl_lua $$
  local co = coroutine.wrap(function()
    print(pcall(coroutine.yield, 1))
    return 'done'
  end)
  print(co())
$$;

-- operator intervention can't be caught
SET statement_timeout = 200;
DO LANGUAGE pl_lua $$
  print(pcall(function() while true do end end))
  print('not reached')
$$;
DO LANGUAGE pl_lua $$
  print(xpcall(function() while true do end end, print))
  print('not reached')
$$;
DO LANGUAGE pl_lua $$
  print(coroutine.resume(coroutine.create(function() while true do end end)))
  print('not reached')
$$;
RESET statement_timeout;
-- and the session is still usable
DO LANGUAGE pl_lua $$ print(pcall(error, 'fine', 0)) $$;

-- tostring of an error object with long fields, which need more room than
-- a short string would
DO LANGUAGE pl_lua $$
  local ok, e = pcall(spi.error, { sqlstate = '22012', message = ('m'):rep(5000),
                                   detail = ('d'):rep(3000), hint = ('h'):rep(2000) })
  local s = tostring(e)
  print(ok, #s, s:sub(1, 20), s:sub(-3))
  -- no detail or hint, and no SQLSTATE given: the default is used
  ok, e = pcall(spi.error, 'only a message')
  print(tostring(e))
$$;
