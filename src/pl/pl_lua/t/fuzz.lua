-- Fuzz driver for PL/Lua, the body of a trusted PL/Lua function:
--
--   CREATE FUNCTION pl_lua_fuzz(seed integer, iterations integer, trace boolean)
--     RETURNS text LANGUAGE pl_lua AS $$ <this file> $$;
--
-- It calls the facilities of PL/Lua at random, with values chosen to be
-- awkward (boundary numbers, invalid strings, hostile tables), and feeds the
-- objects it makes back into later calls, so that objects of the wrong kind,
-- stale objects and closed cursors are used as arguments.  Every call is
-- protected, so an error is expected; what is looked for is a crash, a
-- sanitizer report or an assertion failure in the server.  The same seed
-- repeats the same sequence, and with trace on, each action is written to
-- the server log first, so that the last one logged before a crash is the
-- culprit.  Objects of types created by the setup (see 003_fuzz.pl) are
-- used, and changed by DDL, while objects of them are alive.

-- seed, iterations and trace are the parameters of the function
math.randomseed(seed)
local random, unpack = math.random, table.unpack

local objs = {}					-- objects made so far, for use as arguments
local NOBJS = 60
local seed_pool				-- refills the pool, defined below

local function pick(t) return t[random(#t)] end
local function chance(n) return random(n) == 1 end

local function remember(v)
  if v ~= nil and type(v) ~= 'number' and type(v) ~= 'boolean' then
    if #objs >= NOBJS then objs[random(#objs)] = v else objs[#objs + 1] = v end
  end
  return v
end

local function obj()
  if #objs < 20 and seed_pool then pcall(seed_pool) end
  if #objs == 0 then return nil end
  return objs[random(#objs)]
end

local function rbytes(n)
  local t = {}
  for i = 1, n do t[i] = string.char(random(0, 255)) end
  return table.concat(t)
end

-- values: a function that makes one, so that tables are fresh
local int_edge = { 0, 1, -1, 2, 3, 7, 127, 128, 255, 256, 32767, 32768, -32768,
  2147483647, 2147483648, -2147483648, -2147483649, 4294967296,
  math.maxinteger, math.mininteger, math.maxinteger - 1, 1 << 40, 1 << 62 }
local float_edge = { 0.0, -0.0, 0.5, -0.5, 1.5, 1e15, 1e16, 1e308, -1e308,
  5e-324, 1/0, -1/0, 0/0, 2^53, 2^63, -2^63, 2^31, 123456789.123456789 }
local str_edge = { '', ' ', 'a', 'abc', 'abcd', '1', '-1', '+1', '1e5', '1e500',
  ' 12 ', '0x10', 'NaN', 'Infinity', '-infinity', 'true', 'false', 't', 'f',
  'null', 'NULL', '{}', '[]', '{1,2}', '{{1,2},{3,4}}', '{a,"b c",NULL}',
  '(1,abc,2.5)', '(1,"x y",,{1})', '[1,5)', '(,)', 'empty', '[1,2]',
  '{"a":1}', '[1,2,3]', '"s"', 'null', '2026-01-01', '2026-02-30', '12:30:15',
  '2026-03-08 12:30:15+01', '1 day 2 hours', 'infinity', '-infinity', 'epoch',
  'a\0b', '\xff', '\xc3\x28', '\xef\xbf\xbe', '\\x6100', '\\xZZ', '10.0.0.1/8',
  '12345678-1234-1234-1234-123456789abc', '(1,2)', '<a/>', "it's", 'a"b',
  'a\\b', ('x'):rep(100), ('x'):rep(10000), ('9'):rep(500), ('{'):rep(300),
  ('['):rep(300), '$1', ';', '--', 'a,b', 'sad', 'ok', 'a', 'b', 'c' }
-- (no huge integer keys: a table of them makes an array as long as the
-- largest key, which is allowed, and could take all the memory there is)
local keys = { 1, 2, 3, 0, -1, 4, 100, 'a', 'b', 'c', 'd', 'e', 'n',
  'arr', 'r', 'z', 'w', 'id', 'lower', 'upper', 'isempty', 'year', 'month',
  'day', 'hour', 'min', 'sec', 'usec', 'epoch', 'dow', 'doy', 'week',
  'isoweek', 'timezone', 'nosuchfield', '', 'x', 1.5, 0/0, true }

local hostile

local function value(depth)
  depth = depth or 0
  local r = random(30)
  if r == 1 then return nil
  elseif r == 2 then return true
  elseif r == 3 then return false
  elseif r <= 7 then return pick(int_edge)
  elseif r <= 9 then return pick(float_edge)
  elseif r <= 14 then return pick(str_edge)
  elseif r == 15 then return rbytes(random(0, 40))
  elseif r <= 20 then return obj()
  elseif r <= 24 then
    if depth > 3 then return nil end
    local t = {}
    for i = 1, random(0, 5) do
      if chance(4) then t[pick(keys)] = value(depth + 1) else t[i] = value(depth + 1) end
    end
    if chance(6) then t.n = random(0, 8) end
    if chance(10) then t[t] = t end
    return t
  elseif r == 25 then return hostile(depth)
  elseif r == 26 then return function() return value(depth + 1) end
  elseif r == 27 then return coroutine.create(function() end)
  elseif r == 28 then return pick(objs)
  else return random(-10, 10) end
end

-- side effects that hostile code runs while the server code is in the
-- middle of something
local mutators
local ddl

local function mutate()
  return pick(mutators)()
end

hostile = function(depth)
  local t, mt = {}, {}
  for i = 1, random(0, 3) do t[i] = value((depth or 0) + 2) end
  if chance(2) then mt.__index = function(_, k) mutate(); return value(3) end end
  if chance(3) then mt.__newindex = function() mutate() end end
  if chance(2) then mt.__len = function() mutate(); return pick({ 0, 1, 3, 10, 1000, -1, 1.5, 'x', 2^40 }) end end
  if chance(3) then
    mt.__pairs = function(t)
      local n = 0
      return function() n = n + 1; mutate(); if n < 5 then return pick(keys), value(3) end end, t, nil
    end
  end
  if chance(3) then mt.__tostring = function() mutate(); return pick({ 'x', 1, {}, '', ('y'):rep(5000), true }) end end
  if chance(5) then mt.__call = function() mutate(); return value(3) end end
  if chance(5) then mt.__eq = function() mutate(); return chance(2) end end
  if chance(5) then mt.__lt = function() mutate(); return chance(2) end end
  if chance(5) then mt.__concat = function() mutate(); return value(3) end end
  if chance(5) then mt.__close = function() mutate() end end
  if chance(8) then mt.__metatable = 'locked' end
  pcall(setmetatable, t, mt)
  return t
end

-- type names to construct: some do not exist, some are pseudo-types
local types = { 'integer', 'smallint', 'bigint', 'oid', 'numeric', 'numeric(5,2)',
  'float4', 'float8', 'text', 'varchar(3)', 'char(2)', 'name', 'bytea', 'boolean',
  'date', 'time', 'timetz', 'timestamp', 'timestamptz', 'interval', 'json',
  'jsonb', 'xml', 'uuid', 'inet', 'cidr', 'macaddr', 'point', 'box', 'bit(3)',
  'varbit', 'tsvector', 'tsquery', 'int4range', 'int8range', 'numrange',
  'tsrange', 'daterange', 'int4multirange', 'integer[]', 'text[]',
  'numeric[]', 'jsonb[]', 'date[]', 'integer[][]', 'fz_comp', 'fz_comp[]',
  'fz_domain', 'fz_enum', 'fz_nested', 'fz_t', 'record', 'oidvector',
  'int2vector', 'regclass', 'regtype', 'internal', 'void', 'cstring', 'any',
  'anyarray', 'unknown', 'trigger', 'pg_lsn', 'money', 'nosuchtype', 'pg_class',
  'pg_type', 'information_schema.sql_identifier' }

local function ti(name)
  name = name or pick(types)
  local ok, t = pcall(function() return pgtype[name] end)
  if ok then return t end
end

local function args(n)
  local t = {}
  for i = 1, n do t[i] = value() end
  return t, n
end

-- valid text forms for the types above, so that most actions get past the
-- first check of their arguments
local good = {
  integer = { '0', '1', '-1', '2147483647' }, smallint = { '1', '-32768' },
  bigint = { '1', '9223372036854775807' }, oid = { '1259' },
  numeric = { '1.5', '-0.001', 'NaN', '123456789012345678901234567890' },
  ['numeric(5,2)'] = { '123.45', '1' }, float4 = { '1.5' }, float8 = { '1e300', '-0' },
  text = { 'abc', '' }, ['varchar(3)'] = { 'ab' }, ['char(2)'] = { 'a' }, name = { 'abc' },
  bytea = { '\\x6100ff', '\\x' }, boolean = { 't', 'false' }, date = { '2026-01-02', 'infinity' },
  time = { '12:30:15.25' }, timetz = { '12:30:15+01' }, timestamp = { '2026-03-08 12:30:15' },
  timestamptz = { '2026-03-08 12:30:15+01', 'epoch' }, interval = { '1 day 2 hours', '-1 month' },
  json = { '{"a": [1, 2, null]}' }, jsonb = { '{"a": [1, 2, null], "b": {"c": "d"}}', '[]', '"s"', '1.5', 'null' },
  xml = { '<a>b</a>' }, uuid = { '12345678-1234-1234-1234-123456789abc' },
  inet = { '10.0.0.1/8' }, cidr = { '10.0.0.0/8' }, macaddr = { '08:00:2b:01:02:03' },
  point = { '(1,2)' }, box = { '(1,2),(3,4)' }, ['bit(3)'] = { '101' }, varbit = { '1010' },
  tsvector = { "'a' 'b':2" }, tsquery = { "'a' & 'b'" }, int4range = { '[1,5)', 'empty', '(,)' },
  int8range = { '[1,5]' }, numrange = { '(1.5,2.5]' }, tsrange = { '[2026-01-01,2026-02-01)' },
  daterange = { '[2026-01-01,2026-02-01)' }, int4multirange = { '{[1,3),[5,7)}' },
  ['integer[]'] = { '{1,2,3}', '{}', '{NULL,1}' }, ['text[]'] = { '{a,"b c",NULL}' },
  ['numeric[]'] = { '{1.5,2}' }, ['jsonb[]'] = { '{"{\\"a\\":1}",NULL}' },
  ['date[]'] = { '{2026-01-01}' }, ['integer[][]'] = { '{{1,2},{3,4}}' },
  fz_comp = { '(1,abc,1.5,"{1,2}","{}")', '(,,,,)' }, ['fz_comp[]'] = { '{"(1,a,1.5,,)",NULL}' },
  fz_domain = { '5' }, fz_enum = { 'a', 'c' },
  fz_nested = { '("(1,a,1.5,,)","{}","[1,2)")' }, regclass = { 'pg_class' },
  regtype = { 'integer' }, pg_lsn = { '0/16B3748' }, money = { '1.50' },
}

local function goodstr(name)
  local g = good[name]
  if g then return pick(g) end
  return pick(str_edge)
end

-- arguments for a constructor of the type, of the right shape for it
local function smart_args(name)
  local r = random(10)
  if r <= 4 then return { goodstr(name) }, 1 end
  if r == 5 then
    if name:find('range') then return { value(), value(), pick({ '[)', '(]', '[]', '()', 'x' }) }, 3 end
    if name:find('%[') then
      local t = {}
      for i = 1, random(0, 4) do t[i] = pick({ 1, 2, 3, 'a', 1.5, false }) end
      if chance(3) then t = { t, { 3, 4 } } end
      return chance(2) and { t } or { t, random(0, 3), random(0, 3) }, 1
    end
    if name:find('fz_') then
      return { { a = value(), b = pick({ 'ab', 'abcdef', 1 }), c = pick({ 1.5, 999.99, 1000, 'x' }),
                 d = { 1, 2, value() }, e = { k = { 1, 2 } }, n = value(), arr = { value() }, r = value() } }, 1
    end
    return { value() }, 1
  end
  if r == 6 and name:find('fz_comp') then return { value(), value(), value(), value(), value() }, 5 end
  if r == 7 then return { { year = 2026, month = random(-2, 14), day = random(-5, 35),
                             hour = random(-3, 30), min = random(0, 70), sec = pick({ 0, 1.5, 59.999999, 61 }),
                             timezone = pick({ true, 'UTC', 'America/New_York', 3600, '+0100', 'bogus' }),
                             epoch = chance(3) and pick({ 0, 1e9, 1/0, 2^40 }) or nil } }, 1 end
  local a, n = args(random(0, 3))
  return a, n
end

-- valid binary forms, from the types' own send functions
local bin = {}
local function load_bins()
  local qs = { integer = 'int4send(42)', smallint = 'int2send(7)', bigint = 'int8send(1234567890123)',
    numeric = 'numeric_send(1.5)', text = 'textsend(\'abc\')', bytea = 'byteasend(\'\\x6100\'::bytea)',
    boolean = 'boolsend(true)', date = 'date_send(\'2026-01-02\')', time = 'time_send(\'12:30:15\')',
    timestamptz = 'timestamptz_send(now())', timestamp = 'timestamp_send(now()::timestamp)',
    interval = 'interval_send(\'1 day\')', jsonb = 'jsonb_send(\'{"a":[1,2]}\')',
    float8 = 'float8send(1.5)', uuid = 'uuid_send(gen_random_uuid())', ['integer[]'] = 'array_send(\'{1,2,3}\'::int[])',
    ['text[]'] = 'array_send(\'{a,b}\'::text[])', int4range = 'range_send(int4range(1,5))',
    fz_comp = 'record_send(row(1,\'abc\',1.5,\'{1}\',\'{}\')::fz_comp)', inet = 'inet_send(\'10.0.0.1\')' }
  for t, q in pairs(qs) do
    local ok, r = pcall(spi.execute, 'select ' .. q .. ' as b')
    if ok and r[1] then bin[t] = r[1].b end
  end
end

local function mutate_bytes(b)
  if b == nil then return rbytes(random(0, 20)) end
  local r = random(6)
  if r == 1 then return b:sub(1, random(0, #b))
  elseif r == 2 then return b .. rbytes(random(1, 8))
  elseif r == 3 and #b > 0 then
    local i = random(#b)
    return b:sub(1, i - 1) .. string.char(random(0, 255)) .. b:sub(i + 1)
  elseif r == 4 and #b > 3 then
    local i = random(#b - 3)
    return b:sub(1, i - 1) .. '\xff\xff\xff\xff' .. b:sub(i + 4)
  elseif r == 5 and #b > 3 then
    local i = random(#b - 3)
    return b:sub(1, i - 1) .. '\x7f\xff\xff\xff' .. b:sub(i + 4)
  end
  return b
end

-- how many parameters a query has, so that it can be given that many values
local function nparams(q)
  local m = 0
  for n in q:gmatch('%$(%d+)') do m = math.max(m, tonumber(n)) end
  return m
end

-- a receiver for an operation: usually an object that was made before
local function recv()
  if chance(6) then return value() end
  local o = obj()
  if type(o) ~= 'userdata' and not chance(4) then o = obj() end
  return o
end

-- a datum of the named type from the pool, if there is one
local function of_type(name)
  for _ = 1, 8 do
    local o = obj()
    if type(o) == 'userdata' then
      local ok, t = pcall(pgtype, o)
      if ok and t then
        local ok2, n = pcall(t.name, t)
        if ok2 and n == name then return o end
      end
    end
  end
end

local function datum_of(name)
  local o = of_type(name)
  if o ~= nil then return o end
  local t = ti(name)
  if t and good[name] then
    local ok, d = pcall(t.fromstring, t, pick(good[name]))
    if ok then return d end
  end
end

-- an object to try an operation that needs a particular kind of object
-- a key that suits the object: integers for an array, column names for a row
local function key_for(o)
  local ok, kind = pcall(function() return pgtype(o):kind() end)
  if ok and kind == 'array' then return pick({ 1, 2, 3, 4, 0, -1, 7 }) end
  if ok and kind == 'composite' then return pick({ 'a', 'b', 'c', 'd', 'e', 'n', 'arr', 'r', 1, 2, 3, 4, 5 }) end
  if ok and kind == 'range' then return pick({ 'lower', 'upper', 'isempty', 'lower_inc', 'upper_inc' }) end
  return pick(keys)
end

local function composite_like()
  local name = pick({ 'fz_comp', 'fz_comp', 'jsonb', 'integer[]', 'text[]', 'integer[][]', 'fz_nested', 'int4range' })
  return datum_of(name) or recv()
end

local function numeric_value()
  local r = random(5)
  if r == 1 then return pgtype.numeric(pick(int_edge))
  elseif r == 2 then return pgtype.numeric(pick(float_edge))
  elseif r == 3 then return of_type('numeric') or pgtype.numeric(7)
  else return pgtype.numeric:fromstring(pick(good.numeric)) end
end

local methods = { 'round', 'trunc', 'floor', 'ceil', 'abs', 'sqrt', 'log', 'exp',
  'sign', 'isnan', 'tointeger', 'tonumber', 'equal', 'as_table', 'name',
  'element', 'kind', 'fromstring', 'frombinary', 'dump', 'own', 'disown',
  'close', 'isopen', 'isowned', 'fetch', 'move', 'rows', 'open', 'execute',
  'execute_count', 'getcursor', 'numargs', 'argtype' }

local queries = { 'select $1', 'select $1::int', 'select $1::text',
  'select $1::numeric', 'select $1::jsonb', 'select $1, $2', 'select $1 -> $2',
  'select * from fz_t', 'select c from fz_t', 'select (c).a from fz_t',
  'select ARRAY[$1,$2]', 'select $1::fz_comp', 'select $1::fz_comp[]',
  'select row($1, $2)::fz_comp', 'select $1::int4range',
  'insert into fz_t(id, c, v) values ($1::int, $2::fz_comp, $3) on conflict do nothing',
  'update fz_t set c = $1 where id < 3', 'delete from fz_t where id = $1::int and id > 5',
  'select generate_series(1, least($1::int, 50))', 'select now()', "select '{}'::jsonb",
  'select pg_typeof($1)', 'select $1 = $2', 'select count(*) from fz_t',
  'values (1),(2),(3)', 'select 1/0', 'selec', '', 'select 1; select 2',
  'select $1::date', 'select $1::timestamptz', 'select $1::interval',
  'select $1::bytea', 'select $1::int[]', 'select $1::fz_nested',
  'select $1::fz_domain', 'select * from fz_t where v = $1',
  'select (c).* from fz_t', 'select $1::record', 'select t from fz_t t',
  'select array_agg(c) from fz_t', 'select to_jsonb(c) from fz_t' }

local cursors = {}

local actions = {}
local function act(name, f) actions[#actions + 1] = { name = name, f = f } end

act('construct', function()
  local name = pick(types)
  local t = ti(name)
  local a, n
  if chance(4) then a, n = args(random(0, 4)) else a, n = smart_args(name) end
  return remember(t(unpack(a, 1, n)))
end)
act('construct1', function()
  local t = ti()
  return remember(t(value()))
end)
act('fromstring', function()
  local name = pick(types)
  local t = ti(name)
  return remember(t:fromstring(chance(5) and value() or goodstr(name)))
end)
act('frombinary', function()
  local name = pick(types)
  local t = ti(name)
  return remember(t:frombinary(mutate_bytes(bin[name])))
end)
act('frombinary-good', function()
  local names = {}
  for k in pairs(bin) do names[#names + 1] = k end
  table.sort(names)			-- the order of pairs() differs between runs
  local name = pick(names)
  return remember(ti(name):frombinary(bin[name]))
end)
act('array-construct', function()
  local t = ti('integer')
  local a, n = args(random(0, 4))
  return remember(pgtype.array[pick({ 'integer', 'text', 'numeric', 'jsonb', 'fz_comp', 'date', 'bytea' })](unpack(a, 1, n)))
end)
act('tostring', function() return tostring(value()) end)
act('index', function() local o = recv(); return remember(o[pick(keys)]) end)
-- the first index of a two-level access: a key that gives something
-- that can be indexed again, when the object has one
local function inner(o)
  for _ = 1, 10 do
    local k = pick(keys)
    local ok, v = pcall(function() return o[k] end)
    if ok and (type(v) == 'userdata' or type(v) == 'table') then return v, k end
  end
end
act('index2', function()
  local o = chance(6) and recv() or composite_like()
  local v = inner(o)
  if v == nil then v = o end
  return remember(v[pick(keys)])
end)
act('newindex', function()
  local o = chance(6) and recv() or composite_like()
  o[chance(5) and pick(keys) or key_for(o)] = value()
end)
act('newindex2', function()
  local o = chance(6) and recv() or composite_like()
  local v = inner(o)
  if v == nil then v = o end
  v[chance(5) and pick(keys) or key_for(v)] = value()
end)
act('len', function() return #recv() end)
act('pairs', function()
  local n = 0
  for k, v in pairs(recv()) do n = n + 1; remember(v); if n > 50 then break end end
end)
act('ipairs', function()
  local n = 0
  for k, v in ipairs(recv()) do n = n + 1; remember(v); if n > 50 then break end end
end)
act('call-options', function()
  local o = chance(8) and recv() or composite_like()
  local opts = { map = pick({ function(...) mutate(); return value() end,
                              function(...) return ... end, 5, 'x' }),
                 null = value(), discard = chance(3), pg_numeric = chance(2),
                 norecurse = chance(2) }
  if chance(3) then return remember(o(value())) end
  return remember(o(opts))
end)
act('arith', function()
  local a, b = recv(), recv()
  if chance(2) then a, b = numeric_value(), chance(4) and value() or numeric_value() end
  local op = random(12)
  if op == 1 then return remember(a + b) elseif op == 2 then return remember(a - b)
  elseif op == 3 then return remember(a * b) elseif op == 4 then return remember(a / b)
  elseif op == 5 then return remember(a // b) elseif op == 6 then return remember(a % b)
  elseif op == 7 then return remember(a ^ b) elseif op == 8 then return remember(-a)
  elseif op == 9 then return a .. b elseif op == 10 then return a < b
  elseif op == 11 then return a == b else return a <= b end
end)
local numeric_methods = { 'round', 'trunc', 'floor', 'ceil', 'abs', 'sqrt', 'log', 'exp', 'sign',
  'isnan', 'tointeger', 'tonumber', 'equal' }
local typeinfo_methods = { 'name', 'element', 'kind', 'fromstring', 'frombinary' }
local cursor_methods = { 'fetch', 'move', 'rows', 'open', 'isopen', 'close', 'own', 'disown', 'isowned' }
local stmt_methods = { 'execute', 'execute_count', 'rows', 'getcursor', 'numargs', 'argtype' }
act('method', function()
  local o, m
  local r = random(8)
  if r <= 3 then o, m = numeric_value(), pick(numeric_methods)
  elseif r == 4 then o, m = ti(), pick(typeinfo_methods)
  elseif r == 5 then o, m = (#cursors > 0 and pick(cursors) or spi.newcursor()), pick(cursor_methods)
  elseif r == 6 then o, m = spi.prepare(pick(queries), { 'text', 'text' }), pick(stmt_methods)
  elseif r == 7 then o, m = datum_of('timestamptz'), 'as_table'
  else o, m = recv(), pick(methods) end
  if false then
    -- one the object has
    for _ = 1, 12 do
      local ok, f = pcall(function() return o[m] end)
      if ok and f ~= nil then break end
      m = pick(methods)
    end
  end
  local a, n = args(random(0, 3))
  if m == 'round' or m == 'trunc' then a[1] = pick({ 0, 1, 2, -1, 20, 1e9, value() }); n = 1 end
  return remember(o[m](o, unpack(a, 1, n)))
end)
act('numeric-module', function()
  local num = require 'pl_lua.numeric'
  local names = { 'abs', 'ceil', 'equal', 'exp', 'floor', 'isnan', 'sign', 'sqrt',
    'log', 'round', 'trunc', 'tointeger', 'tonumber', 'new' }
  local a, n = args(random(0, 3))
  if chance(2) then a, n = { numeric_value(), chance(2) and value() or numeric_value() }, 2 end
  return remember(num[pick(names)](unpack(a, 1, n)))
end)
act('jsonb-module', function()
  local j = require 'pl_lua.jsonb'
  local names = { 'is_object', 'is_array', 'set_as_object', 'set_as_array',
    'set_as_unknown', 'pairs', 'ipairs', 'type' }
  local a, n = args(random(0, 3))
  if chance(4) then a, n = { of_type('jsonb') or value(), chance(2) or nil }, 2 end
  if chance(5) then a, n = { { a = 1, b = { 2, 3 } } }, 1 end
  local f = j[pick(names)]
  local r = f(unpack(a, 1, n))
  if type(r) == 'function' then for i = 1, 20 do if r() == nil then break end end end
  return remember(r)
end)
act('time-module', function()
  local tm = require 'pl_lua.time'
  local which = pick({ 'new', 'as_table' })
  local a, n
  if chance(6) then
    a, n = args(random(0, 3))
  elseif which == 'new' then
    local f = { year = random(1900, 2100), month = random(-1, 13), day = random(0, 32),
                hour = random(-1, 25), min = random(0, 61), sec = pick({ 0, 1.5, 59 }),
                timezone = pick({ true, 'UTC', 'Europe/Paris', 3600, '+0200' }) }
    if chance(4) then f = { epoch = pick({ 0, 1e9, -1e9, 2^31 }) } end
    a, n = { pick({ 'date', 'timestamptz', 'timestamp', 'interval', 'time', 'timetz' }), f }, 2
  else
    a, n = { datum_of(pick({ 'timestamptz', 'timestamp', 'date', 'interval' })) or value(),
             pick({ 'UTC', 'America/New_York', 3600, 'bogus', true }) }, 2
  end
  return remember(tm[which](unpack(a, 1, n)))
end)
act('error-module', function()
  local e = require 'pl_lua.error'
  return e[pick({ 'type', 'errcode', 'category' })](value())
end)
act('elog', function()
  local a, n = args(random(1, 4))
  if chance(2) then a[1] = pick(str_edge) end
  local level = pick({ 'debug', 'log', 'info', 'notice', 'warning', 'error', 'bogus', 5, 'WARNING' })
  if chance(3) then return spi[pick({ 'debug', 'log', 'info', 'notice', 'warning', 'error' })](unpack(a, 1, n)) end
  return spi.elog(level, unpack(a, 1, n))
end)
act('quote', function()
  return spi[pick({ 'quote_ident', 'quote_literal', 'quote_nullable' })](value())
end)
-- a short description of values, for the trace
local function describe(a, n)
  local d = {}
  for i = 1, n do
    local v = a[i]
    local t = type(v)
    if t == 'table' then
      local c = 0
      for _ in next, v do c = c + 1 end
      d[#d + 1] = ('table(%d entries, n=%s)'):format(c, tostring(rawget(v, 'n')))
    elseif t == 'string' then d[#d + 1] = ('string(%d)'):format(#v)
    else d[#d + 1] = t .. ':' .. (t == 'userdata' and '' or tostring(v)) end
  end
  return table.concat(d, ', ')
end

act('helpers', function()
  local f = pick({ 'encode_array_literal', 'encode_array_constructor',
                   'encode_typed_literal', 'looks_like_number', 'return_query' })
  local a, n = args(random(0, 3))
  if f == 'encode_array_literal' or f == 'encode_array_constructor' then
    if chance(3) == false then
      a[1] = { pick({ 1, 'a', 'b c', false }), pick({ 2, 'd"e', 1.5 }), { 3, 'x' }, chance(2) and nil or 'y', n = random(0, 5) }
      if chance(3) then a[1] = { a[1], a[1] } end
    end
  elseif f == 'encode_typed_literal' then
    a[1], a[2] = value(), pick(types)
    n = 2
  end
  if trace then spi.elog('log', ('  helper %s(%s)'):format(f, describe(a, n))) end
  return remember(spi[f](unpack(a, 1, n)))
end)
act('spi-execute', function()
  local f = pick({ spi.execute, spi.execute_count })
  local q = pick(queries)
  local a, n = args(chance(8) and random(0, 3) or nparams(q))
  local r
  if f == spi.execute_count then r = f(q, value(), unpack(a, 1, n)) else r = f(q, unpack(a, 1, n)) end
  if type(r) == 'table' then
    for i = 1, math.min(#r, 3) do remember(r[i]) end
  end
  return r
end)
act('spi-rows', function()
  local q = pick(queries)
  local a, n = args(chance(8) and random(0, 2) or nparams(q))
  local c = 0
  for r in spi.rows(q, unpack(a, 1, n)) do
    remember(r)
    c = c + 1
    if c > random(0, 4) then break end
    if chance(6) then mutate() end
  end
end)
act('spi-prepare', function()
  local types_arg = {}
  for i = 1, random(0, 3) do
    types_arg[i] = pick({ 'integer', 'text', 'fz_comp', 'jsonb', 'int[]', 'nosuch', pgtype.integer, value() })
  end
  local st = spi.prepare(pick(queries), chance(5) and value() or types_arg,
    chance(3) and { scroll = chance(2), hold = chance(2), fetch_count = random(0, 5),
                    custom_plan = chance(2), generic_plan = chance(2) } or nil)
  remember(st)
  local a, n = args(random(0, 3))
  if chance(2) then remember(st(unpack(a, 1, n))) end
  return st
end)
act('cursor', function()
  local c = (#cursors > 0 and chance(2)) and pick(cursors) or spi.newcursor(chance(3) and pick({ 'fzc1', 'fzc2', '' }) or nil)
  cursors[#cursors + 1] = c
  if #cursors > 8 then table.remove(cursors, 1) end
  remember(c)
  local op = random(10)
  if op == 1 then c:open(pick(queries), value(), value())
  elseif op == 2 then return remember(c:fetch(value(), pick({ 'forward', 'backward', 'next', 'prior', 'absolute', 'relative', value() })))
  elseif op == 3 then c:move(value(), pick({ 'forward', 'absolute', 'bogus' }))
  elseif op == 4 then c:close()
  elseif op == 5 then
    local n = 0
    for r in c:rows() do n = n + 1; remember(r); if n > 3 or chance(3) then break end end
  elseif op == 6 then c:disown()
  elseif op == 7 then c:own()
  elseif op == 8 then return spi.findcursor(pick({ 'fzc1', 'fzc2', '', 'nosuch', value() }))
  elseif op == 9 then c:open(spi.prepare(pick(queries), { 'text', 'text' }), value(), value())
  else return c:isopen(), c:name(), c:isowned() end
end)
act('commit', function() return pick({ spi.commit, spi.rollback, spi.is_atomic, spi.readonly })() end)
act('pcall-nest', function()
  local f = pick({ pcall, xpcall, lpcall })
  if f == xpcall then
    return f(function() mutate(); return value() end, function(e) mutate(); return value() end)
  end
  return f(function() mutate(); error(value()) end)
end)
act('error-throw', function()
  local ok, e = pcall(error, value(), random(0, 3))
  remember(e)
  if chance(2) then error(e) end
end)
act('coroutine', function()
  local co = coroutine.create(function(...)
    mutate()
    local x = coroutine.yield(value())
    mutate()
    error(value())
  end)
  remember(co)
  coroutine.resume(co, value())
  if chance(2) then coroutine.resume(co, value()) end
  if chance(2) then coroutine.close(co) end
  local w = coroutine.wrap(function() coroutine.yield(value()); mutate(); return value() end)
  w(); if chance(2) then w() end
end)
act('load', function()
  local f = load(pick({ 'return ...', 'return pgtype.integer(...)', 'return spi.execute(...)',
                        'local t <close> = nil; return 1', 'return spi' }), 'x', pick({ 't', 'b', 'bt' }), pick({ nil, _G, {}, value() }))
  if f then return remember(f(value())) end
end)
act('gc', function() collectgarbage(pick({ 'collect', 'step', 'count', 'incremental', 'generational' })) end)
act('ddl', function() return ddl() end)

mutators = {
  function() collectgarbage() end,
  function() if #cursors > 0 then pick(cursors):close() end end,
  function() local o = obj(); if o ~= nil then o[pick(keys)] = value() end end,
  function() spi.execute('select 1') end,
  function() ddl() end,
  function() error(value()) end,
  function() for i = #objs, 1, -1 do if chance(3) then table.remove(objs, i) end end end,
  function() local o = obj(); if type(o) == 'userdata' then tostring(o) end end,
  function() pcall(spi.execute, 'select 1/0') end,
  function() local c = spi.newcursor(); c:open('select * from fz_t'); c:fetch(1); remember(c) end,
  function() if #cursors > 0 then pick(cursors):fetch(1) end end,
  function() return end,
}

-- DDL on the types and tables that the objects refer to
local ddl_state = {}
local ddl_list = {
  { 'ALTER TYPE fz_comp ADD ATTRIBUTE z integer CASCADE', 'z' },
  { 'ALTER TYPE fz_comp DROP ATTRIBUTE z CASCADE', 'z' },
  { 'ALTER TABLE fz_t ADD COLUMN w integer', 'w' },
  { 'ALTER TABLE fz_t DROP COLUMN w', 'w' },
  { 'ALTER TABLE fz_t ALTER COLUMN v TYPE varchar(5)' },
  { 'ALTER TABLE fz_t ALTER COLUMN v TYPE text' },
  { 'ALTER TYPE fz_comp ALTER ATTRIBUTE a TYPE bigint CASCADE' },
  { 'ALTER TYPE fz_comp ALTER ATTRIBUTE a TYPE integer CASCADE' },
  { 'ALTER TYPE fz_comp RENAME ATTRIBUTE a TO a2' },
  { 'ALTER TYPE fz_comp RENAME ATTRIBUTE a2 TO a' },
  { 'ALTER DOMAIN fz_domain ADD CONSTRAINT fz_dc CHECK (value < 1000)' },
  { 'ALTER DOMAIN fz_domain DROP CONSTRAINT fz_dc' },
  { 'ALTER TABLE fz_t ALTER COLUMN id TYPE bigint' },
  { 'ALTER TABLE fz_t ALTER COLUMN id TYPE integer' },
  { 'TRUNCATE fz_t' },
  { "INSERT INTO fz_t VALUES (1, ROW(1,'x',1.5,'{1}','{}'), 'v1'), (2, NULL, NULL) ON CONFLICT DO NOTHING" },
}
ddl = function()
  local d = pick(ddl_list)
  return pcall(spi.execute, d[1])
end

-- valid objects of every type, and rows of the test table
local seeded = false
seed_pool = function()
  if not seeded then load_bins(); seeded = true end
  NOBJS = 120
  for _, name in ipairs(types) do
    local t = ti(name)
    if t and good[name] then
      for _, str in ipairs(good[name]) do pcall(function() remember(t:fromstring(str)) end) end
    end
  end
  pcall(function()
    for _, r in ipairs(spi.execute('select * from fz_t')) do
      remember(r); remember(r.c)
    end
    for _, r in ipairs(spi.execute("select array_agg(c) as a, now() as t, '{\"a\":[1]}'::jsonb as j, int4range(1,4) as r from fz_t")) do
      remember(r.a); remember(r.t); remember(r.j); remember(r.r)
    end
  end)
end
seed_pool()

local counts = { ok = 0, err = 0 }
local byaction = {}
local hist = {}
local byerr = {}
for i = 1, iterations do
  local a = pick(actions)
  if trace then spi.elog('log', ('fuzz seed %d iter %d: %s'):format(seed, i, a.name)) end
  local ok, e = pcall(a.f)
  local st = byaction[a.name]
  if st == nil then st = { 0, 0 }; byaction[a.name] = st end
  if ok then
    counts.ok = counts.ok + 1; st[1] = st[1] + 1
  else
    counts.err = counts.err + 1; st[2] = st[2] + 1
    -- an error value can be a hostile table whose __tostring raises errors
    local okm, m = pcall(function()
      return (tostring(e):gsub('^.-:%d+: ', ''):gsub('%d+', 'N'):sub(1, 70))
    end)
    if not okm then m = 'error value that cannot be converted to a string' end
    hist[m] = (hist[m] or 0) + 1
    local key = a.name .. ': ' .. m
    byerr[key] = (byerr[key] or 0) + 1
  end
  if i % 50 == 0 then collectgarbage() end
end

-- a summary: how many actions ran to the end, per action, and the commonest
-- errors, so that a driver that mostly fails early can be seen as one
local out = { ('seed %d: %d actions ok, %d raised errors'):format(seed, counts.ok, counts.err) }
local names = {}
for k in pairs(byaction) do names[#names + 1] = k end
table.sort(names)
local parts = {}
for _, k in ipairs(names) do
  parts[#parts + 1] = ('%s %d/%d'):format(k, byaction[k][1], byaction[k][1] + byaction[k][2])
end
out[#out + 1] = 'ok/total per action: ' .. table.concat(parts, ', ')
local msgs = {}
for m, n in pairs(hist) do msgs[#msgs + 1] = { m, n } end
table.sort(msgs, function(x, y) if x[2] ~= y[2] then return x[2] > y[2] end return x[1] < y[1] end)
for i = 1, math.min(#msgs, 12) do
  out[#out + 1] = ('%5d  %s'):format(msgs[i][2], msgs[i][1])
end
-- the commonest error of each action that mostly fails
local best = {}
for key, n in pairs(byerr) do
  local name = key:match('^(.-): ')
  local st = byaction[name]
  if st and st[1] * 4 < st[1] + st[2] and (best[name] == nil or n > best[name][2]) then
    best[name] = { key, n }
  end
end
local bn = {}
for name in pairs(best) do bn[#bn + 1] = name end
table.sort(bn)
for _, name in ipairs(bn) do out[#out + 1] = ('mostly failing: %5d  %s'):format(best[name][2], best[name][1]) end
return table.concat(out, '\n')
