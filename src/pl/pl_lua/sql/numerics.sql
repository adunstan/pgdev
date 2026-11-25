--
-- numeric values
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

CREATE FUNCTION nm_arith(a numeric, b numeric) RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(a + b)
  r[#r + 1] = tostring(a - b)
  r[#r + 1] = tostring(a * b)
  r[#r + 1] = tostring(a / b)
  r[#r + 1] = tostring(a % b)
  r[#r + 1] = tostring(a // b)
  r[#r + 1] = tostring(a ^ 2)
  r[#r + 1] = tostring(-a)
  return table.concat(r, " ")
$$;
SELECT nm_arith(7.5, 2), nm_arith(-7.5, 2), nm_arith(7, -2);
SELECT nm_arith(1, 0);

-- exactness
CREATE FUNCTION nm_exact() RETURNS text LANGUAGE pl_lua AS $$
  local x = pgtype.numeric("0.1") + pgtype.numeric("0.2")
  local y = 0.1 + 0.2
  return tostring(x) .. " " .. tostring(y == 0.3) .. " " .. tostring(x == pgtype.numeric("0.3"))
$$;
SELECT nm_exact();

-- mixed arithmetic with Lua numbers gives numeric
CREATE FUNCTION nm_mixed(a numeric) RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(a * 3)
  r[#r + 1] = tostring(3 * a)
  r[#r + 1] = tostring(a + 0.25)
  r[#r + 1] = tostring(10 - a)
  r[#r + 1] = pgtype(a * 3):name()
  r[#r + 1] = tostring(a == 1.10)
  r[#r + 1] = tostring(a < 2)
  r[#r + 1] = tostring(a <= 1.10)
  r[#r + 1] = tostring(a > 1)
  r[#r + 1] = tostring(a >= 3)
  return table.concat(r, " ")
$$;
SELECT nm_mixed(1.10);
CREATE FUNCTION nm_bad() RETURNS text LANGUAGE pl_lua AS $$
  local a = pgtype.numeric(1)
  return tostring(a + {})
$$;
SELECT nm_bad();

-- equality is between numerics only
CREATE FUNCTION nm_eq() RETURNS text LANGUAGE pl_lua AS $$
  local a, b = pgtype.numeric("1.0"), pgtype.numeric("1.00")
  local num = require 'pl_lua.numeric'
  return tostring(a == b) .. " " .. tostring(a == 1) .. " " .. tostring(num.equal(a, 1)) ..
         " " .. tostring(a:equal(b)) .. " " .. tostring(rawequal(a, b))
$$;
SELECT nm_eq();

-- module functions and methods
CREATE FUNCTION nm_funcs() RETURNS text LANGUAGE pl_lua AS $$
  local num = require 'pl_lua.numeric'
  local x = pgtype.numeric("-3.14159")
  local r = {}
  r[#r + 1] = tostring(x:abs())
  r[#r + 1] = tostring(x:ceil())
  r[#r + 1] = tostring(x:floor())
  r[#r + 1] = tostring(x:round(2))
  r[#r + 1] = tostring(x:round())
  r[#r + 1] = tostring(x:trunc(3))
  r[#r + 1] = tostring(x:trunc())
  r[#r + 1] = tostring(x:sign())
  r[#r + 1] = tostring(num.sqrt(16))
  r[#r + 1] = tostring(num.exp(0))
  r[#r + 1] = tostring(num.log(1))
  r[#r + 1] = tostring(num.log(2, 8):round(5))
  r[#r + 1] = tostring(x:isnan()) .. tostring(num.isnan("NaN"))
  r[#r + 1] = tostring(num.round(2.5)) .. tostring(num.round(-2.5))
  r[#r + 1] = tostring(num.new(5)) .. tostring(num.new("5.50"))
  return table.concat(r, " ")
$$;
SELECT nm_funcs();
CREATE FUNCTION nm_sqrt_neg() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.numeric(-1):sqrt())
$$;
SELECT nm_sqrt_neg();

-- conversion back to Lua numbers
CREATE FUNCTION nm_tolua() RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  local function show(v) return v == nil and "nil" or (math.type(v) .. ":" .. tostring(v)) end
  r[#r + 1] = show(pgtype.numeric(5):tointeger())
  r[#r + 1] = show(pgtype.numeric("5.0"):tointeger())
  r[#r + 1] = show(pgtype.numeric("5.5"):tointeger())
  r[#r + 1] = show(pgtype.numeric("1e30"):tointeger())
  r[#r + 1] = show(pgtype.numeric("NaN"):tointeger())
  r[#r + 1] = show(pgtype.numeric("9223372036854775807"):tointeger())
  r[#r + 1] = show(pgtype.numeric("9223372036854775808"):tointeger())
  r[#r + 1] = show(pgtype.numeric("2.5"):tonumber())
  r[#r + 1] = show(pgtype.numeric(7):tonumber())
  return table.concat(r, " ")
$$;
SELECT nm_tolua();

-- NaN and infinity
CREATE FUNCTION nm_special() RETURNS text LANGUAGE pl_lua AS $$
  local nan = pgtype.numeric("NaN")
  return tostring(nan) .. " " .. tostring(nan + 1) .. " " .. tostring(pgtype.numeric("Infinity") - 1)
$$;
SELECT nm_special();

-- numerics as arguments and results
CREATE FUNCTION nm_ret(a numeric) RETURNS numeric STRICT LANGUAGE pl_lua AS $$
  return a * 2
$$;
SELECT nm_ret(1.25), nm_ret(NULL) IS NULL;
CREATE FUNCTION nm_ret_typmod() RETURNS numeric(6,2) LANGUAGE pl_lua AS $$
  return pgtype.numeric("1.2345")
$$;
SELECT nm_ret_typmod();

-- big numbers keep their precision
CREATE FUNCTION nm_big() RETURNS text LANGUAGE pl_lua AS $$
  local x = pgtype.numeric("123456789012345678901234567890")
  return tostring(x * x)
$$;
SELECT nm_big();

-- numerics as table keys are not useful, but must not crash
CREATE FUNCTION nm_key() RETURNS text LANGUAGE pl_lua AS $$
  local t = {}
  local a = pgtype.numeric(1)
  t[a] = "x"
  return tostring(t[pgtype.numeric(1)]) .. tostring(t[a])
$$;
SELECT nm_key();

-- the limits of Lua integers
CREATE FUNCTION nm_limits() RETURNS text LANGUAGE pl_lua AS $$
  local num = require 'pl_lua.numeric'
  local function show(v) return v == nil and "nil" or math.type(v) end
  return tostring(num.maxinteger) .. " " .. tostring(num.mininteger) .. " " ..
         show(num.tointeger(num.maxinteger)) .. " " .. show(num.tointeger(num.maxinteger + 1)) .. " " ..
         show(num.tointeger(num.mininteger)) .. " " .. show(num.tointeger(num.mininteger - 1))
$$;
SELECT nm_limits();
