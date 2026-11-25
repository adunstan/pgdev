--
-- Interpreter states: one for the untrusted language, and one for each role
-- that runs trusted code
--
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
CREATE EXTENSION IF NOT EXISTS pl_luau;
RESET client_min_messages;

\set VERBOSITY terse

CREATE ROLE regress_pll_interp_a;
CREATE ROLE regress_pll_interp_b;
GRANT USAGE ON LANGUAGE pl_lua TO regress_pll_interp_a, regress_pll_interp_b;

CREATE FUNCTION interp_set(v text) RETURNS void
LANGUAGE pl_lua AS $$ _G.marker = v $$;
CREATE FUNCTION interp_get() RETURNS text
LANGUAGE pl_lua AS $$ return marker $$;
GRANT EXECUTE ON FUNCTION interp_set(text), interp_get() TO regress_pll_interp_a, regress_pll_interp_b;

-- globals set by one role are not visible to another
SET ROLE regress_pll_interp_a;
SELECT interp_set('from a');
SELECT interp_get();
RESET ROLE;
SET ROLE regress_pll_interp_b;
SELECT interp_get();
SELECT interp_set('from b');
SELECT interp_get();
RESET ROLE;
SET ROLE regress_pll_interp_a;
SELECT interp_get();
RESET ROLE;

-- as the superuser, a third state
SELECT interp_get();

-- SECURITY DEFINER functions run in the owner's state
CREATE FUNCTION interp_definer() RETURNS text SECURITY DEFINER
LANGUAGE pl_lua AS $$ return marker $$;
GRANT EXECUTE ON FUNCTION interp_definer() TO regress_pll_interp_a, regress_pll_interp_b;
SET ROLE regress_pll_interp_a;
SELECT interp_definer();
RESET ROLE;
SELECT interp_set('from the owner');
SET ROLE regress_pll_interp_a;
SELECT interp_definer();
SELECT interp_get();
RESET ROLE;

-- the untrusted language has one state, whoever calls it
CREATE FUNCTION interp_u_set(v text) RETURNS void
LANGUAGE pl_luau AS $$ _G.marker = v $$;
CREATE FUNCTION interp_u_get() RETURNS text
LANGUAGE pl_luau AS $$ return marker $$;
SELECT interp_u_set('untrusted');
SELECT interp_u_get();
SELECT interp_get();

-- the languages do not share data
DO LANGUAGE pl_luau $$ print(rawget(_G, 'marker')) $$;

-- configuration settings that run code when an interpreter is created
SET pl_lua.on_common_init = 'initialized_by_setting = "yes"';
CREATE ROLE regress_pll_interp_c;
GRANT USAGE ON LANGUAGE pl_lua TO regress_pll_interp_c;
SET ROLE regress_pll_interp_c;
DO LANGUAGE pl_lua $$ print(initialized_by_setting) $$;
RESET ROLE;
-- existing interpreters are not affected
DO LANGUAGE pl_lua $$ print(initialized_by_setting) $$;
RESET pl_lua.on_common_init;

-- an error in initialization code is reported, and creation is retried later
CREATE ROLE regress_pll_interp_d;
GRANT USAGE ON LANGUAGE pl_lua TO regress_pll_interp_d;
SET pl_lua.on_trusted_init = 'error("init failed")';
SET ROLE regress_pll_interp_d;
DO LANGUAGE pl_lua $$ print("not reached") $$;
RESET ROLE;
RESET pl_lua.on_trusted_init;
SET ROLE regress_pll_interp_d;
DO LANGUAGE pl_lua $$ print("works now") $$;
RESET ROLE;

DROP FUNCTION interp_set(text), interp_get(), interp_definer(),
              interp_u_set(text), interp_u_get();
REVOKE USAGE ON LANGUAGE pl_lua FROM regress_pll_interp_a, regress_pll_interp_b, regress_pll_interp_c, regress_pll_interp_d;
DROP ROLE regress_pll_interp_a, regress_pll_interp_b, regress_pll_interp_c, regress_pll_interp_d;
