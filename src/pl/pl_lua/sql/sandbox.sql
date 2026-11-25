--
-- The trusted sandbox
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
CREATE EXTENSION IF NOT EXISTS pl_luau;
RESET client_min_messages;
\set VERBOSITY terse

-- what is visible in the sandbox
DO LANGUAGE pl_lua $$
  local function keys(t)
    local n = {}
    for k in pairs(t) do if not (t == table and k == 'create') then n[#n + 1] = k end end
    -- compare bytes, not by the locale's collation
    table.sort(n, function(a, b)
      for i = 1, math.min(#a, #b) do
        local x, y = a:byte(i), b:byte(i)
        if x ~= y then return x < y end
      end
      return #a < #b
    end)
    return table.concat(n, ' ')
  end
  print(keys(_G))
  for _, lib in ipairs{ 'coroutine', 'os', 'package', 'string', 'table', 'utf8' } do
    print(lib, keys(_G[lib]))
  end
  print(_G.math.sqrt ~= nil, _G.math.huge)
$$;

-- things that must not be there
DO LANGUAGE pl_lua $$
  print(io, debug, dofile, loadfile, loadstring, string.dump, os.execute,
        os.exit, os.getenv, os.remove, os.rename, os.tmpname, os.setlocale,
        package.loadlib, package.path, package.cpath, package.config,
        rawget(_G, 'trusted'), trusted)
$$;

-- the global environment is the sandbox itself
DO LANGUAGE pl_lua $$
  print(_G._G == _G, _G.string == string, _G.pcall == pcall)
  print(_G.require ~= nil, package.loaded._G == _G, package.loaded.string == string)
$$;

-- load accepts text only, and runs chunks in the sandbox by default
DO LANGUAGE pl_lua $$
  print(load('return 1 + 1')())
  print(load('x = 17; return x')(), x)
  print(load('return y', 'chunk', 't', { y = 'env' })())
  print(type(load('return y', 'chunk', 't', nil)))
  print(load(string.char(27) .. 'Lua'))
  print(load(string.char(27) .. 'Lua', 'chunk', 'b'))
  print(load('return (', 'chunk'))
  local parts = { 'return ', '40 + ', '2' }
  local i = 0
  print(load(function() i = i + 1 return parts[i] end)())
$$;

-- the metatable of strings must not give access to the real string library
DO LANGUAGE pl_lua $$
  local mt = getmetatable('')
  print(('abc'):upper(), mt.__index == string, mt.__index.dump)
  mt.__index.evil = function() return 'evil' end
  string.evil2 = function() return 'evil2' end
  print(('abc').evil, ('abc').evil2)
  print(pcall(setmetatable, 'abc', {}))
  print(pcall(rawset, 'abc', 'x', 1))
$$;
DO LANGUAGE pl_lua $$
  print(('abc').evil, ('abc').evil2, type(string.evil), type(string.evil2))
$$;
DO LANGUAGE pl_luau $$
  print(('abc').evil, ('abc').evil2, string.evil, string.evil2)
$$;

-- modules that have not been allowed
DO LANGUAGE pl_lua $$
  for _, name in ipairs{ 'io', 'debug', 'lpeg', 'pl_lua.trusted', 'pl_lua.paths',
                         'no.such.module' } do
    local ok, err = pcall(require, name)
    print(name, ok, (err:gsub('\n.*', '')))
  end
  print(pcall(require))
  print(pcall(require, {}))
$$;

-- modules that are available
DO LANGUAGE pl_lua $$
  for _, name in ipairs{ 'string', 'table', 'math', 'os', 'coroutine', 'utf8',
                         'package', 'pl_lua.spi', 'pl_lua.pgtype',
                         'pl_lua.elog', 'pl_lua.error', 'pl_lua.numeric',
                         'pl_lua.jsonb', 'pl_lua.time' } do
    print(name, type(require(name)))
  end
  print(require('pl_lua.spi') == spi, require('pl_lua.pgtype') == pgtype)
  print(require('string') == string, require('_G') == _G)
$$;

-- package.preload and package.searchers are available to sandbox code
DO LANGUAGE pl_lua $$
  package.preload['my.mod'] = function(name, extra)
    return { name = name, extra = extra }
  end
  local m, extra = require 'my.mod'
  print(m.name, m.extra, extra, package.loaded['my.mod'] == m)
  print(require 'my.mod' == m)

  table.insert(package.searchers, 1, function(name)
    if name == 'searched' then
      return function() return 'by searcher' end
    end
    return '\n\tnot mine'
  end)
  print(require 'searched')
  print((select(2, pcall(require, 'nothing'))))
$$;

-- the untrusted language has everything
DO LANGUAGE pl_luau $$
  print(io ~= nil, debug ~= nil, string.dump ~= nil, os.execute ~= nil,
        dofile ~= nil, package.loadlib ~= nil)
  print(type(trusted), type(require('io')))
$$;

-- an untrusted language user can't be fooled by sandbox state
DO LANGUAGE pl_luau $$ print(rawget(_G, 'shared'), rawget(_G, 'visible')) $$;

-- trusted code run by another role gets its own sandbox
DO LANGUAGE pl_lua $$ _G.visible = 'first role' $$;
CREATE ROLE regress_pll_sandbox_other;
GRANT USAGE ON LANGUAGE pl_lua TO regress_pll_sandbox_other;
SET ROLE regress_pll_sandbox_other;
DO LANGUAGE pl_lua $$ print(visible) _G.visible = 'other role' $$;
DO LANGUAGE pl_lua $$ print(visible) $$;
RESET ROLE;
DO LANGUAGE pl_lua $$ print(visible) $$;
REVOKE USAGE ON LANGUAGE pl_lua FROM regress_pll_sandbox_other;
DROP ROLE regress_pll_sandbox_other;

-- Trusted code cannot set finalizers: the garbage collector could run them
-- while PL/Lua's C code is in the middle of its work
DO LANGUAGE pl_lua $$
  print(pcall(setmetatable, {}, { __gc = function() print('finalized') end }))
  print(pcall(setmetatable, {}, { __gc = false }))
  local mt = {}
  rawset(mt, '__gc', function() end)
  print(pcall(setmetatable, {}, mt))
  -- a field added after the metatable was set marks nothing, so nothing runs
  local late = {}
  setmetatable({}, late)
  late.__gc = function() print('finalized late') end
  collectgarbage()
  collectgarbage()
  -- other metatables and __close are fine
  local t = setmetatable({}, { __index = function(_, k) return k .. '!' end,
                               __call = function() return 'called' end })
  print(t.x, t(), getmetatable(t) ~= nil, getmetatable(setmetatable(t, nil)))
  do
    local c <close> = setmetatable({}, { __close = function() print('closed') end })
  end
  print(pcall(setmetatable, 5, {}))
  print(pcall(setmetatable, {}, 5))
  print(pcall(setmetatable))
$$;

-- loaded code and other ways of calling the function are covered too
DO LANGUAGE pl_lua $$
  local f = load("return setmetatable({}, { __gc = function() end })")
  print(pcall(f))
  print(pcall(pcall, setmetatable, {}, { __gc = true }))
  print(select(2, pcall(function() return _G.setmetatable({}, { __gc = print }) end)))
$$;

-- the untrusted language is not restricted
DO LANGUAGE pl_luau $$
  setmetatable({}, { __gc = function() print('finalized in the untrusted language') end })
  collectgarbage()
  collectgarbage()
$$;

-- The metatables of PL/Lua's own objects cannot be reached: a metatable that
-- could be reached could be changed, and replacing the __gc of statements
-- or cursors would run user code at collection time, which is not allowed
DO LANGUAGE pl_lua $$
  local objs = {
    statement = spi.prepare('select 1'),
    cursor = spi.newcursor(),
    typeinfo = pgtype.integer,
    numeric = pgtype.numeric(1),
    composite = spi.execute('select 1 as a')[1],
    array = pgtype.array.integer(1, 2),
    range = pgtype.int4range(1, 2),
    jsonb = pgtype.jsonb:fromstring('{}'),
    time = pgtype.date('2026-01-01'),
    text = pgtype.bytea('x'),
  }
  local names = {}
  for k in pairs(objs) do names[#names + 1] = k end
  table.sort(names)
  for _, k in ipairs(names) do
    print(k, getmetatable(objs[k]))
  end
  local st = objs.statement
  print(pcall(function() getmetatable(st).__gc = function() print('USER CODE RAN') end end))
  print(pcall(setmetatable, st, {}))
  st, objs = nil, nil
  collectgarbage()
  collectgarbage()
  -- the marker metatables of tables made from jsonb belong to the user's tables
  local t = pgtype.jsonb:fromstring('{"a": 1}'){}
  print(type(getmetatable(t)), (pcall(setmetatable, t, { __index = function() return 'x' end })), t.nothing)
$$;

-- the same for the trigger object
CREATE TABLE sb_trg_t (a integer);
CREATE FUNCTION sb_trg() RETURNS trigger LANGUAGE pl_lua AS $$
  print(getmetatable(trigger), pcall(function() getmetatable(trigger).__gc = print end))
$$;
CREATE TRIGGER sb_trg BEFORE INSERT ON sb_trg_t FOR EACH ROW EXECUTE FUNCTION sb_trg();
INSERT INTO sb_trg_t VALUES (1);
DROP TABLE sb_trg_t;
DROP FUNCTION sb_trg();

-- precompiled chunks cannot be loaded, whatever the mode asked for
DO LANGUAGE pl_lua $$
  print(load('\27Lua\84\0\25\147\13\10\26\10'))
  print(load('\27Lua', 'chunk', 'b'))
  print(load('\27Lua', 'chunk', 'bt'))
  print(pcall(load, function() return '\27Lua' end, 'chunk', 'b'))
$$;
