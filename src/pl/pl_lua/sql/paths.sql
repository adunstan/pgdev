--
-- The pl_lua.paths module
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
CREATE EXTENSION IF NOT EXISTS pl_luau;
RESET client_min_messages;
\set VERBOSITY terse

-- some of the directories might not exist, so check only that each is a
-- string that looks like an absolute path
DO LANGUAGE pl_luau $$
  local paths = require 'pl_lua.paths'
  local names = { 'bin', 'lib', 'pkglib', 'libdir', 'share', 'locale', 'doc',
                  'etc', 'html', 'include', 'includeserver', 'man',
                  'pkginclude' }
  for _, name in ipairs(names) do
    local p = paths[name]()
    print(name, type(p), p:match('^/') ~= nil or p:match('^%a:[/\\]') ~= nil)
  end
  print(paths.libdir() == paths.pkglib())
$$;

-- bin has the postgres executable
DO LANGUAGE pl_luau $$
  local bin = require('pl_lua.paths').bin()
  local f = io.open(bin .. '/postgres', 'r') or io.open(bin .. '/postgres.exe', 'r')
  print(f ~= nil)
$$;

-- not for trusted code
DO LANGUAGE pl_lua $$
  print(pcall(require, 'pl_lua.paths'))
$$;
