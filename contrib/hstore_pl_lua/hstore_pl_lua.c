/*-------------------------------------------------------------------------
 *
 * hstore_pl_lua.c
 *	  Transforms between hstore and PL/Lua tables.
 *
 * The same functions serve the pl_lua and pl_luau languages.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  contrib/hstore_pl_lua/hstore_pl_lua.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "fmgr.h"
#include "hstore/hstore.h"
#include "pl_lua.h"

PG_MODULE_MAGIC_EXT(
					.name = "hstore_pl_lua",
					.version = PG_VERSION
);

/* Linkage to functions in the hstore module */
typedef HStore *(*hstoreUniquePairs_t) (Pairs *a, int32 l, Size *buflen);
typedef int (*hstoreUniquePairs_int_t) (Pairs *a, int32 l, Size *buflen);
typedef HStore *(*hstorePairs_t) (Pairs *pairs, int32 pcount, Size buflen);
typedef size_t (*hstoreCheckKeyLen_t) (size_t len);
typedef size_t (*hstoreCheckValLen_t) (size_t len);

static hstoreUniquePairs_int_t hstoreUniquePairs_p;
static hstorePairs_t hstorePairs_p;
static hstoreCheckKeyLen_t hstoreCheckKeyLen_p;
static hstoreCheckValLen_t hstoreCheckValLen_p;
static HStore *(*hstoreUpgrade_p) (Datum orig);

/*
 * Linkage to the functions in pl_lua.  On some platforms a module cannot
 * refer to the functions of another by name, so look them up, as is done for
 * the functions of hstore.
 */
typedef void (*pl_lua_cpcall_t) (lua_State * L, lua_CFunction func, void *arg,
								 int nvalues);
typedef void (*pl_lua_pg_call_t) (lua_State * L, void (*fn) (void *arg),
								  void *arg);
static pl_lua_cpcall_t pl_lua_cpcall_p;
static pl_lua_pg_call_t pl_lua_pg_call_p;

/* Static asserts verify that the typedefs above match the declarations */
StaticAssertVariableIsOfType(&hstoreUniquePairs, hstoreUniquePairs_int_t);
StaticAssertVariableIsOfType(&hstorePairs, hstorePairs_t);
StaticAssertVariableIsOfType(&hstoreCheckKeyLen, hstoreCheckKeyLen_t);
StaticAssertVariableIsOfType(&hstoreCheckValLen, hstoreCheckValLen_t);

/* registry key for handing the result table out of the protected call */
static const char hs_result_key = 0;

/* State passed to the Lua C functions and the PostgreSQL-side callbacks */
typedef struct to_hstore_state
{
	Pairs	   *pairs;
	int32		npairs;
	HStore	   *result;
} to_hstore_state;

void		_PG_init(void);

PGDLLEXPORT Datum hstore_to_pl_lua(PG_FUNCTION_ARGS);
PGDLLEXPORT Datum pl_lua_to_hstore(PG_FUNCTION_ARGS);

static pl_lua_node *get_node(FunctionCallInfo fcinfo);
static int	push_hstore_cfunc(lua_State * L);
static int	to_hstore_cfunc(lua_State * L);
static void build_hstore(void *arg);

/*
 * Module initialize function: fetch function pointers for cross-module calls.
 */
void
_PG_init(void)
{
	hstoreUpgrade_p = (HStore *(*) (Datum))
		load_external_function("$libdir/hstore", "hstoreUpgrade", true, NULL);
	hstoreUniquePairs_p = (hstoreUniquePairs_int_t)
		load_external_function("$libdir/hstore", "hstoreUniquePairs", true, NULL);
	hstorePairs_p = (hstorePairs_t)
		load_external_function("$libdir/hstore", "hstorePairs", true, NULL);
	hstoreCheckKeyLen_p = (hstoreCheckKeyLen_t)
		load_external_function("$libdir/hstore", "hstoreCheckKeyLen", true, NULL);
	hstoreCheckValLen_p = (hstoreCheckValLen_t)
		load_external_function("$libdir/hstore", "hstoreCheckValLen", true, NULL);
	pl_lua_cpcall_p = (pl_lua_cpcall_t)
		load_external_function("$libdir/pl_lua", "pl_lua_cpcall", true, NULL);
	pl_lua_pg_call_p = (pl_lua_pg_call_t)
		load_external_function("$libdir/pl_lua", "pl_lua_pg_call", true, NULL);
}

/* These defines must be after the module init function */
#define hstoreUpgrade hstoreUpgrade_p
#define hstoreUniquePairs hstoreUniquePairs_p
#define hstorePairs hstorePairs_p
#define hstoreCheckKeyLen hstoreCheckKeyLen_p
#define hstoreCheckValLen hstoreCheckValLen_p
#define pl_lua_cpcall pl_lua_cpcall_p
#define pl_lua_pg_call pl_lua_pg_call_p

static pl_lua_node *
get_node(FunctionCallInfo fcinfo)
{
	pl_lua_node *node = (pl_lua_node *) fcinfo->context;

	if (node == NULL || node->magic != PL_LUA_NODE_MAGIC)
		elog(ERROR, "hstore transform functions can only be called by PL/Lua");
	return node;
}

/*
 * Lua C function: push a table made from the hstore passed as the userdata
 * argument.  Pairs whose value is null are left out, since Lua tables cannot
 * hold nil.
 */
static int
push_hstore_cfunc(lua_State * L)
{
	HStore	   *in = (HStore *) lua_touserdata(L, 1);
	int			count = HS_COUNT(in);
	char	   *base = STRPTR(in);
	HEntry	   *entries = ARRPTR(in);
	int			i;

	luaL_checkstack(L, 4, "out of stack space");
	lua_createtable(L, 0, count);
	for (i = 0; i < count; i++)
	{
		if (HSTORE_VALISNULL(entries, i))
			continue;
		lua_pushlstring(L, HSTORE_KEY(entries, base, i),
						HSTORE_KEYLEN(entries, i));
		lua_pushlstring(L, HSTORE_VAL(entries, base, i),
						HSTORE_VALLEN(entries, i));
		lua_rawset(L, -3);
	}

	/* leave the table where the caller can find it */
	lua_rawsetp(L, LUA_REGISTRYINDEX, &hs_result_key);
	return 0;
}

PG_FUNCTION_INFO_V1(hstore_to_pl_lua);

Datum
hstore_to_pl_lua(PG_FUNCTION_ARGS)
{
	HStore	   *in = PG_GETARG_HSTORE_P(0);
	lua_State  *L = get_node(fcinfo)->L;

	pl_lua_cpcall(L, push_hstore_cfunc, in, 0);

	/* the table was left in the registry, as protected calls drop results */
	lua_rawgetp(L, LUA_REGISTRYINDEX, &hs_result_key);
	lua_pushnil(L);
	lua_rawsetp(L, LUA_REGISTRYINDEX, &hs_result_key);

	PG_RETURN_VOID();
}

/* Build the hstore, in PostgreSQL code that may raise errors */
static void
build_hstore(void *arg)
{
	to_hstore_state *st = (to_hstore_state *) arg;
	Size		buflen;
	int32		i;
	int32		pcount;

	for (i = 0; i < st->npairs; i++)
	{
		st->pairs[i].keylen = hstoreCheckKeyLen(st->pairs[i].keylen);
		if (!st->pairs[i].isnull)
			st->pairs[i].vallen = hstoreCheckValLen(st->pairs[i].vallen);
	}

	pcount = hstoreUniquePairs(st->pairs, st->npairs, &buflen);
	st->result = hstorePairs(st->pairs, pcount, buflen);
}

/*
 * Lua C function: convert the table that is its second argument to an
 * hstore.  The strings making up the keys and values are kept alive by a
 * table on the stack until the hstore has been built.
 */
static int
to_hstore_cfunc(lua_State * L)
{
	to_hstore_state *st = (to_hstore_state *) lua_touserdata(L, 1);
	int			tbl = 2;
	int			anchor;
	int			n = 0;
	int			i;
	Pairs	   *pairs;
	int			slot = 0;

	luaL_checkstack(L, 12, "out of stack space");

	/* first count the entries */
	lua_pushnil(L);
	while (lua_next(L, tbl))
	{
		n++;
		lua_pop(L, 1);
	}

	lua_createtable(L, 2 * n, 0);
	anchor = lua_gettop(L);
	pairs = (Pairs *) lua_newuserdatauv(L, sizeof(Pairs) * Max(n, 1), 0);

	i = 0;
	lua_pushnil(L);
	while (lua_next(L, tbl))
	{
		size_t		len;
		int			ktype = lua_type(L, -2);
		int			vtype = lua_type(L, -1);

		/* a __tostring metamethod called below may have added entries */
		if (i >= n)
			return luaL_error(L, "table was modified during transformation to hstore");
		if (ktype != LUA_TSTRING && ktype != LUA_TNUMBER)
			return luaL_error(L, "cannot transform a table key of type %s to an hstore key",
							  lua_typename(L, ktype));
		if (vtype != LUA_TSTRING && vtype != LUA_TNUMBER &&
			vtype != LUA_TBOOLEAN && vtype != LUA_TUSERDATA)
			return luaL_error(L, "cannot transform a table value of type %s to an hstore value",
							  lua_typename(L, vtype));

		/* the key, copied so that a numeric key is not converted in place */
		lua_pushvalue(L, -2);
		pairs[i].key = (char *) luaL_tolstring(L, -1, &len);
		pairs[i].keylen = len;
		lua_rawseti(L, anchor, ++slot);
		lua_pop(L, 1);

		pairs[i].val = (char *) luaL_tolstring(L, -1, &len);
		pairs[i].vallen = len;
		pairs[i].isnull = false;
		pairs[i].needfree = false;
		lua_rawseti(L, anchor, ++slot);

		/* drop the value, keeping the key for lua_next */
		lua_pop(L, 1);
		i++;
	}

	/* fewer entries than were counted: the table was changed as well */
	if (i != n)
		return luaL_error(L, "table was modified during transformation to hstore");

	st->pairs = pairs;
	st->npairs = n;
	pl_lua_pg_call(L, build_hstore, st);
	return 0;
}

PG_FUNCTION_INFO_V1(pl_lua_to_hstore);

Datum
pl_lua_to_hstore(PG_FUNCTION_ARGS)
{
	int			idx = PG_GETARG_INT32(0);
	lua_State  *L = get_node(fcinfo)->L;
	to_hstore_state st;

	if (lua_type(L, idx) != LUA_TTABLE)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("cannot transform non-table Lua value to hstore")));

	st.pairs = NULL;
	st.npairs = 0;
	st.result = NULL;

	lua_pushvalue(L, idx);
	pl_lua_cpcall(L, to_hstore_cfunc, &st, 1);

	PG_RETURN_POINTER(st.result);
}
