--
-- Making modules available inside the sandbox
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
CREATE EXTENSION IF NOT EXISTS pl_luau;
RESET client_min_messages;
\set VERBOSITY terse

-- the interpreter has not been created yet, so these settings apply to it;
-- the preload table stands in for modules installed on the file system
SET pl_lua.on_init = $$
  package.preload.tm_plain = function()
    return {
      name = 'plain',
      func = function() return 'called' end,
      sub = { value = 1, func = function() return 'sub called' end },
    }
  end
  package.preload.tm_callable = function()
    return setmetatable({}, { __call = function(_, a) return 'called with ' .. tostring(a) end })
  end
  package.preload.tm_counter = function()
    return { n = 0 }
  end
$$;
SET pl_lua.on_trusted_init = $$
  trusted.allow('tm_plain')
  trusted.allow('tm_callable', 'tm_call')
  trusted.allow('tm_plain', 'tm_direct', 'direct')
  trusted.allow('tm_plain', 'tm_copy', 'copy')
  trusted.allow('tm_plain', 'tm_scopy', 'scopy')
  trusted.allow('tm_plain', 'tm_sproxy', 'sproxy')
  trusted.allow('tm_plain', 'tm_fn', function(m) return 'made from ' .. m.name end)
  trusted.allow('tm_plain', 'tm_gname', 'direct', 'GNAME')
  trusted.allow('tm_plain', 'tm_gtrue', 'direct', true)
  trusted.allow({ 'tm_counter', { 'tm_plain', 'tm_listed', 'direct' } }, nil, 'copy')
  trusted.require('tm_plain', 'tm_required')
  trusted.allow('tm_plain', 'tm_removed')
  trusted.remove('tm_removed')
  trusted.remove('os', true)
  trusted.remove('utf8')
  trusted.allow('tm_plain', 'tm_gremoved', 'direct', true)
  trusted.remove('tm_gremoved', true)
$$;

DO LANGUAGE pl_lua $$
  local plain = require 'tm_plain'
  print(plain.name, plain.func(), plain.sub.func(), plain.sub.value)
  print(require 'tm_call'(5))
  print(require('tm_direct').name, require('tm_fn'))
  print(require('tm_listed').name, require('tm_counter').n)
  print(tm_required.name, tm_required == require 'tm_required')
  print(GNAME.name, tm_gtrue.name, rawget(_G, 'tm_direct'))
  print(pcall(require, 'tm_removed'))
  print((pcall(require, 'tm_gremoved')), tm_gremoved)
  print(os, pcall(require, 'os'))
  print((pcall(require, 'utf8')), utf8 ~= nil)
$$;

-- the modes differ in how well they protect the module
DO LANGUAGE pl_lua $$
  local proxy, direct, copy, scopy, sproxy =
    require 'tm_plain', require 'tm_direct', require 'tm_copy',
    require 'tm_scopy', require 'tm_sproxy'
  print(proxy ~= direct, copy ~= direct, scopy ~= direct, sproxy ~= direct)
  print(getmetatable(proxy), getmetatable(copy), getmetatable(scopy))
  -- assigning in a proxy or a copy does not modify the module
  proxy.name = 'changed by proxy'
  copy.name = 'changed by copy'
  proxy.sub.value = 'changed by proxy'
  copy.sub.value = 'changed by copy'
  print(direct.name, direct.sub.value)
  -- a shallow copy and a shallow proxy share the nested tables
  scopy.sub.value = 'changed by scopy'
  print(direct.sub.value)
  sproxy.sub.value = 'changed by sproxy'
  print(direct.sub.value)
  -- a direct reference is the module itself
  direct.name = 'changed directly'
  print(require('tm_plain').name, require('tm_sproxy').name, require('tm_copy').name)
  -- iteration works over the proxies and the copies
  local function count(t) local n = 0 for _ in pairs(t) do n = n + 1 end return n end
  print(count(proxy), count(copy), count(scopy), count(sproxy), count(direct))
$$;

-- the settings affect only the interpreters created afterwards, and the
-- untrusted language doesn't have the trusted module in the sandbox
DO LANGUAGE pl_luau $$
  print(type(trusted), (pcall(require, 'tm_plain')))
  print(type(require 'os'), type(utf8))
$$;

-- the trusted module is not available inside the sandbox
DO LANGUAGE pl_lua $$
  print(trusted, pcall(require, 'pl_lua.trusted'))
$$;

-- bad arguments
DO LANGUAGE pl_luau $$
  local trusted = require 'pl_lua.trusted'
  print(pcall(trusted.allow, 42))
  print(pcall(trusted.allow, 'tm_plain', 'x', 'nonsense'))
  print(pcall(trusted.allow, 'tm_plain', 42))
  print(pcall(trusted.allow, 'tm_plain', 'x', 'direct', {}))
  print(pcall(trusted.remove, {}))
$$;

-- only superusers can change the initialization settings
CREATE ROLE regress_pll_trusted_user;
SET ROLE regress_pll_trusted_user;
SET pl_lua.on_trusted_init = 'print(1)';
SET pl_lua.on_init = 'print(1)';
SET pl_lua.on_common_init = 'print(1)';
SET pl_lua.install_globals = off;
RESET ROLE;
DROP ROLE regress_pll_trusted_user;
