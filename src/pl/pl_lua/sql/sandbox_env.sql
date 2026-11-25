--
-- Global variables of functions in the trusted language
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

-- an assignment to an undeclared variable is private to the function, unless
-- it goes through _G
CREATE FUNCTION sbe_set(v text) RETURNS void LANGUAGE pl_lua
AS $$ shared = v; _G.visible = v $$;
CREATE FUNCTION sbe_get() RETURNS text LANGUAGE pl_lua
AS $$ return tostring(shared) .. '/' .. tostring(visible) $$;
SELECT sbe_set('hello');
SELECT sbe_get();
DO LANGUAGE pl_lua $$ print(shared, visible) $$;
