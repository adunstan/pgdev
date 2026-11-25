/* src/pl/pl_lua/pl_lua--1.0.sql */

CREATE FUNCTION pl_lua_call_handler() RETURNS language_handler
  LANGUAGE c AS 'MODULE_PATHNAME';

CREATE FUNCTION pl_lua_inline_handler(internal) RETURNS void
  STRICT LANGUAGE c AS 'MODULE_PATHNAME';

CREATE FUNCTION pl_lua_validator(oid) RETURNS void
  STRICT LANGUAGE c AS 'MODULE_PATHNAME';

CREATE TRUSTED LANGUAGE pl_lua
  HANDLER pl_lua_call_handler
  INLINE pl_lua_inline_handler
  VALIDATOR pl_lua_validator;

-- The language object, but not the functions, can be owned by a non-superuser.
ALTER LANGUAGE pl_lua OWNER TO @extowner@;

COMMENT ON LANGUAGE pl_lua IS 'PL/Lua procedural language';
