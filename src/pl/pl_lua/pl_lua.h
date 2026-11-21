/*-------------------------------------------------------------------------
 *
 * pl_lua.h
 *	  Declarations for transform modules that work with PL/Lua.
 *
 * This is the only PL/Lua header that is installed.  A transform module
 * (such as contrib/hstore_pl_lua) includes it, after postgres.h, to get the
 * Lua headers and the functions below.
 *
 * TRANSFORM FUNCTIONS
 *
 * Transform functions are called by PL/Lua with fcinfo->context pointing to
 * a pl_lua_node, which carries the lua_State of the calling interpreter.
 *
 * A "from SQL" function receives the SQL value as its (only) argument.  It
 * must leave the equivalent Lua value on top of the Lua stack, and return
 * (Datum) 0.
 *
 * A "to SQL" function receives, as its argument, the Lua stack index (an
 * absolute one, in the frame that is current when the function is called) of
 * the value to convert, as an int32 Datum, and returns the SQL value.  The
 * value is not to be removed or changed.
 *
 * Transform modules do not link their own copy of Lua; the Lua API symbols
 * are resolved from pl_lua itself, which has been loaded by the time any of
 * its transform functions can be called.
 *
 * ERROR HANDLING
 *
 * Lua errors (memory exhaustion in lua_pushstring, for example) and
 * PostgreSQL errors (ereport) must not be allowed to unwind through each
 * other.  So a transform function must do all of its Lua API work in a
 * lua_CFunction run with pl_lua_cpcall(), which reports a failure as a
 * PostgreSQL error.  Inside that function, any PostgreSQL code that can
 * raise an error, including palloc(), must be run with pl_lua_pg_call(),
 * which turns an error into a Lua error that the enclosing
 * pl_lua_cpcall() turns back into a PostgreSQL error.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * src/pl/pl_lua/pl_lua.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef PL_LUA_H
#define PL_LUA_H

#include "nodes/nodes.h"

#include <lua.h>
#include <lualib.h>
#include <lauxlib.h>

/*
 * We support Lua 5.4 from release 5.4.6, and Lua 5.5.  The earlier 5.4
 * releases need runtime stack limit calculations, and 5.4.5 changed the ABI,
 * so requiring 5.4.6 excludes all of them.  configure and meson check this
 * too, but check again in case the headers found here are not the ones they
 * saw.
 */
#if LUA_VERSION_NUM < 504 || LUA_VERSION_NUM > 505
#error unsupported Lua version: Lua 5.4 or 5.5 is required
#endif
#if LUA_VERSION_NUM == 504 && LUA_VERSION_RELEASE_NUM < 50406
#error unsupported Lua version: Lua 5.4.6 or later is required
#endif

#define PL_LUA_NODE_MAGIC	0x504c4c55

/*
 * Passed as fcinfo->context to transform functions.  This is not a real
 * Node: type is T_Invalid.
 */
typedef struct pl_lua_node
{
	NodeTag		type;
	uint32		magic;
	lua_State  *L;
} pl_lua_node;

/*
 * Call func(L) in protected mode, with 'arg' as its first argument (a light
 * userdata) followed by the 'nvalues' values on the top of L's stack, which
 * are moved to it.  If it fails, raise the failure as a PostgreSQL error.
 * Returns normally only on success.  func should return 0.  Everything that
 * func leaves on its stack is discarded.
 */
extern PGDLLEXPORT void pl_lua_cpcall(lua_State * L, lua_CFunction func,
									  void *arg, int nvalues);

/*
 * From inside a lua_CFunction: call fn(arg), converting any PostgreSQL error
 * it raises into a Lua error in L.
 */
extern PGDLLEXPORT void pl_lua_pg_call(lua_State * L, void (*fn) (void *arg), void *arg);

#endif							/* PL_LUA_H */
