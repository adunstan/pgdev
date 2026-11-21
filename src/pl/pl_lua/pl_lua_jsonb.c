/*-------------------------------------------------------------------------
 *
 * pl_lua_jsonb.c
 *	  jsonb datum objects and the pl_lua.jsonb module.
 *
 * Conversion from jsonb to Lua walks the value with a JsonbIterator, one
 * token at a time; each call into the jsonb machinery is wrapped in PLL_TRY
 * on its own, so that no Lua call is ever made inside a PG_TRY body.
 * Conversion from Lua first gathers the entries of each table into a Lua
 * table (running any __pairs metamethod), and then feeds the jsonb parse
 * state with one small PLL_TRY per token.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua_jsonb.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <math.h>

#include "catalog/pg_proc.h"
#include "catalog/pg_type.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "nodes/makefuncs.h"
#include "nodes/nodeFuncs.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/datum.h"
#include "utils/fmgroids.h"
#include "utils/fmgrprotos.h"
#include "utils/jsonb.h"
#include "utils/numeric.h"

#include "pl_lua_int.h"

#define PLL_MT_JSONB_OBJECT		"pl_lua.jsonb.object"
#define PLL_MT_JSONB_ARRAY		"pl_lua.jsonb.array"

#define PLL_JSONB_DEFAULT_THRESH	1000
#define PLL_JSONB_DEFAULT_FRAC		1000

/* Settings for conversion from jsonb to Lua (the mapping call) */
typedef struct ToLuaState
{
	bool		has_map;
	bool		discard;
	bool		pg_numeric;
	bool		norecurse;
	bool		has_null;
	int			map_idx;		/* absolute stack indexes */
	int			null_idx;
	int			path_idx;		/* table holding the keys of enclosing
								 * containers */
	int			pathlen;
	int			depth;
} ToLuaState;

/* Settings and state for conversion from Lua to jsonb */
typedef struct ToJsonbState
{
	JsonbInState state;
	bool		has_map;
	int			map_idx;
	bool		has_null;
	int			null_idx;
	bool		empty_object;
	lua_Integer thresh;
	lua_Integer frac;
	bool		has_direct;		/* the whole value was a datum object */
	Datum		direct;
} ToJsonbState;

typedef enum CollectionKind
{
	COLLECTION_OBJECT,
	COLLECTION_ARRAY
} CollectionKind;

static JsonbIteratorToken jsonb_next(lua_State * L, JsonbIterator **it,
									 JsonbValue *v, bool skip_nested);
static JsonbIterator *jsonb_iter_init(lua_State * L, JsonbContainer *c);
static Jsonb *jsonb_arg(lua_State * L, int idx, pll_datum **pd);
static void push_jsonb_datum(lua_State * L, Jsonb *jb);
static void push_jsonb_numeric(lua_State * L, ToLuaState *st, Numeric num);
static void push_jsonb_scalar(lua_State * L, ToLuaState *st, JsonbValue *v);
static void map_and_store(lua_State * L, ToLuaState *st, int tbl, bool shift);
static void jsonb_container_to_lua(lua_State * L, ToLuaState *st,
								   JsonbIterator **it, bool isarray);
static void check_stack_depth_lua(lua_State * L);
static void read_tolua_options(lua_State * L, ToLuaState *st, int optidx);
static int	jsonb_call(lua_State * L);
static int	jsonb_snapshot(lua_State * L, Jsonb *jb, int first_key);
static int	jsonb_iter_step(lua_State * L);
static int	jsonb_pairs_common(lua_State * L, int first_key);
static int	jsonb_pairs(lua_State * L);
static int	jsonb_ipairs(lua_State * L);
static int	jsonb_type(lua_State * L);
static int	jsonb_is_object(lua_State * L);
static int	jsonb_is_array(lua_State * L);
static int	jsonb_marker(lua_State * L, int which);
static int	jsonb_set_as_object(lua_State * L);
static int	jsonb_set_as_array(lua_State * L);
static int	jsonb_set_as_unknown(lua_State * L);
static int	table_marker(lua_State * L, int idx);
static int	set_marker(lua_State * L, const char *mtname);
static void emit(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok,
				 bool top, JsonbValue *jv);
static void emit_string(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok,
						bool top, const char *s, size_t len);
static void emit_null(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok,
					  bool top);
static void emit_number(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok,
						bool top, int idx);
static void emit_datum(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok,
					   bool top, pll_datum *pd);
static void emit_container_begin(lua_State * L, ToJsonbState *st,
								 JsonbIteratorToken seq);
static void emit_container_end(lua_State * L, ToJsonbState *st,
							   JsonbIteratorToken seq);
static void lua_to_jsonb(lua_State * L, ToJsonbState *st, int idx,
						 JsonbIteratorToken tok, bool top);
static void lua_collection_to_jsonb(lua_State * L, ToJsonbState *st, int vidx,
									JsonbIteratorToken tok, bool top);
static int	collect_entries(lua_State * L, int vidx);
static bool positive_integer_key(lua_State * L, int idx, lua_Integer * k);
static CollectionKind classify_collection(lua_State * L, ToJsonbState *st,
										  int vidx, int ent, int n,
										  lua_Integer * maxkey);
static void emit_key(lua_State * L, ToJsonbState *st, int kidx);
static bool has_tostring(lua_State * L, int idx);
static void lua_type_error(lua_State * L, int idx);
static void read_tojsonb_options(lua_State * L, ToJsonbState *st, int optsidx);
static const char *jsonb_root_type(lua_State * L, Jsonb *jb);

/* ------------------------------------------------------------------------
 * Small wrappers around PostgreSQL code that can raise errors
 * ------------------------------------------------------------------------
 */

static JsonbIteratorToken
jsonb_next(lua_State * L, JsonbIterator **it, JsonbValue *v, bool skip_nested)
{
	JsonbIteratorToken volatile tok = WJB_DONE;

	PLL_TRY();
	{
		tok = JsonbIteratorNext(it, v, skip_nested);
	}
	PLL_CATCH(L);
	return tok;
}

static JsonbIterator *
jsonb_iter_init(lua_State * L, JsonbContainer *c)
{
	JsonbIterator *volatile it = NULL;

	PLL_TRY();
	{
		it = JsonbIteratorInit(c);
	}
	PLL_CATCH(L);
	return it;
}

/*
 * Get the Jsonb behind the jsonb datum object at idx.  Raises a Lua error if
 * it is not one.
 */
static Jsonb *
jsonb_arg(lua_State * L, int idx, pll_datum **pdp)
{
	pll_datum  *pd = pll_datum_test(L, idx);
	Jsonb	   *volatile jb = NULL;

	if (pd == NULL || pd->ti->basetypoid != JSONBOID)
		luaL_error(L, "bad argument #%d (jsonb value expected)", idx);
	PLL_TRY();
	{
		jb = DatumGetJsonbP(pd->value);
	}
	PLL_CATCH(L);
	if (pdp)
		*pdp = pd;
	return jb;
}

/* Push a jsonb datum object holding a copy of jb */
static void
push_jsonb_datum(lua_State * L, Jsonb *jb)
{
	pll_typeinfo *ti = pll_typeinfo_push(L, JSONBOID, -1);

	pll_datum_push(L, ti, JsonbPGetDatum(jb));
	lua_remove(L, -2);
}

/* ------------------------------------------------------------------------
 * jsonb to Lua
 * ------------------------------------------------------------------------
 */

static void
check_stack_depth_lua(lua_State * L)
{
	PLL_TRY();
	{
		check_stack_depth();
	}
	PLL_CATCH(L);
	luaL_checkstack(L, 24, "jsonb value nested too deeply");
}

/*
 * Push a jsonb numeric as a Lua number: an integer if the text form has no
 * fractional part and is short enough, otherwise a float.  Or, if the
 * pg_numeric option is set, as a numeric datum object.
 */
static void
push_jsonb_numeric(lua_State * L, ToLuaState *st, Numeric num)
{
	char	   *volatile str = NULL;

	if (st->pg_numeric)
	{
		pll_typeinfo *ti = pll_typeinfo_push(L, NUMERICOID, -1);

		pll_datum_push(L, ti, NumericGetDatum(num));
		lua_remove(L, -2);
		return;
	}

	PLL_TRY();
	{
		str = DatumGetCString(DirectFunctionCall1(numeric_out,
												  NumericGetDatum(num)));
	}
	PLL_CATCH(L);

	if (strspn(str + (str[0] == '-'), "0123456789") ==
		strlen(str + (str[0] == '-')) && strlen(str) <= 18)
		lua_pushinteger(L, (lua_Integer) strtoll(str, NULL, 10));
	else
		lua_pushnumber(L, (lua_Number) strtod(str, NULL));
}

static void
push_jsonb_scalar(lua_State * L, ToLuaState *st, JsonbValue *v)
{
	switch (v->type)
	{
		case jbvNull:
			if (st->has_null)
				lua_pushvalue(L, st->null_idx);
			else
				lua_pushnil(L);
			break;
		case jbvString:
			lua_pushlstring(L, v->val.string.val, v->val.string.len);
			break;
		case jbvBool:
			lua_pushboolean(L, v->val.boolean);
			break;
		case jbvNumeric:
			push_jsonb_numeric(L, st, v->val.numeric);
			break;
		case jbvBinary:
			{
				Jsonb	   *volatile jb = NULL;

				PLL_TRY();
				{
					jb = JsonbValueToJsonb(v);
				}
				PLL_CATCH(L);
				push_jsonb_datum(L, jb);
			}
			break;
		default:
			luaL_error(L, "unexpected jsonb value type %d", (int) v->type);
	}
}

/*
 * The stack has the key and the value on top.  Call the map function if
 * there is one, replacing both with its results, then, if tbl is nonzero,
 * store the result in that table (unless the key or value is nil) and pop
 * both.  If tbl is zero, leave key and value on the stack.
 */
static void
map_and_store(lua_State * L, ToLuaState *st, int tbl, bool shift)
{
	if (st->has_map)
	{
		int			i;

		luaL_checkstack(L, st->pathlen + 8, "jsonb value nested too deeply");
		lua_pushvalue(L, st->map_idx);
		lua_pushvalue(L, -3);
		lua_pushvalue(L, -3);
		for (i = 1; i <= st->pathlen; i++)
			lua_rawgeti(L, st->path_idx, i);
		lua_call(L, 2 + st->pathlen, 2);
		/* stack: oldkey oldvalue newkey newvalue */
		lua_rotate(L, -4, 2);
		lua_pop(L, 2);
	}

	if (tbl != 0)
	{
		if (!lua_isnil(L, -1) && !lua_isnil(L, -2))
		{
			/* array elements are indexed from 0 in the map, from 1 in Lua */
			if (shift && lua_isinteger(L, -2))
			{
				lua_pushinteger(L, lua_tointeger(L, -2) + 1);
				lua_copy(L, -1, -3);
				lua_pop(L, 1);
			}
			lua_rawset(L, tbl);
		}
		else
			lua_pop(L, 2);
	}
}

/*
 * Convert the container whose begin token has just been read.  The key of
 * the container is on top of the stack; this pushes the result (a table, or
 * nil if the values are being discarded).
 */
static void
jsonb_container_to_lua(lua_State * L, ToLuaState *st, JsonbIterator **it,
					   bool isarray)
{
	JsonbValue	v;
	int			tbl = 0;
	lua_Integer i = 0;
	bool		pushed_path = false;

	check_stack_depth_lua(L);

	if (st->depth++ > 0)
	{
		lua_pushvalue(L, -1);
		lua_rawseti(L, st->path_idx, ++st->pathlen);
		pushed_path = true;
	}

	if (!st->discard)
	{
		lua_newtable(L);
		luaL_setmetatable(L, isarray ? PLL_MT_JSONB_ARRAY : PLL_MT_JSONB_OBJECT);
		tbl = lua_gettop(L);
	}

	for (;;)
	{
		JsonbIteratorToken tok = jsonb_next(L, it, &v, st->norecurse);

		if (tok == WJB_END_ARRAY || tok == WJB_END_OBJECT || tok == WJB_DONE)
			break;

		if (isarray)
			lua_pushinteger(L, i++);
		else
		{
			if (tok != WJB_KEY)
				luaL_error(L, "unexpected jsonb token in object");
			lua_pushlstring(L, v.val.string.val, v.val.string.len);
			tok = jsonb_next(L, it, &v, st->norecurse);
		}

		if (tok == WJB_BEGIN_ARRAY || tok == WJB_BEGIN_OBJECT)
		{
			jsonb_container_to_lua(L, st, it, tok == WJB_BEGIN_ARRAY);
			if (st->discard)
			{
				/* nothing is built, so there is nothing to map */
				lua_pop(L, 2);
				continue;
			}
		}
		else
			push_jsonb_scalar(L, st, &v);

		map_and_store(L, st, tbl, isarray);
		if (tbl == 0)
			lua_pop(L, 2);
	}

	if (pushed_path)
	{
		lua_pushnil(L);
		lua_rawseti(L, st->path_idx, st->pathlen--);
	}
	st->depth--;

	if (st->discard)
		lua_pushnil(L);
}

/*
 * Read the options of a mapping call from the table (or other value) at
 * optidx, if any.  Leaves the map function, null value and the path table on
 * the stack.
 */
static void
read_tolua_options(lua_State * L, ToLuaState *st, int optidx)
{
	memset(st, 0, sizeof(ToLuaState));

	if (optidx != 0 && lua_type(L, optidx) == LUA_TTABLE)
	{
		lua_getfield(L, optidx, "map");
		if (!lua_isnil(L, -1))
		{
			if (!lua_isfunction(L, -1))
				luaL_error(L, "the map option must be a function");
			st->has_map = true;
			st->map_idx = lua_gettop(L);
		}
		else
			lua_pop(L, 1);

		lua_getfield(L, optidx, "null");
		if (!lua_isnil(L, -1))
		{
			st->has_null = true;
			st->null_idx = lua_gettop(L);
		}
		else
			lua_pop(L, 1);

		lua_getfield(L, optidx, "discard");
		st->discard = lua_toboolean(L, -1);
		lua_pop(L, 1);
		lua_getfield(L, optidx, "pg_numeric");
		st->pg_numeric = lua_toboolean(L, -1);
		lua_pop(L, 1);
		lua_getfield(L, optidx, "norecurse");
		st->norecurse = lua_toboolean(L, -1);
		lua_pop(L, 1);
	}
	else if (optidx != 0 && lua_isfunction(L, optidx))
	{
		lua_pushvalue(L, optidx);
		st->has_map = true;
		st->map_idx = lua_gettop(L);
	}
	else if (optidx != 0 && !lua_isnil(L, optidx))
	{
		lua_pushvalue(L, optidx);
		st->has_null = true;
		st->null_idx = lua_gettop(L);
	}

	lua_newtable(L);
	st->path_idx = lua_gettop(L);
}

/*
 * The __call metamethod of jsonb datum objects: convert to a Lua table.
 */
static int
jsonb_call(lua_State * L)
{
	ToLuaState	st;
	Jsonb	   *jb = jsonb_arg(L, 1, NULL);
	JsonbIterator *it;
	JsonbValue	v;
	JsonbIteratorToken tok;

	luaL_checkstack(L, 16, "out of stack space");
	read_tolua_options(L, &st, lua_gettop(L) >= 2 ? 2 : 0);

	it = jsonb_iter_init(L, &jb->root);
	tok = jsonb_next(L, &it, &v, false);

	lua_pushnil(L);				/* key of the root */
	if (tok == WJB_BEGIN_ARRAY && v.val.array.rawScalar)
	{
		(void) jsonb_next(L, &it, &v, false);
		push_jsonb_scalar(L, &st, &v);
		/* the scalar is a lone element; its key is nil */
		map_and_store(L, &st, 0, false);
	}
	else
	{
		jsonb_container_to_lua(L, &st, &it, tok == WJB_BEGIN_ARRAY);
		if (!st.discard)
			map_and_store(L, &st, 0, false);
	}

	if (st.discard)
		return 0;
	return 1;
}

/* ------------------------------------------------------------------------
 * Iteration
 * ------------------------------------------------------------------------
 */

/*
 * Push two tables, of keys and of values, for the top-level members of jb,
 * and return the count.  The keys are 'first_key' based indexes for arrays.
 * Null elements leave a nil in the value table (the table is only ever
 * indexed densely, by position).
 */
static int
jsonb_snapshot(lua_State * L, Jsonb *jb, int first_key)
{
	ToLuaState	st;
	JsonbIterator *it;
	JsonbValue	v;
	JsonbIteratorToken tok;
	int			n = 0;
	int			ktab;
	int			vtab;
	bool		isarray;

	memset(&st, 0, sizeof(st));
	st.pg_numeric = true;
	luaL_checkstack(L, 12, "out of stack space");
	lua_newtable(L);
	ktab = lua_gettop(L);
	lua_newtable(L);
	vtab = lua_gettop(L);

	it = jsonb_iter_init(L, &jb->root);
	tok = jsonb_next(L, &it, &v, true);
	if (tok == WJB_BEGIN_ARRAY && v.val.array.rawScalar)
	{
		/* a scalar: iterates as nothing */
		return 0;
	}
	isarray = (tok == WJB_BEGIN_ARRAY);

	for (;;)
	{
		tok = jsonb_next(L, &it, &v, true);
		if (tok == WJB_END_ARRAY || tok == WJB_END_OBJECT || tok == WJB_DONE)
			break;

		n++;
		if (isarray)
			lua_pushinteger(L, first_key + n - 1);
		else
			lua_pushlstring(L, v.val.string.val, v.val.string.len);
		lua_rawseti(L, ktab, n);

		if (!isarray)
			tok = jsonb_next(L, &it, &v, true);
		push_jsonb_scalar(L, &st, &v);
		lua_rawseti(L, vtab, n);
	}
	return n;
}

/*
 * The iterator function: upvalues are the keys table, the values table, the
 * count and the current position.
 */
static int
jsonb_iter_step(lua_State * L)
{
	lua_Integer pos = lua_tointeger(L, lua_upvalueindex(4)) + 1;

	if (pos > lua_tointeger(L, lua_upvalueindex(3)))
	{
		lua_pushnil(L);
		return 1;
	}
	lua_pushinteger(L, pos);
	lua_copy(L, -1, lua_upvalueindex(4));
	lua_pop(L, 1);
	lua_rawgeti(L, lua_upvalueindex(1), pos);
	lua_rawgeti(L, lua_upvalueindex(2), pos);
	return 2;
}

static int
jsonb_pairs_common(lua_State * L, int first_key)
{
	Jsonb	   *jb = jsonb_arg(L, 1, NULL);
	int			n = jsonb_snapshot(L, jb, first_key);

	lua_pushinteger(L, n);
	lua_pushinteger(L, 0);
	lua_pushcclosure(L, jsonb_iter_step, 4);
	lua_pushnil(L);
	lua_pushnil(L);
	return 3;
}

static int
jsonb_pairs(lua_State * L)
{
	return jsonb_pairs_common(L, 0);
}

static int
jsonb_ipairs(lua_State * L)
{
	return jsonb_pairs_common(L, 0);
}

/* ------------------------------------------------------------------------
 * The pl_lua.jsonb module
 * ------------------------------------------------------------------------
 */

static const char *
jsonb_root_type(lua_State * L, Jsonb *jb)
{
	const char *volatile name = "null";

	PLL_TRY();
	{
		if (JB_ROOT_IS_SCALAR(jb))
		{
			JsonbValue	v;

			(void) JsonbExtractScalar(&jb->root, &v);
			switch (v.type)
			{
				case jbvNull:
					name = "null";
					break;
				case jbvString:
					name = "string";
					break;
				case jbvNumeric:
					name = "number";
					break;
				case jbvBool:
					name = "boolean";
					break;
				default:
					name = "null";
					break;
			}
		}
		else if (JB_ROOT_IS_OBJECT(jb))
			name = "object";
		else
			name = "array";
	}
	PLL_CATCH(L);
	return name;
}

/*
 * Is the table at idx marked?  Returns 1 for an object, 0 for an array, -1
 * for no marking, -2 if it has some other metatable.
 */
static int
table_marker(lua_State * L, int idx)
{
	int			result;

	if (!lua_getmetatable(L, idx))
		return -1;
	luaL_getmetatable(L, PLL_MT_JSONB_OBJECT);
	if (lua_rawequal(L, -1, -2))
		result = 1;
	else
	{
		lua_pop(L, 1);
		luaL_getmetatable(L, PLL_MT_JSONB_ARRAY);
		result = lua_rawequal(L, -1, -2) ? 0 : -2;
	}
	lua_pop(L, 2);
	return result;
}

static int
jsonb_type(lua_State * L)
{
	pll_datum  *pd = pll_datum_test(L, 1);
	bool		lax = lua_toboolean(L, 2);

	if (pd != NULL && pd->ti->basetypoid == JSONBOID)
	{
		Jsonb	   *jb = jsonb_arg(L, 1, NULL);

		lua_pushstring(L, jsonb_root_type(L, jb));
		return 1;
	}

	if (!lax)
		return 0;

	if (pd != NULL)
	{
		if (pd->ti->basetypoid == NUMERICOID)
		{
			lua_pushstring(L, "number");
			return 1;
		}
		return 0;
	}

	switch (lua_type(L, 1))
	{
		case LUA_TNIL:
			lua_pushstring(L, "null");
			return 1;
		case LUA_TSTRING:
			lua_pushstring(L, "string");
			return 1;
		case LUA_TNUMBER:
			lua_pushstring(L, "number");
			return 1;
		case LUA_TBOOLEAN:
			lua_pushstring(L, "boolean");
			return 1;
		case LUA_TTABLE:
			switch (table_marker(L, 1))
			{
				case 1:
					lua_pushstring(L, "object");
					return 1;
				case 0:
					lua_pushstring(L, "array");
					return 1;
				default:
					return 0;
			}
		default:
			return 0;
	}
}

/* which: 1 for object, 0 for array */
static int
jsonb_marker(lua_State * L, int which)
{
	luaL_checktype(L, 1, LUA_TTABLE);
	switch (table_marker(L, 1))
	{
		case 1:
		case 0:
			lua_pushboolean(L, table_marker(L, 1) == which);
			return 1;
		default:
			return 0;
	}
}

static int
jsonb_is_object(lua_State * L)
{
	return jsonb_marker(L, 1);
}

static int
jsonb_is_array(lua_State * L)
{
	return jsonb_marker(L, 0);
}

/* Set the marking of the table at 1, to mtname or (if NULL) to nothing */
static int
set_marker(lua_State * L, const char *mtname)
{
	luaL_checktype(L, 1, LUA_TTABLE);
	if (table_marker(L, 1) == -2)
		return luaL_error(L, "table already has a metatable");

	/*
	 * luaL_setmetatable sets the metatable of the value on top of the stack,
	 * which is the table only if no other arguments were given.  Anything
	 * else on top would be a value whose metatable is shared, that of all
	 * strings for instance.
	 */
	lua_settop(L, 1);
	if (mtname)
		luaL_setmetatable(L, mtname);
	else
	{
		lua_pushnil(L);
		lua_setmetatable(L, 1);
	}
	lua_settop(L, 1);
	return 1;
}

static int
jsonb_set_as_object(lua_State * L)
{
	return set_marker(L, PLL_MT_JSONB_OBJECT);
}

static int
jsonb_set_as_array(lua_State * L)
{
	return set_marker(L, PLL_MT_JSONB_ARRAY);
}

static int
jsonb_set_as_unknown(lua_State * L)
{
	return set_marker(L, NULL);
}

/* ------------------------------------------------------------------------
 * Lua to jsonb
 * ------------------------------------------------------------------------
 */

/* Make the "cannot convert" error for the value at idx */
static void
lua_type_error(lua_State * L, int idx)
{
	pll_tj_error(L, ERRCODE_DATATYPE_MISMATCH,
				 "cannot convert a Lua %s value to jsonb",
				 luaL_typename(L, idx));
}

static bool
has_tostring(lua_State * L, int idx)
{
	if (luaL_getmetafield(L, idx, "__tostring") == LUA_TNIL)
		return false;
	lua_pop(L, 1);
	return true;
}

/*
 * Push a scalar (or, at the top, the rawScalar wrapper around it), or one
 * token of a nested value, to the parse state.
 */
static void
emit(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok, bool top,
	 JsonbValue *jv)
{
	PLL_TRY();
	{
		if (top)
		{
			JsonbValue	wrapper;

			wrapper.type = jbvArray;
			wrapper.val.array.rawScalar = true;
			wrapper.val.array.nElems = 1;
			wrapper.val.array.elems = NULL;
			pushJsonbValue(&st->state, WJB_BEGIN_ARRAY, &wrapper);
			pushJsonbValue(&st->state, WJB_ELEM, jv);
			pushJsonbValue(&st->state, WJB_END_ARRAY, NULL);
		}
		else
			pushJsonbValue(&st->state, tok, jv);
	}
	PLL_CATCH(L);
}

static void
emit_container_begin(lua_State * L, ToJsonbState *st, JsonbIteratorToken seq)
{
	PLL_TRY();
	{
		check_stack_depth();
		pushJsonbValue(&st->state, seq, NULL);
	}
	PLL_CATCH(L);
}

static void
emit_container_end(lua_State * L, ToJsonbState *st, JsonbIteratorToken seq)
{
	PLL_TRY();
	{
		pushJsonbValue(&st->state, seq, NULL);
	}
	PLL_CATCH(L);
}

/* A string value or key; the string is copied */
static void
emit_string(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok, bool top,
			const char *s, size_t len)
{
	JsonbValue	jv;

	if (memchr(s, '\0', len) != NULL)
		pll_tj_error(L, ERRCODE_UNTRANSLATABLE_CHARACTER,
					 "unsupported Unicode escape sequence: \\u0000 cannot be converted to text");

	PLL_TRY();
	{
		(void) pg_verify_mbstr(GetDatabaseEncoding(), s, (int) len, false);
		jv.type = jbvString;
		jv.val.string.val = pnstrdup(s, len);
		jv.val.string.len = (int) len;
	}
	PLL_CATCH(L);
	emit(L, st, tok, top, &jv);
}

static void
emit_null(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok, bool top)
{
	JsonbValue	jv;

	jv.type = jbvNull;
	emit(L, st, tok, top, &jv);
}

/* A Lua number, at idx */
static void
emit_number(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok, bool top,
			int idx)
{
	JsonbValue	jv;
	bool		isint = lua_isinteger(L, idx);
	lua_Integer iv = isint ? lua_tointeger(L, idx) : 0;
	lua_Number	nv = isint ? 0 : lua_tonumber(L, idx);

	if (!isint && !isfinite(nv))
		pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
					 "cannot convert a non-finite number to jsonb");

	PLL_TRY();
	{
		jv.type = jbvNumeric;
		if (isint)
			jv.val.numeric = DatumGetNumeric(DirectFunctionCall1(int8_numeric,
																 Int64GetDatum(iv)));
		else
			jv.val.numeric = DatumGetNumeric(DirectFunctionCall1(float8_numeric,
																 Float8GetDatum(nv)));
	}
	PLL_CATCH(L);
	emit(L, st, tok, top, &jv);
}

/*
 * A datum object.  A numeric becomes a JSON number; anything else is
 * converted by to_jsonb(), which the current user must be allowed to execute.
 * If this is the whole value, the result is used as it is.
 */
static void
emit_datum(lua_State * L, ToJsonbState *st, JsonbIteratorToken tok, bool top,
		   pll_datum *pd)
{
	JsonbValue	jv;
	Jsonb	   *volatile jb = NULL;
	bool		isnumeric = (pd->ti->basetypoid == NUMERICOID);

	PLL_TRY();
	{
		if (isnumeric)
		{
			jv.type = jbvNumeric;
			jv.val.numeric = DatumGetNumeric(datumCopy(pd->value, false, -1));
		}
		else
		{
			FmgrInfo	flinfo;
			FuncExpr   *fexpr;
			Const	   *arg;
			AclResult	aclresult;

			aclresult = object_aclcheck(ProcedureRelationId, F_TO_JSONB,
										GetUserId(), ACL_EXECUTE);
			if (aclresult != ACLCHECK_OK)
				aclcheck_error(aclresult, OBJECT_FUNCTION, "to_jsonb");

			/* to_jsonb needs to be able to find the type of its argument */
			arg = makeConst(pd->ti->typoid, pd->ti->typmod, InvalidOid,
							pd->ti->typlen, pd->value, false,
							pd->ti->typbyval);
			fexpr = makeFuncExpr(F_TO_JSONB, JSONBOID, list_make1(arg),
								 InvalidOid, InvalidOid, COERCE_EXPLICIT_CALL);
			fmgr_info(F_TO_JSONB, &flinfo);
			fmgr_info_set_expr((Node *) fexpr, &flinfo);
			jb = DatumGetJsonbP(FunctionCall1(&flinfo, pd->value));
		}
	}
	PLL_CATCH(L);

	if (isnumeric)
	{
		emit(L, st, tok, top, &jv);
		return;
	}

	if (top)
	{
		st->has_direct = true;
		st->direct = JsonbPGetDatum(jb);
		return;
	}

	PLL_TRY();
	{
		if (JB_ROOT_IS_SCALAR(jb))
			(void) JsonbExtractScalar(&jb->root, &jv);
		else
		{
			jv.type = jbvBinary;
			jv.val.binary.data = &jb->root;
			jv.val.binary.len = VARSIZE(jb) - VARHDRSZ;
		}
	}
	PLL_CATCH(L);
	emit(L, st, tok, false, &jv);
}

/*
 * Gather the entries of the collection at vidx into a table on top of the
 * stack, as k1, v1, k2, v2, ...; returns the number of entries.  Uses the
 * __pairs metamethod if there is one.
 */
static int
collect_entries(lua_State * L, int vidx)
{
	int			ent;
	int			n = 0;

	vidx = lua_absindex(L, vidx);
	luaL_checkstack(L, 12, "out of stack space");
	lua_newtable(L);
	ent = lua_gettop(L);

	if (luaL_getmetafield(L, vidx, "__pairs") != LUA_TNIL)
	{
		/* stack: metamethod */
		lua_pushvalue(L, vidx);
		lua_call(L, 1, 3);
		/* iterator, state, control */
		for (;;)
		{
			lua_pushvalue(L, -3);
			lua_pushvalue(L, -3);
			lua_pushvalue(L, -3);
			lua_call(L, 2, 2);
			if (lua_isnil(L, -2))
			{
				lua_pop(L, 2);
				break;
			}
			lua_pushvalue(L, -2);
			lua_copy(L, -1, -4);	/* new control value */
			lua_pop(L, 1);
			lua_rawseti(L, ent, 2 * n + 2);
			lua_rawseti(L, ent, 2 * n + 1);
			n++;
		}
		lua_pop(L, 3);
		return n;
	}

	lua_pushnil(L);
	while (lua_next(L, vidx))
	{
		lua_rawseti(L, ent, 2 * n + 2);
		lua_pushvalue(L, -1);
		lua_rawseti(L, ent, 2 * n + 1);
		n++;
	}
	return n;
}

static bool
positive_integer_key(lua_State * L, int idx, lua_Integer * k)
{
	int			isnum;
	lua_Integer v;

	if (lua_type(L, idx) != LUA_TNUMBER)
		return false;
	v = lua_tointegerx(L, idx, &isnum);
	if (!isnum || v < 1)
		return false;
	*k = v;
	return true;
}

/*
 * Decide whether the collection is to become an array or an object.  For an
 * array, *maxkey is the number of elements.  For an array, also leaves a
 * table indexed by position on the stack.
 */
static CollectionKind
classify_collection(lua_State * L, ToJsonbState *st, int vidx, int ent, int n,
					lua_Integer * maxkey)
{
	int			marker = -1;
	bool		allints = true;
	lua_Integer minkey = LUA_MAXINTEGER;
	lua_Integer maxk = 0;
	int			nints = 0;
	int			i;
	CollectionKind kind;

	if (lua_type(L, vidx) == LUA_TTABLE)
		marker = table_marker(L, vidx);

	for (i = 0; i < n; i++)
	{
		lua_Integer k;

		lua_rawgeti(L, ent, 2 * i + 1);
		if (positive_integer_key(L, -1, &k))
		{
			nints++;
			minkey = Min(minkey, k);
			maxk = Max(maxk, k);
		}
		else
			allints = false;
		lua_pop(L, 1);
	}

	if (marker == 1)
		kind = COLLECTION_OBJECT;
	else if (marker == 0)
		kind = COLLECTION_ARRAY;
	else if (n == 0)
		kind = st->empty_object ? COLLECTION_OBJECT : COLLECTION_ARRAY;
	else if (allints &&
			 minkey - 1 <= st->thresh &&
			 (double) maxk <= (double) st->frac * (double) nints)
		kind = COLLECTION_ARRAY;
	else
		kind = COLLECTION_OBJECT;

	*maxkey = (kind == COLLECTION_ARRAY) ? maxk : 0;

	if (kind == COLLECTION_ARRAY)
	{
		lua_newtable(L);
		for (i = 0; i < n; i++)
		{
			lua_Integer k;

			lua_rawgeti(L, ent, 2 * i + 1);
			if (positive_integer_key(L, -1, &k))
			{
				lua_pop(L, 1);
				lua_rawgeti(L, ent, 2 * i + 2);
				lua_rawseti(L, -2, k);
			}
			else
				lua_pop(L, 1);
		}
	}
	return kind;
}

/* Emit the object key at stack index kidx */
static void
emit_key(lua_State * L, ToJsonbState *st, int kidx)
{
	size_t		len;
	const char *s;

	kidx = lua_absindex(L, kidx);
	switch (lua_type(L, kidx))
	{
		case LUA_TSTRING:
			s = lua_tolstring(L, kidx, &len);
			emit_string(L, st, WJB_KEY, false, s, len);
			return;
		case LUA_TNUMBER:
			s = luaL_tolstring(L, kidx, &len);
			emit_string(L, st, WJB_KEY, false, s, len);
			lua_pop(L, 1);
			return;
		default:
			if (has_tostring(L, kidx))
			{
				s = luaL_tolstring(L, kidx, &len);
				emit_string(L, st, WJB_KEY, false, s, len);
				lua_pop(L, 1);
				return;
			}
			pll_tj_error(L, ERRCODE_DATATYPE_MISMATCH,
						 "cannot use a Lua %s value as a jsonb object key",
						 luaL_typename(L, kidx));
	}
}

static void
lua_collection_to_jsonb(lua_State * L, ToJsonbState *st, int vidx,
						JsonbIteratorToken tok, bool top)
{
	int			base = lua_gettop(L);
	int			ent;
	int			n;
	int			i;
	lua_Integer maxkey;
	CollectionKind kind;

	n = collect_entries(L, vidx);
	ent = lua_gettop(L);
	kind = classify_collection(L, st, vidx, ent, n, &maxkey);

	if (kind == COLLECTION_ARRAY)
	{
		int			byidx = lua_gettop(L);
		lua_Integer j;

		emit_container_begin(L, st, WJB_BEGIN_ARRAY);
		for (j = 1; j <= maxkey; j++)
		{
			lua_rawgeti(L, byidx, j);
			if (lua_isnil(L, -1))
				emit_null(L, st, WJB_ELEM, false);
			else
				lua_to_jsonb(L, st, -1, WJB_ELEM, false);
			lua_pop(L, 1);
		}
		emit_container_end(L, st, WJB_END_ARRAY);
	}
	else
	{
		emit_container_begin(L, st, WJB_BEGIN_OBJECT);
		for (i = 0; i < n; i++)
		{
			lua_rawgeti(L, ent, 2 * i + 1);
			emit_key(L, st, -1);
			lua_rawgeti(L, ent, 2 * i + 2);
			lua_to_jsonb(L, st, -1, WJB_VALUE, false);
			lua_pop(L, 2);
		}
		emit_container_end(L, st, WJB_END_OBJECT);
	}

	lua_settop(L, base);
	(void) tok;
	(void) top;
}

/*
 * Convert the value at idx; tok says how a scalar is to be pushed (WJB_ELEM
 * or WJB_VALUE), and top whether this is the whole value.
 */
static void
lua_to_jsonb(lua_State * L, ToJsonbState *st, int idx, JsonbIteratorToken tok,
			 bool top)
{
	int			base = lua_gettop(L);
	int			vidx;
	size_t		len;
	const char *s;
	pll_datum  *pd;

	luaL_checkstack(L, 16, "jsonb value nested too deeply");
	idx = lua_absindex(L, idx);

	if (st->has_map)
	{
		lua_pushvalue(L, st->map_idx);
		lua_pushvalue(L, idx);
		lua_call(L, 1, 1);
		vidx = lua_gettop(L);
	}
	else
		vidx = idx;

	if (st->has_null && lua_rawequal(L, vidx, st->null_idx))
		emit_null(L, st, tok, top);
	else
	{
		switch (lua_type(L, vidx))
		{
			case LUA_TNIL:
				emit_null(L, st, tok, top);
				break;
			case LUA_TBOOLEAN:
				{
					JsonbValue	jv;

					jv.type = jbvBool;
					jv.val.boolean = lua_toboolean(L, vidx);
					emit(L, st, tok, top, &jv);
				}
				break;
			case LUA_TNUMBER:
				emit_number(L, st, tok, top, vidx);
				break;
			case LUA_TSTRING:
				s = lua_tolstring(L, vidx, &len);
				emit_string(L, st, tok, top, s, len);
				break;
			case LUA_TTABLE:
				lua_collection_to_jsonb(L, st, vidx, tok, top);
				break;
			case LUA_TUSERDATA:
				pd = pll_datum_test(L, vidx);
				if (pd != NULL)
					emit_datum(L, st, tok, top, pd);
				else if (luaL_getmetafield(L, vidx, "__pairs") != LUA_TNIL)
				{
					lua_pop(L, 1);
					lua_collection_to_jsonb(L, st, vidx, tok, top);
				}
				else if (has_tostring(L, vidx))
				{
					s = luaL_tolstring(L, vidx, &len);
					emit_string(L, st, tok, top, s, len);
				}
				else
					lua_type_error(L, vidx);
				break;
			default:
				lua_type_error(L, vidx);
		}
	}

	lua_settop(L, base);
}

static void
read_tojsonb_options(lua_State * L, ToJsonbState *st, int optsidx)
{
	st->thresh = PLL_JSONB_DEFAULT_THRESH;
	st->frac = PLL_JSONB_DEFAULT_FRAC;

	if (optsidx == 0 || lua_isnil(L, optsidx))
		return;
	if (lua_type(L, optsidx) != LUA_TTABLE)
		pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
					 "jsonb conversion options must be a table");

	lua_getfield(L, optsidx, "map");
	if (!lua_isnil(L, -1))
	{
		if (!lua_isfunction(L, -1))
			pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
						 "the map option must be a function");
		st->has_map = true;
		st->map_idx = lua_gettop(L);
	}
	else
		lua_pop(L, 1);

	lua_getfield(L, optsidx, "null");
	if (!lua_isnil(L, -1))
	{
		st->has_null = true;
		st->null_idx = lua_gettop(L);
	}
	else
		lua_pop(L, 1);

	lua_getfield(L, optsidx, "empty_object");
	st->empty_object = lua_toboolean(L, -1);
	lua_pop(L, 1);

	lua_getfield(L, optsidx, "array_thresh");
	if (!lua_isnil(L, -1))
	{
		int			isnum;

		st->thresh = lua_tointegerx(L, -1, &isnum);
		if (!isnum)
			pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
						 "the array_thresh option must be an integer");
	}
	lua_pop(L, 1);

	lua_getfield(L, optsidx, "array_frac");
	if (!lua_isnil(L, -1))
	{
		int			isnum;

		st->frac = lua_tointegerx(L, -1, &isnum);
		if (!isnum)
			pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
						 "the array_frac option must be an integer");
	}
	lua_pop(L, 1);
}

Datum
pll_jsonb_todatum(lua_State * L, int idx, int optsidx)
{
	ToJsonbState st;
	int			top = lua_gettop(L);
	Datum volatile result = (Datum) 0;

	memset(&st, 0, sizeof(st));
	idx = lua_absindex(L, idx);
	if (optsidx != 0)
		optsidx = lua_absindex(L, optsidx);
	luaL_checkstack(L, 16, "out of stack space");

	read_tojsonb_options(L, &st, optsidx);
	lua_to_jsonb(L, &st, idx, WJB_ELEM, true);

	if (st.has_direct)
		result = st.direct;
	else
	{
		PLL_TRY();
		{
			result = JsonbPGetDatum(JsonbValueToJsonb(st.state.result));
		}
		PLL_CATCH(L);
	}

	lua_settop(L, top);
	return result;
}

/* ------------------------------------------------------------------------
 * Module initialization
 * ------------------------------------------------------------------------
 */

static const luaL_Reg jsonb_datum_methods[] = {
	{"__call", jsonb_call},
	{"__tostring", pll_datum_tostring},
	{"__pairs", jsonb_pairs},
	{NULL, NULL}
};

static const luaL_Reg jsonb_module_funcs[] = {
	{"is_object", jsonb_is_object},
	{"is_array", jsonb_is_array},
	{"set_as_object", jsonb_set_as_object},
	{"set_as_array", jsonb_set_as_array},
	{"set_as_unknown", jsonb_set_as_unknown},
	{"pairs", jsonb_pairs},
	{"ipairs", jsonb_ipairs},
	{"type", jsonb_type},
	{NULL, NULL}
};

int
pll_open_jsonb(lua_State * L)
{
	luaL_newmetatable(L, PLL_MT_JSONB);
	luaL_setfuncs(L, jsonb_datum_methods, 0);
	pll_datum_add_compare(L);
	lua_pop(L, 1);

	luaL_newmetatable(L, PLL_MT_JSONB_OBJECT);
	lua_pop(L, 1);
	luaL_newmetatable(L, PLL_MT_JSONB_ARRAY);
	lua_pop(L, 1);

	luaL_newlib(L, jsonb_module_funcs);
	return 1;
}
