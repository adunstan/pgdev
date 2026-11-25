/* contrib/hstore_pl_lua/hstore_pl_luau--1.0.sql */

-- complain if script is sourced in psql, rather than via CREATE EXTENSION
\echo Use "CREATE EXTENSION hstore_pl_luau" to load this file. \quit

-- This library resolves the Lua API from pl_lua, which must be loaded first.
LOAD '$libdir/pl_lua';

CREATE FUNCTION hstore_to_pl_luau(val internal) RETURNS internal
LANGUAGE C STRICT IMMUTABLE
AS 'MODULE_PATHNAME', 'hstore_to_pl_lua';

CREATE FUNCTION pl_luau_to_hstore(val internal) RETURNS @extschema:hstore@.hstore
LANGUAGE C STRICT IMMUTABLE
AS 'MODULE_PATHNAME', 'pl_lua_to_hstore';

CREATE TRANSFORM FOR @extschema:hstore@.hstore LANGUAGE pl_luau (
    FROM SQL WITH FUNCTION hstore_to_pl_luau(internal),
    TO SQL WITH FUNCTION pl_luau_to_hstore(internal)
);
