--
-- The pl_lua.strict setting
--
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;

\set VERBOSITY terse

CREATE FUNCTION strict_off() RETURNS text
LANGUAGE pl_lua AS $$ return tostring(undefined_variable) $$;
SELECT strict_off();

SET pl_lua.strict = on;

CREATE FUNCTION strict_on() RETURNS text
LANGUAGE pl_lua AS $$ return tostring(undefined_variable) $$;
SELECT strict_on();

-- assignment to an undeclared variable is still allowed
CREATE FUNCTION strict_assign() RETURNS integer
LANGUAGE pl_lua AS $$
  count = count + 1
  return count
end
do
  count = 0
$$;
SELECT strict_assign(), strict_assign();

-- the setting applies when the function is compiled
SET pl_lua.strict = off;
SELECT strict_on();
SELECT strict_off();
SET pl_lua.strict = on;
SELECT strict_off();

-- standard names are still found
CREATE FUNCTION strict_globals() RETURNS text
LANGUAGE pl_lua AS $$ return type(string.format) .. type(spi) .. type(pgtype) $$;
SELECT strict_globals();

-- anonymous blocks follow the setting too
DO LANGUAGE pl_lua $$ print(undefined_in_do) $$;
SET pl_lua.strict = off;
DO LANGUAGE pl_lua $$ print(undefined_in_do) $$;
