--
-- The extension scripts, and the languages they create
--
\set VERBOSITY terse

-- start from scratch, whatever earlier tests created
SET client_min_messages = warning;
DROP EXTENSION IF EXISTS pl_lua CASCADE;
DROP EXTENSION IF EXISTS pl_luau CASCADE;
RESET client_min_messages;

CREATE EXTENSION pl_lua;
CREATE EXTENSION pl_luau;

SELECT lanname, lanpltrusted, lanispl,
       lanplcallfoid::regproc, laninline::regproc, lanvalidator::regproc
  FROM pg_language WHERE lanname LIKE 'pl\_lua%' ORDER BY lanname;

SELECT extname, extversion FROM pg_extension WHERE extname LIKE 'pl\_lua%' ORDER BY extname;

-- the language objects are owned by the bootstrap superuser here
SELECT lanname, pg_get_userbyid(lanowner) = current_user AS owned_by_me
  FROM pg_language WHERE lanname LIKE 'pl\_lua%' ORDER BY lanname;

COMMENT ON LANGUAGE pl_lua IS NULL;
SELECT obj_description(oid, 'pg_language') FROM pg_language WHERE lanname = 'pl_lua';

-- functions in a dropped language go with it only when CASCADE is given
CREATE FUNCTION ext_f() RETURNS integer LANGUAGE pl_lua AS $$ return 1 $$;
DROP EXTENSION pl_lua;
SET client_min_messages = warning;
DROP EXTENSION pl_lua CASCADE;
RESET client_min_messages;
CREATE EXTENSION pl_lua;

-- the extensions are not relocatable, and live in pg_catalog
SELECT extname, extrelocatable, extnamespace::regnamespace
  FROM pg_extension WHERE extname LIKE 'pl\_lua%' ORDER BY extname;

-- a non-superuser may create the trusted language but not the untrusted one
DROP EXTENSION pl_lua;
DROP EXTENSION pl_luau;
CREATE ROLE ext_user LOGIN;
DO $$ BEGIN EXECUTE format('GRANT CREATE ON DATABASE %I TO ext_user', current_database()); END $$;
GRANT CREATE ON SCHEMA public TO ext_user;
SET ROLE ext_user;
CREATE EXTENSION pl_lua;
CREATE EXTENSION pl_luau;
SELECT lanname, pg_get_userbyid(lanowner) AS owner
  FROM pg_language WHERE lanname LIKE 'pl\_lua%' ORDER BY lanname;
CREATE FUNCTION ext_user_f() RETURNS integer LANGUAGE pl_lua AS $$ return 5 $$;
SELECT ext_user_f();
RESET ROLE;
DROP FUNCTION ext_user_f();
DROP EXTENSION pl_lua;
REVOKE CREATE ON SCHEMA public FROM ext_user;
DO $$ BEGIN EXECUTE format('REVOKE CREATE ON DATABASE %I FROM ext_user', current_database()); END $$;
DROP ROLE ext_user;

-- reinstall for the benefit of later tests
CREATE EXTENSION pl_lua;
CREATE EXTENSION pl_luau;
