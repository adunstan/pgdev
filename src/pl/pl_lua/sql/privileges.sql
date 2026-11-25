--
-- Who can do what
--
\set VERBOSITY terse
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
CREATE EXTENSION IF NOT EXISTS pl_luau;
RESET client_min_messages;
CREATE ROLE regress_pll_u1;
CREATE ROLE regress_pll_u2;
CREATE TABLE pll_secret (v text);
INSERT INTO pll_secret VALUES ('s3cret');
GRANT ALL ON SCHEMA public TO regress_pll_u1, regress_pll_u2;

-- untrusted language: no grants, no creation, no DO blocks
GRANT USAGE ON LANGUAGE pl_luau TO regress_pll_u1;
SET ROLE regress_pll_u1;
CREATE FUNCTION pll_u_f() RETURNS int LANGUAGE pl_luau AS $$ return 1 $$;
DO LANGUAGE pl_luau $$ print(1) $$;
RESET ROLE;

-- a superuser's untrusted function can be run by anyone allowed to execute
-- it, and all of them share the one untrusted interpreter
CREATE FUNCTION pll_su_set(v text) RETURNS void LANGUAGE pl_luau AS $$ _G.shared = v $$;
CREATE FUNCTION pll_su_get() RETURNS text LANGUAGE pl_luau AS $$ return tostring(_G.shared) $$;
GRANT EXECUTE ON FUNCTION pll_su_set(text), pll_su_get() TO regress_pll_u1, regress_pll_u2;
SET ROLE regress_pll_u1;
SELECT pll_su_set('from u1');
RESET ROLE;
SET ROLE regress_pll_u2;
SELECT pll_su_get();
RESET ROLE;

-- trusted language: the module registry is not reachable from sandboxed code
SET SESSION AUTHORIZATION regress_pll_u1;
CREATE FUNCTION pll_u_trusted() RETURNS text LANGUAGE pl_lua AS $$
  local out = {}
  out[#out+1] = type(trusted)
  out[#out+1] = tostring((pcall(require, 'pl_lua.trusted')))
  out[#out+1] = tostring((pcall(require, 'pl_lua')))
  out[#out+1] = tostring((pcall(require, 'os')))
  out[#out+1] = type(package.loadlib) .. '/' .. type(package.searchers) .. '/' .. type(package.cpath)
  out[#out+1] = type(package.loaded.os) .. '/' .. type(package.loaded.io) .. '/' .. type(package.loaded.debug)
  out[#out+1] = type(_G._PL_IDENT_NEW) .. '/' .. type(_G.pgtype) .. '/' .. type(_G.spi)
  return table.concat(out, ' ')
$$;
SELECT pll_u_trusted();

-- the modules that can be loaded are the allowed ones only: no search path,
-- no loading from files
CREATE FUNCTION pll_u_modules() RETURNS text LANGUAGE pl_lua AS $$
  local function keys(t)
    local k = {}
    for n in pairs(t) do k[#k+1] = tostring(n) end
    table.sort(k)
    return table.concat(k, ',')
  end
  local out = { keys(require('os')), keys(package), tostring(package.path) }
  for _, name in ipairs{ 'no_such_module', '../../lib/foo', '/etc/passwd' } do
    local ok, e = pcall(require, name)
    out[#out+1] = tostring(ok) .. ' ' .. (tostring(e):gsub('\n', ' '))
  end
  return table.concat(out, '; ')
$$;
SELECT pll_u_modules();

-- the privileges of the calling role apply to SPI
CREATE FUNCTION pll_u_read() RETURNS text LANGUAGE pl_lua AS $$
  return spi.execute("SELECT v FROM pll_secret")[1].v
$$;
SELECT pll_u_read();
CREATE FUNCTION pll_u_role() RETURNS text LANGUAGE pl_lua AS $$
  local out = {}
  local su = spi.execute("SELECT rolname FROM pg_roles WHERE rolsuper ORDER BY oid LIMIT 1")[1].rolname
  for _, q in ipairs{ "SET ROLE " .. su,
                      "ALTER SYSTEM SET work_mem = '1MB'",
                      "COPY pll_secret TO PROGRAM 'true'", "COPY pll_secret TO '/tmp/pll_x'",
                      "CREATE LANGUAGE x HANDLER plpgsql_call_handler",
                      "SET pl_lua.on_init = 'x'", "CREATE FUNCTION f() RETURNS int LANGUAGE pl_luau AS 'return 1'" } do
    local ok, e = pcall(spi.execute, q)
    out[#out+1] = tostring(ok) .. ' ' .. tostring(type(e) == 'table' and e.sqlstate or e)
  end
  return table.concat(out, ' ')
$$;
SELECT pll_u_role();
RESET SESSION AUTHORIZATION;
-- with the privilege granted, the same function works
GRANT SELECT ON pll_secret TO regress_pll_u1;
SET ROLE regress_pll_u1;
SELECT pll_u_read();
RESET ROLE;

-- without USAGE on the trusted language, no functions
REVOKE USAGE ON LANGUAGE pl_lua FROM PUBLIC;
SET ROLE regress_pll_u2;
CREATE FUNCTION pll_u2_f() RETURNS int LANGUAGE pl_lua AS $$ return 1 $$;
DO LANGUAGE pl_lua $$ print(1) $$;
RESET ROLE;
GRANT USAGE ON LANGUAGE pl_lua TO PUBLIC;

DROP OWNED BY regress_pll_u1, regress_pll_u2;
DROP ROLE regress_pll_u1;
DROP ROLE regress_pll_u2;
DROP TABLE pll_secret;
DROP FUNCTION pll_su_set(text);
DROP FUNCTION pll_su_get();
