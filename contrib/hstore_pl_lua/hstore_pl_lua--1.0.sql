/* contrib/hstore_pl_lua/hstore_pl_lua--1.0.sql */

-- complain if script is sourced in psql, rather than via CREATE EXTENSION
\echo Use "CREATE EXTENSION hstore_pl_lua" to load this file. \quit

-- This library resolves the Lua API from pl_lua, which must be loaded first.
LOAD '$libdir/pl_lua';

CREATE FUNCTION hstore_to_pl_lua(val internal) RETURNS internal
LANGUAGE C STRICT IMMUTABLE
AS 'MODULE_PATHNAME';

CREATE FUNCTION pl_lua_to_hstore(val internal) RETURNS @extschema:hstore@.hstore
LANGUAGE C STRICT IMMUTABLE
AS 'MODULE_PATHNAME';

CREATE TRANSFORM FOR @extschema:hstore@.hstore LANGUAGE pl_lua (
    FROM SQL WITH FUNCTION hstore_to_pl_lua(internal),
    TO SQL WITH FUNCTION pl_lua_to_hstore(internal)
);
