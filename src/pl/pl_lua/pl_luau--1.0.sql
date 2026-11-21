/* src/pl/pl_lua/pl_luau--1.0.sql */

CREATE FUNCTION pl_luau_call_handler() RETURNS language_handler
  LANGUAGE c AS 'MODULE_PATHNAME';

CREATE FUNCTION pl_luau_inline_handler(internal) RETURNS void
  STRICT LANGUAGE c AS 'MODULE_PATHNAME';

CREATE FUNCTION pl_luau_validator(oid) RETURNS void
  STRICT LANGUAGE c AS 'MODULE_PATHNAME';

CREATE LANGUAGE pl_luau
  HANDLER pl_luau_call_handler
  INLINE pl_luau_inline_handler
  VALIDATOR pl_luau_validator;

COMMENT ON LANGUAGE pl_luau IS 'PL/LuaU untrusted procedural language';
