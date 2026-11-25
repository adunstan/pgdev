--
-- Attacks that have been used against PL/Perl and similar languages, tried
-- against the trusted language as an unprivileged role.  Each attack is a
-- function that returns true if it got what it was after; the output lists
-- the ones that did, so an empty result is the expected one.  What a role
-- without privileges can do through SPI, and cancellation, are checked in
-- t/004_attacker.pl, which needs a real login as that role.
--
-- The groups, with what they correspond to in PL/Perl:
--   load      use, require, do FILE, eval of strings, DynaLoader, XSLoader
--   process   system, exec, backticks, pipe opens, fork, kill, exit
--   files     open, opendir, unlink, glob, chdir
--   escape    reaching the real symbol table or the real interpreter
--             state from the Safe compartment (utf8 and swash tricks, tie,
--             DESTROY, regular expression code blocks)
--
\set VERBOSITY terse
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
CREATE EXTENSION IF NOT EXISTS pl_luau;
RESET client_min_messages;

CREATE ROLE regress_pll_attacker;
GRANT ALL ON SCHEMA public TO regress_pll_attacker;
SET SESSION AUTHORIZATION regress_pll_attacker;

CREATE FUNCTION pll_attack_corpus() RETURNS TABLE (grp text, attack text, result text)
LANGUAGE pl_lua AS $$
  local results = {}
  local function attack(group, name, f)
    local ok, got = pcall(f)
    if ok and got then
      results[#results + 1] = { group, name, 'SUCCEEDED' }
    end
  end
  local function exists(path)
    local v = _G
    for part in path:gmatch('[^.]+') do
      if type(v) ~= 'table' then return false end
      v = rawget(v, part)
      if v == nil then return false end
    end
    return true
  end

  -- load
  for _, m in ipairs{ 'io', 'debug', 'ffi', 'lfs', 'socket', 'posix',
                      'lpeg', 'cjson', 'pl_lua', 'pl_lua.trusted', 'pgtype.internal',
                      'jit', 'bit32' } do
    attack('load', 'require ' .. m, function()
      local ok, mod = pcall(require, m)
      return ok and mod ~= nil
    end)
  end
  attack('load', 'require os gives a way to run commands', function()
    local os2 = require('os')
    return os2.execute ~= nil or os2.exit ~= nil or os2.getenv ~= nil or
           os2.remove ~= nil or os2.rename ~= nil or os2.tmpname ~= nil or
           os2.setlocale ~= nil
  end)
  for _, f in ipairs{ 'dofile', 'loadfile', 'loadstring', 'setfenv', 'getfenv',
                      'module', 'newproxy' } do
    attack('load', 'global ' .. f, function() return exists(f) end)
  end
  attack('load', 'string.dump', function() return exists('string.dump') end)
  attack('load', 'load a precompiled chunk', function()
    return load('\27Lua\84\0\25\147\13\10\26\10', 'x', 'b') ~= nil
  end)
  attack('load', 'load a precompiled chunk, mode given as bt', function()
    return load('\27Lua', 'x', 'bt') ~= nil
  end)
  attack('load', 'load with a nil mode', function()
    return load('\27Lua') ~= nil
  end)
  attack('load', 'load through a reader function', function()
    local sent = false
    return load(function() if not sent then sent = true return '\27Lua' end end) ~= nil
  end)
  attack('load', 'package.loadlib', function() return exists('package.loadlib') end)
  attack('load', 'package.cpath', function() return exists('package.cpath') end)
  attack('load', 'package.searchpath', function() return exists('package.searchpath') end)
  attack('load', 'a searcher that reads files', function()
    for _, s in ipairs(package.searchers) do
      local ok, f = pcall(s, '/etc/passwd')
      if ok and type(f) == 'function' then return true end
    end
    return false
  end)
  attack('load', 'package.loaded exposes a library', function()
    for _, m in ipairs{ 'io', 'debug', 'ffi' } do
      if package.loaded[m] ~= nil and m ~= 'package' then return true end
    end
    return false
  end)

  -- process and files
  for _, f in ipairs{ 'os.execute', 'os.exit', 'os.remove', 'os.rename', 'os.tmpname',
                      'os.getenv', 'os.setlocale', 'io.open', 'io.popen', 'io.lines',
                      'io.read', 'io.write', 'io.stdout', 'io.stdin', 'io.tmpfile',
                      'io.output', 'io.input' } do
    attack('process', f, function() return exists(f) end)
  end

  -- escape
  for _, f in ipairs{ 'debug.getinfo', 'debug.getlocal', 'debug.getupvalue',
                      'debug.setupvalue', 'debug.upvalueid', 'debug.sethook',
                      'debug.getregistry', 'debug.getmetatable', 'debug.setmetatable',
                      'debug.getuservalue', 'debug.traceback' } do
    attack('escape', f, function() return exists(f) end)
  end
  attack('escape', 'getmetatable of the globals', function()
    local mt = getmetatable(_G)
    return mt ~= nil and mt ~= false
  end)
  attack('escape', 'string metatable leads to the real string table', function()
    local mt = getmetatable('')
    if type(mt) ~= 'table' then return false end
    local idx = mt.__index
    if idx == nil then return false end
    -- changes to what the metatable gives must not reach strings
    pcall(function() idx.upper = function() return 'x' end end)
    pcall(function() mt.__index = {} end)
    pcall(function() mt.__call = function() end end)
    return ('a'):upper() ~= 'A' or ('a'):len() ~= 1
  end)
  attack('escape', 'setmetatable on a string', function()
    return pcall(setmetatable, 'x', {})
  end)
  attack('escape', 'a metatable with __gc', function()
    return pcall(setmetatable, {}, { __gc = function() end })
  end)
  attack('escape', 'a metatable made by a library function', function()
    local ok, mt = pcall(getmetatable, pgtype.integer(1))
    if ok and mt ~= false and mt ~= nil then return true end
    ok, mt = pcall(getmetatable, spi.execute)
    if ok and mt ~= nil then return true end
    ok, mt = pcall(getmetatable, coroutine.create(print))
    return ok and mt ~= nil
  end)
  attack('escape', 'rawget of the metatable of a datum', function()
    local d = pgtype.integer(1)
    local ok, v = pcall(rawget, d, '__index')
    return ok and v ~= nil
  end)
  attack('escape', 'reach the registry through a function upvalue', function()
    return exists('debug.getupvalue')
  end)
  attack('escape', 'change the global table that other roles use', function()
    return rawget(_G, '_G') ~= _G or rawget(_G, '_ENV') ~= nil
  end)
  attack('escape', 'pgtype gives access to internals', function()
    local n = 0
    for k, v in pairs(pgtype) do
      if type(k) == 'string' and k:find('^_') then n = n + 1 end
    end
    return n > 0
  end)
  attack('escape', 'a coroutine that outlives the call has access to the real globals', function()
    local co = coroutine.wrap(function() return _G end)
    return co() ~= _G
  end)
  attack('escape', 'string.format with %n', function()
    return pcall(string.format, '%n', 1)
  end)
  attack('escape', 'string.unpack past the end of the string', function()
    local ok = pcall(string.unpack, 'i4', 'ab', 1)
    local ok2 = pcall(string.unpack, 'c10', 'ab')
    local ok3 = pcall(string.unpack, 'z', 'abc')
    return ok or ok2 or ok3
  end)
  attack('escape', 'string.sub or string.byte read beyond the string', function()
    return #string.sub('abc', 1, 1e9) ~= 3 or select('#', string.byte('abc', 1, 1e5)) ~= 3
  end)
  attack('escape', 'utf8 functions read beyond the string', function()
    local ok, v = pcall(utf8.codepoint, 'a', 1, 100)
    return ok
  end)

  -- sort the results so that the output is stable
  table.sort(results, function(a, b)
    if a[1] ~= b[1] then return a[1] < b[1] end
    return a[2] < b[2]
  end)
  for _, r in ipairs(results) do coroutine.yield(r[1], r[2], r[3]) end
$$;
SELECT * FROM pll_attack_corpus();


-- objects kept after the call, the transaction or the table they came from
-- are gone must give errors or valid data, never stale memory
CREATE TABLE pll_keep (a integer, b text, c numeric[]);
INSERT INTO pll_keep SELECT i, repeat('x', i), ARRAY[i, i + 1] FROM generate_series(1, 20) i;
CREATE FUNCTION pll_keep_save() RETURNS void LANGUAGE pl_lua AS $$
  _G.res = spi.execute("SELECT * FROM pll_keep ORDER BY a")
  _G.row = _G.res[3]
  _G.arr = _G.row.c
  _G.cur = spi.newcursor()
  _G.cur:open("SELECT * FROM pll_keep ORDER BY a")
  _G.cur_named = spi.newcursor('pll_keep_cur')
  _G.cur_named:open("SELECT * FROM pll_keep ORDER BY a")
  _G.stmt = spi.prepare("SELECT * FROM pll_keep WHERE a > $1", { 'integer' })
  _G.it = _G.res[1]
  _G.pairsfn = pairs(_G.arr)
$$;
CREATE FUNCTION pll_keep_use() RETURNS text LANGUAGE pl_lua AS $$
  local out = {}
  local function try(name, f)
    local ok, v = pcall(f)
    out[#out + 1] = name .. '=' .. (ok and 'ok' or 'error')
  end
  try('result', function() return #_G.res, _G.res[20].b, tostring(_G.res[1].c) end)
  try('row', function() return _G.row.a, _G.row.b, #_G.row end)
  try('array', function() return #_G.arr, _G.arr[1], tostring(_G.arr) end)
  try('pairs', function() for k, v in _G.pairsfn, _G.arr do end end)
  try('cursor', function() return _G.cur:fetch(3) end)
  try('named cursor', function() return _G.cur_named:fetch(3) end)
  try('statement', function() return #_G.stmt:execute(5) end)
  try('statement cursor', function()
    return _G.stmt:getcursor(5):fetch(2)
  end)
  return table.concat(out, ' ')
$$;
SELECT pll_keep_save();
SELECT pll_keep_use();
-- again in a new transaction, after the cursors were closed by it
BEGIN;
SELECT pll_keep_use();
ROLLBACK;
-- after the table is changed and dropped
ALTER TABLE pll_keep ADD COLUMN d integer;
SELECT pll_keep_use();
ALTER TABLE pll_keep DROP COLUMN b;
SELECT pll_keep_use();
DROP TABLE pll_keep;
SELECT pll_keep_use();
-- and after the results were made in a subtransaction that was rolled back
CREATE FUNCTION pll_keep_sub() RETURNS text LANGUAGE pl_lua AS $$
  local keep
  pcall(function()
    keep = spi.execute("SELECT 1 AS a, 'x'::text AS b, ARRAY[1,2] AS c")
    error('rollback')
  end)
  collectgarbage()
  spi.execute("SELECT generate_series(1, 1000)")
  return tostring(keep[1].a) .. ' ' .. keep[1].b .. ' ' .. tostring(#keep[1].c)
$$;
SELECT pll_keep_sub();
DROP FUNCTION pll_keep_save();
DROP FUNCTION pll_keep_use();
DROP FUNCTION pll_keep_sub();
RESET SESSION AUTHORIZATION;
DROP OWNED BY regress_pll_attacker;
DROP ROLE regress_pll_attacker;
