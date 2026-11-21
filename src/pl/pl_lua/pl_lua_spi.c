/*-------------------------------------------------------------------------
 *
 * pl_lua_spi.c
 *	  The pl_lua.spi module: queries, prepared statements, cursors, quoting
 *	  functions and transaction control.
 *
 * Queries are always prepared with variable parameters, so that the types of
 * parameters that are not given explicitly are deduced from the query, as
 * for PREPARE.  Statement objects keep their plans (with SPI_keepplan) and
 * the deduced parameter types in a private memory context, which is released
 * by the statement's finalizer.
 *
 * Each operation is split into phases: the work that uses PostgreSQL
 * facilities is done inside PLL_TRY, and the work that uses Lua (converting
 * values, building result tables) outside it.  See pl_lua_int.h.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua_spi.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "access/xact.h"
#include "executor/spi.h"
#include "nodes/params.h"
#include "parser/parse_param.h"
#include "parser/parse_type.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"
#include "tcop/cmdtag.h"
#include "utils/memutils.h"
#include "utils/plancache.h"
#include "utils/portal.h"

#include "pl_lua_int.h"

#define PLL_MT_STMT		"pl_lua.spi.statement"
#define PLL_MT_CURSOR	"pl_lua.spi.cursor"

#define PLL_DEFAULT_FETCH_COUNT	50

/* Registry key of the table (weak values) mapping portal names to cursors */
static const char pll_spi_cursors_key = 0;

/*
 * The persistent part of a statement.  The parameter type array must live
 * in mcxt, since the parser may enlarge it when the plan is revalidated.
 */
#define PLL_MT_SPI_EXEC "pl_lua.spi.exec"

typedef struct pll_stmt_data
{
	MemoryContext mcxt;
	SPIPlanPtr	plan;
	Oid		   *argtypes;
	int			nargs;
	int			cursor_options;
	int			fetch_count;
	Oid		   *preset;			/* types of datum arguments, while preparing */
	int			npreset;
} pll_stmt_data;

/* The userdata of a statement object */
typedef struct pll_stmt
{
	pll_stmt_data *data;
} pll_stmt;

/*
 * The userdata of a cursor object.  Its uservalue is the portal name (a
 * string), or nil if it is a new cursor that has not been opened.
 */
typedef struct pll_cursor
{
	bool		owned;
} pll_cursor;

/* State of one execution of a query or statement */
typedef struct pll_exec
{
	pll_stmt_data *sd;
	pll_stmt_data adhoc;		/* used if the query is not a statement */
	bool		is_adhoc;
	SPIPlanPtr	plan;
	MemoryContext tmpcxt;
	int			nparams;
	Datum	   *values;
	bool	   *nulls;
	/* results */
	int			rc;
	SPITupleTable *tuptable;
	uint64		processed;
	char	   *portalname;
} pll_exec;

typedef struct pll_direction
{
	const char *name;
	FetchDirection dir;
} pll_direction;

static const pll_direction pll_directions[] = {
	{"forward", FETCH_FORWARD},
	{"next", FETCH_FORWARD},
	{"backward", FETCH_BACKWARD},
	{"prior", FETCH_BACKWARD},
	{"absolute", FETCH_ABSOLUTE},
	{"relative", FETCH_RELATIVE},
	{NULL, FETCH_FORWARD}
};

static void pll_parser_setup(ParseState *pstate, void *arg);
static void pll_check_query_string(lua_State * L, int idx, const char **query);
static void pll_exec_prepare_adhoc(lua_State * L, pll_exec *ex, const char *query,
								   int firstarg, int nargs);
static Oid *pll_preset_types(lua_State * L, int first, int nargs);
static Node *pll_paramref_hook(ParseState *pstate, ParamRef *pref);
static void pll_exec_begin(lua_State * L, pll_exec *ex);
static void pll_exec_params(lua_State * L, pll_exec *ex, int first, int n);
static void pll_exec_end(lua_State * L, pll_exec *ex);
static pll_exec *pll_exec_new(lua_State * L);
static void pll_exec_release(pll_exec *ex);
static int	pll_exec_close_cfunc(lua_State * L);
static void pll_spi_add_helpers(lua_State * L);
static void pll_exec_run(lua_State * L, pll_exec *ex, long tcount);
static void pll_exec_open_cursor(lua_State * L, pll_exec *ex, const char *name);
static ParamListInfo pll_exec_paramlist(pll_exec *ex);
static bool pll_plan_is_call(SPIPlanPtr plan);
static long pll_get_maxrows(lua_State * L, int idx);
static const char *pll_quote_arg(lua_State * L, int idx);
static void pll_push_quoted_literal(lua_State * L, const char *s);
static void pll_push_exec_result(lua_State * L, pll_exec *ex);
static void pll_push_tuptable(lua_State * L, SPITupleTable *tuptable,
							  uint64 processed);
static void pll_free_tuptable(lua_State * L, SPITupleTable *tuptable);
static pll_stmt_data *pll_check_stmt(lua_State * L, int idx);
static pll_cursor *pll_check_cursor(lua_State * L, int idx);
static const char *pll_cursor_getname(lua_State * L, int idx);
static void pll_cursor_register(lua_State * L, int idx, const char *name);
static void pll_cursor_make(lua_State * L, const char *name, bool owned);
static void pll_cursor_find_or_make(lua_State * L, const char *name);
static void pll_cursor_set_opened(lua_State * L, int idx, const char *portalname);
static void pll_cursor_close_portal(lua_State * L, const char *name);
static int	pll_cursor_gc(lua_State * L);
static int	pll_stmt_gc(lua_State * L);
static int	pll_spi_execute_common(lua_State * L, int qidx, int firstarg, long maxrows,
								   bool method);
static int	pll_spi_execute(lua_State * L);
static int	pll_spi_execute_count(lua_State * L);
static int	pll_spi_rows(lua_State * L);
static int	pll_spi_prepare(lua_State * L);
static int	pll_spi_findcursor(lua_State * L);
static int	pll_spi_newcursor(lua_State * L);
static int	pll_spi_readonly(lua_State * L);
static int	pll_spi_is_atomic(lua_State * L);
static int	pll_spi_commit(lua_State * L);
static int	pll_spi_rollback(lua_State * L);
static int	pll_spi_quote_ident(lua_State * L);
static int	pll_spi_quote_literal(lua_State * L);
static int	pll_spi_quote_nullable(lua_State * L);
static int	pll_stmt_execute(lua_State * L);
static int	pll_stmt_execute_count(lua_State * L);
static int	pll_stmt_rows(lua_State * L);
static int	pll_stmt_getcursor(lua_State * L);
static int	pll_stmt_numargs(lua_State * L);
static int	pll_stmt_argtype(lua_State * L);
static int	pll_cursor_open(lua_State * L);
static int	pll_cursor_fetch(lua_State * L);
static int	pll_cursor_move(lua_State * L);
static int	pll_cursor_fetch_or_move(lua_State * L, bool move);
static int	pll_cursor_rows(lua_State * L);
static bool pll_portal_is_open(lua_State * L, const char *name);
static int	pll_cursor_isopen(lua_State * L);
static int	pll_cursor_close(lua_State * L);
static int	pll_cursor_close_hook(lua_State * L);
static int	pll_cursor_name_method(lua_State * L);
static int	pll_cursor_isowned(lua_State * L);
static int	pll_cursor_own(lua_State * L);
static int	pll_cursor_disown(lua_State * L);
static int	pll_rows_iter(lua_State * L);
static void pll_cursor_fetch_push(lua_State * L, const char *name,
								  FetchDirection dir, long count, bool move);
static void pll_push_rows_iterator(lua_State * L, pll_exec *ex, int fetch_count);
static void pll_collect_argtypes(lua_State * L, int idx, Oid **types, int *ntypes);
static void pll_read_options(lua_State * L, int idx, pll_stmt_data *sd);
static bool pll_option_true(lua_State * L, int idx, const char *key);

static const luaL_Reg pll_spi_funcs[] = {
	{"execute", pll_spi_execute},
	{"execute_count", pll_spi_execute_count},
	{"rows", pll_spi_rows},
	{"prepare", pll_spi_prepare},
	{"findcursor", pll_spi_findcursor},
	{"newcursor", pll_spi_newcursor},
	{"readonly", pll_spi_readonly},
	{"is_atomic", pll_spi_is_atomic},
	{"commit", pll_spi_commit},
	{"rollback", pll_spi_rollback},
	{"quote_ident", pll_spi_quote_ident},
	{"quote_literal", pll_spi_quote_literal},
	{"quote_nullable", pll_spi_quote_nullable},
	{NULL, NULL}
};

static const luaL_Reg pll_stmt_methods[] = {
	{"execute", pll_stmt_execute},
	{"execute_count", pll_stmt_execute_count},
	{"rows", pll_stmt_rows},
	{"getcursor", pll_stmt_getcursor},
	{"numargs", pll_stmt_numargs},
	{"argtype", pll_stmt_argtype},
	{NULL, NULL}
};

static const luaL_Reg pll_cursor_methods[] = {
	{"open", pll_cursor_open},
	{"fetch", pll_cursor_fetch},
	{"move", pll_cursor_move},
	{"rows", pll_cursor_rows},
	{"isopen", pll_cursor_isopen},
	{"close", pll_cursor_close},
	{"name", pll_cursor_name_method},
	{"isowned", pll_cursor_isowned},
	{"own", pll_cursor_own},
	{"disown", pll_cursor_disown},
	{NULL, NULL}
};

/*
 * Open the module.  Besides the functions defined here, the table gets the
 * message functions of pl_lua.elog.
 */
int
pll_open_spi(lua_State * L)
{
	luaL_checkstack(L, 8, "out of stack space");

	/* the cursor registry: portal name -> cursor object, values weak */
	lua_newtable(L);
	lua_newtable(L);
	lua_pushliteral(L, "v");
	lua_setfield(L, -2, "__mode");
	lua_setmetatable(L, -2);
	lua_rawsetp(L, LUA_REGISTRYINDEX, &pll_spi_cursors_key);

	luaL_newmetatable(L, PLL_MT_STMT);
	lua_newtable(L);
	luaL_setfuncs(L, pll_stmt_methods, 0);
	lua_setfield(L, -2, "__index");
	lua_pushcfunction(L, pll_stmt_execute);
	lua_setfield(L, -2, "__call");
	lua_pushcfunction(L, pll_stmt_gc);
	lua_setfield(L, -2, "__gc");
	lua_pop(L, 1);

	luaL_newmetatable(L, PLL_MT_CURSOR);
	lua_newtable(L);
	luaL_setfuncs(L, pll_cursor_methods, 0);
	lua_setfield(L, -2, "__index");
	lua_pushcfunction(L, pll_cursor_gc);
	lua_setfield(L, -2, "__gc");
	lua_pushcfunction(L, pll_cursor_close_hook);
	lua_setfield(L, -2, "__close");
	lua_pop(L, 1);

	luaL_newmetatable(L, PLL_MT_SPI_EXEC);
	lua_pushcfunction(L, pll_exec_close_cfunc);
	lua_setfield(L, -2, "__close");
	lua_pop(L, 1);

	luaL_newlib(L, pll_spi_funcs);

	/* copy in the message functions */
	luaL_requiref(L, PLL_MOD_ELOG, pll_open_elog, 0);
	lua_pushnil(L);
	while (lua_next(L, -2))
	{
		lua_pushvalue(L, -2);
		lua_insert(L, -2);
		lua_settable(L, -5);
	}
	lua_pop(L, 1);

	pll_spi_add_helpers(L);

	return 1;
}

/*
 * Convenience functions that are easiest to write in Lua.  The chunk is
 * called with the spi module and the pgtype module as arguments.  It uses no
 * global variables, so nothing that trusted code might change can affect it.
 */
static const char pll_spi_helpers_src[] =
"local spi, pgtype = ...\n"
"local yield, isyieldable, unpack, tostring, type, error =\n"
"  coroutine.yield, coroutine.isyieldable, table.unpack, tostring, type,\n"
"  error\n"
"local concat, gsub, find, lower, match = table.concat, string.gsub,\n"
"  string.find, string.lower, string.match\n"
"local rawget = rawget\n"
"local _ENV = nil\n"
"\n"
"-- The length of a sequence that can contain nils: n if given, as\n"
"-- table.pack does, otherwise the length operator\n"
"local function seqlen(t)\n"
"  local n = rawget(t, 'n')\n"
"  if type(n) ~= 'number' or n < 0 or n % 1 ~= 0 then n = #t end\n"
"  if type(n) ~= 'number' or n > 16777216 then\n"
"    error('the length of the table is not usable', 0)\n"
"  end\n"
"  return n\n"
"end\n"
"\n"
"-- Yield each row of a query as a row of the result of the function\n"
"function spi.return_query(query, ...)\n"
"  local rt = pgtype(nil, 0)\n"
"  if rt == nil or not isyieldable() then\n"
"    error('return_query can only be used in a function that returns a set', 2)\n"
"  end\n"
"  local composite = rt:kind() == 'composite'\n"
"  for row in spi.rows(query, ...) do\n"
"    if composite then\n"
"      local vals, n = {}, #row\n"
"      for i = 1, n do vals[i] = row[i] end\n"
"      yield(unpack(vals, 1, n))\n"
"    else\n"
"      yield(row[1])\n"
"    end\n"
"  end\n"
"end\n"
"\n"
"-- The text form of a value as a value of the named type\n"
"function spi.encode_typed_literal(value, typename)\n"
"  local t = pgtype[typename]\n"
"  if t == nil then\n"
"    error('type \"' .. tostring(typename) .. '\" does not exist', 2)\n"
"  end\n"
"  if value == nil then return nil end\n"
"  return tostring(t(value))\n"
"end\n"
"\n"
"-- The text form of an array: a table of values or tables\n"
"local function array_literal(t, delim, depth)\n"
"  if depth > 6 then\n"
"    error('arrays have at most 6 dimensions', 0)\n"
"  end\n"
"  local parts = {}\n"
"  for i = 1, seqlen(t) do\n"
"    local v = t[i]\n"
"    if v == nil then\n"
"      parts[i] = 'NULL'\n"
"    elseif type(v) == 'table' then\n"
"      parts[i] = array_literal(v, delim, depth + 1)\n"
"    else\n"
"      local s = tostring(v)\n"
"      if s == '' or find(s, '[%s{}\",\\\\]') or lower(s) == 'null' or\n"
"         find(s, delim, 1, true) then\n"
"        s = '\"' .. gsub(s, '[\\\\\"]', '\\\\%0') .. '\"'\n"
"      end\n"
"      parts[i] = s\n"
"    end\n"
"  end\n"
"  return '{' .. concat(parts, delim) .. '}'\n"
"end\n"
"\n"
"function spi.encode_array_literal(t, delim)\n"
"  if type(t) ~= 'table' then\n"
"    error('encode_array_literal needs a table', 2)\n"
"  end\n"
"  return array_literal(t, delim or ',', 1)\n"
"end\n"
"\n"
"-- An ARRAY[...] expression for an array, with quoted elements\n"
"local function array_constructor(t, depth)\n"
"  if depth > 6 then\n"
"    error('arrays have at most 6 dimensions', 0)\n"
"  end\n"
"  local parts = {}\n"
"  for i = 1, seqlen(t) do\n"
"    local v = t[i]\n"
"    if type(v) == 'table' then\n"
"      parts[i] = array_constructor(v, depth + 1)\n"
"    elseif v == nil then\n"
"      parts[i] = 'NULL'\n"
"    else\n"
"      parts[i] = spi.quote_literal(tostring(v))\n"
"    end\n"
"  end\n"
"  return 'ARRAY[' .. concat(parts, ',') .. ']'\n"
"end\n"
"\n"
"function spi.encode_array_constructor(t)\n"
"  if type(t) ~= 'table' then\n"
"    error('encode_array_constructor needs a table', 2)\n"
"  end\n"
"  return array_constructor(t, 1)\n"
"end\n"
"\n"
"-- Is the value a number, or a string that is written like one?\n"
"function spi.looks_like_number(v)\n"
"  local tv = type(v)\n"
"  if tv == 'number' then return true end\n"
"  if tv == 'userdata' then\n"
"    local ti = pgtype(v)\n"
"    return ti ~= nil and ti:name() == 'numeric'\n"
"  end\n"
"  if tv ~= 'string' then return false end\n"
"  local s = match(v, '^%s*(.-)%s*$')\n"
"  if match(s, '^[+-]?%d+%.?%d*$') or match(s, '^[+-]?%.%d+$') or\n"
"     match(s, '^[+-]?%d+%.?%d*[eE][+-]?%d+$') or\n"
"     match(s, '^[+-]?%.%d+[eE][+-]?%d+$') then\n"
"    return true\n"
"  end\n"
"  local l = lower(gsub(s, '^[+-]', ''))\n"
"  return l == 'inf' or l == 'infinity' or l == 'nan'\n"
"end\n";

/* Add the functions written in Lua to the spi module on top of the stack */
static void
pll_spi_add_helpers(lua_State * L)
{
	if (luaL_loadbufferx(L, pll_spi_helpers_src, sizeof(pll_spi_helpers_src) - 1,
						 "=pl_lua.spi", "t") != LUA_OK)
		lua_error(L);
	lua_pushvalue(L, -2);
	luaL_requiref(L, PLL_MOD_PGTYPE, pll_open_pgtype, 0);
	lua_call(L, 2, 0);
}

/* ------------------------------------------------------------------------
 * Preparing and executing queries
 * ------------------------------------------------------------------------
 */

/*
 * While a query is being parsed: the data of the statement being prepared and
 * the standard hook for references to parameters that we wrap.
 */
static pll_stmt_data *pll_hook_sd = NULL;
static ParseParamRefHook pll_orig_paramref_hook = NULL;

/*
 * Hook for references to parameters.  The standard hook, which deduces types
 * from the context, gives a parameter the type unknown when it is first
 * referenced.  A parameter for which a datum object was passed is given the
 * type of the object instead, as it would be for a typed value.
 */
static Node *
pll_paramref_hook(ParseState *pstate, ParamRef *pref)
{
	Node	   *result = pll_orig_paramref_hook(pstate, pref);
	pll_stmt_data *sd = pll_hook_sd;

	if (result != NULL && IsA(result, Param) && sd->preset != NULL)
	{
		Param	   *param = (Param *) result;
		int			idx = param->paramid - 1;

		if (param->paramkind == PARAM_EXTERN && idx >= 0 &&
			idx < sd->npreset && OidIsValid(sd->preset[idx]) &&
			param->paramtype == UNKNOWNOID)
		{
			sd->argtypes[idx] = sd->preset[idx];
			param->paramtype = sd->preset[idx];
			param->paramcollid = get_typcollation(sd->preset[idx]);
		}
	}
	return result;
}

/* Parser setup hook: parameter types not already known are deduced */
static void
pll_parser_setup(ParseState *pstate, void *arg)
{
	pll_stmt_data *sd = (pll_stmt_data *) arg;

	setup_parse_variable_parameters(pstate, &sd->argtypes, &sd->nargs);
	if (sd->preset != NULL)
	{
		pll_hook_sd = sd;
		pll_orig_paramref_hook = pstate->p_paramref_hook;
		pstate->p_paramref_hook = pll_paramref_hook;
	}
}

/*
 * The types of the datum objects among the nargs values starting at first,
 * as an array of OIDs (InvalidOid for other values), in a userdata that is
 * pushed on the stack.  If there are no datum objects, nothing is pushed and
 * NULL is returned.
 */
static Oid *
pll_preset_types(lua_State * L, int first, int nargs)
{
	Oid		   *types;
	bool		any = false;
	int			i;

	types = (Oid *) lua_newuserdatauv(L, sizeof(Oid) * Max(nargs, 1), 0);
	for (i = 0; i < nargs; i++)
	{
		pll_datum  *d = pll_datum_test(L, first + i);

		types[i] = d ? d->ti->typoid : InvalidOid;
		any |= (d != NULL);
	}
	if (!any)
	{
		lua_pop(L, 1);
		return NULL;
	}
	return types;
}

/* Get the query string at idx, checking it for embedded zeroes and encoding */
static void
pll_check_query_string(lua_State * L, int idx, const char **query)
{
	size_t		len;
	const char *s = luaL_checklstring(L, idx, &len);

	if (strlen(s) != len)
		luaL_error(L, "query string contains a zero byte");
	pll_verify_encoding(L, s, len);
	*query = s;
}

/*
 * Prepare a query that is not a statement object, for one use.  The plan is
 * not saved, and is freed by pll_exec_end.
 */
static void
pll_exec_prepare_adhoc(lua_State * L, pll_exec *ex, const char *query,
					   int firstarg, int nargs)
{
	SPIPrepareOptions popts;
	Oid		   *preset = pll_preset_types(L, firstarg, nargs);

	PLL_TRY();
	{
		MemoryContext oldcxt;

		ex->tmpcxt = AllocSetContextCreate(CurrentMemoryContext,
										   "PL/Lua SPI temporary",
										   ALLOCSET_DEFAULT_SIZES);
		oldcxt = MemoryContextSwitchTo(ex->tmpcxt);
		ex->adhoc.mcxt = ex->tmpcxt;
		ex->adhoc.argtypes = palloc0(sizeof(Oid));
		ex->adhoc.nargs = 0;
		ex->adhoc.cursor_options = 0;
		ex->adhoc.fetch_count = PLL_DEFAULT_FETCH_COUNT;
		ex->adhoc.preset = preset;
		ex->adhoc.npreset = nargs;
		MemoryContextSwitchTo(oldcxt);

		popts.parserSetup = pll_parser_setup;
		popts.parserSetupArg = &ex->adhoc;
		popts.parseMode = RAW_PARSE_DEFAULT;
		popts.cursorOptions = 0;
		ex->plan = SPI_prepare_extended(query, &popts);
		if (ex->plan == NULL)
			elog(ERROR, "SPI_prepare failed: %s",
				 SPI_result_code_string(SPI_result));
		ex->adhoc.plan = ex->plan;
		ex->adhoc.preset = NULL;
	}
	PLL_CATCH(L);
	if (preset != NULL)
		lua_pop(L, 1);

	ex->sd = &ex->adhoc;
	ex->is_adhoc = true;
}

/* Start an execution of an existing statement */
static void
pll_exec_begin(lua_State * L, pll_exec *ex)
{
	PLL_TRY();
	{
		ex->tmpcxt = AllocSetContextCreate(CurrentMemoryContext,
										   "PL/Lua SPI temporary",
										   ALLOCSET_DEFAULT_SIZES);
	}
	PLL_CATCH(L);
	ex->plan = ex->sd->plan;
}

/*
 * Convert n Lua values, from index 'first', to parameter values for the
 * query.  The values are allocated in ex->tmpcxt.
 */
static void
pll_exec_params(lua_State * L, pll_exec *ex, int first, int n)
{
	pll_stmt_data *sd = ex->sd;
	MemoryContext oldcxt;
	int			i;

	if (n != sd->nargs)
	{
		int			nparams = sd->nargs;

		PLL_TRY();
		{
			ereport(ERROR,
					(errcode(ERRCODE_SYNTAX_ERROR),
					 errmsg("wrong number of parameters for SPI query"),
					 errdetail("The query has %d parameters, but %d values were given.",
							   nparams, n)));
		}
		PLL_CATCH(L);
	}

	ex->nparams = sd->nargs;
	if (ex->nparams == 0)
		return;

	ex->values = (Datum *) lua_newuserdatauv(L, sizeof(Datum) * ex->nparams, 0);
	ex->nulls = (bool *) lua_newuserdatauv(L, sizeof(bool) * ex->nparams, 0);

	oldcxt = MemoryContextSwitchTo(ex->tmpcxt);
	for (i = 0; i < ex->nparams; i++)
	{
		ex->values[i] = (Datum) 0;
		ex->nulls[i] = true;

		if (lua_isnil(L, first + i))
			continue;

		if (sd->argtypes[i] == InvalidOid || sd->argtypes[i] == UNKNOWNOID)
		{
			/* a parameter of unknown type is passed as a cstring */
			size_t		len;
			const char *s;
			char	   *copy;

			if (lua_type(L, first + i) == LUA_TBOOLEAN)
				s = lua_toboolean(L, first + i) ? "true" : "false";
			else if (lua_type(L, first + i) == LUA_TSTRING ||
					 lua_type(L, first + i) == LUA_TNUMBER)
				s = lua_tolstring(L, first + i, &len);
			else
			{
				MemoryContextSwitchTo(oldcxt);
				luaL_error(L, "cannot convert a %s to the unknown-type parameter $%d",
						   luaL_typename(L, first + i), i + 1);
				s = NULL;		/* keep compiler quiet */
			}
			copy = MemoryContextStrdup(ex->tmpcxt, s);
			ex->values[i] = CStringGetDatum(copy);
			ex->nulls[i] = false;
		}
		else
		{
			pll_typeinfo *ti;
			bool		isnull;

			MemoryContextSwitchTo(oldcxt);
			ti = pll_typeinfo_push(L, sd->argtypes[i], -1);
			MemoryContextSwitchTo(ex->tmpcxt);
			ex->values[i] = pll_value_todatum(L, first + i, ti, &isnull);
			ex->nulls[i] = isnull;
			lua_pop(L, 1);
		}
	}
	MemoryContextSwitchTo(oldcxt);
}

/* Release what an execution used */
/*
 * Free what an execution holds.  This never raises errors, and is safe to
 * call more than once.
 */
static void
pll_exec_release(pll_exec *ex)
{
	MemoryContext cxt;

	/* an error might have been raised while the temporary context was current */
	if (ex->tmpcxt != NULL && CurrentMemoryContext == ex->tmpcxt)
		MemoryContextSwitchTo(MemoryContextGetParent(ex->tmpcxt));
	cxt = CurrentMemoryContext;

	PG_TRY();
	{
		if (ex->tuptable)
			SPI_freetuptable(ex->tuptable);
		ex->tuptable = NULL;
		if (ex->is_adhoc && ex->plan)
			SPI_freeplan(ex->plan);
		ex->plan = NULL;
		if (ex->tmpcxt)
			MemoryContextDelete(ex->tmpcxt);
		ex->tmpcxt = NULL;
	}
	PG_CATCH();
	{
		pll_discard_error(cxt);
	}
	PG_END_TRY();
}

/* __close of the guard of an execution: the call ended with an error */
static int
pll_exec_close_cfunc(lua_State * L)
{
	pll_exec_release((pll_exec *) luaL_checkudata(L, 1, PLL_MT_SPI_EXEC));
	return 0;
}

/*
 * Make a zeroed execution, as a userdata that is on the stack and marked
 * to be closed, so that the temporary state of the execution is freed
 * however the calling function ends, including by an error that is not
 * raised in code that knows about the execution.
 */
static pll_exec *
pll_exec_new(lua_State * L)
{
	pll_exec   *ex = (pll_exec *) lua_newuserdatauv(L, sizeof(pll_exec), 0);

	memset(ex, 0, sizeof(pll_exec));
	luaL_setmetatable(L, PLL_MT_SPI_EXEC);
	lua_toclose(L, -1);
	return ex;
}

/* The execution finished normally */
static void
pll_exec_end(lua_State * L, pll_exec *ex)
{
	pll_exec_release(ex);
}

/* Build the parameter list for an execution; call in PG code only */
static ParamListInfo
pll_exec_paramlist(pll_exec *ex)
{
	ParamListInfo pli;
	int			i;

	if (ex->nparams == 0)
		return NULL;

	pli = makeParamList(ex->nparams);
	for (i = 0; i < ex->nparams; i++)
	{
		Oid			ptype = ex->sd->argtypes[i];

		pli->params[i].value = ex->values[i];
		pli->params[i].isnull = ex->nulls[i];
		pli->params[i].pflags = PARAM_FLAG_CONST;
		pli->params[i].ptype = (ptype == InvalidOid) ? UNKNOWNOID : ptype;
	}
	return pli;
}

/*
 * Is the plan a single CALL or DO command?  Only those may run in nonatomic
 * mode, which skips the per-statement snapshot and command counter handling
 * that other commands rely on, as in PL/pgSQL.  PG code only.
 */
static bool
pll_plan_is_call(SPIPlanPtr plan)
{
	List	   *sources = SPI_plan_get_plan_sources(plan);
	CachedPlanSource *source;

	if (list_length(sources) != 1)
		return false;
	source = (CachedPlanSource *) linitial(sources);
	return source->commandTag == CMDTAG_CALL || source->commandTag == CMDTAG_DO;
}

/* Run the plan, leaving the results in ex */
static void
pll_exec_run(lua_State * L, pll_exec *ex, long tcount)
{
	PLL_TRY();
	{
		SPIExecuteOptions opts;

		memset(&opts, 0, sizeof(opts));
		opts.params = pll_exec_paramlist(ex);
		opts.read_only = pll_call_readonly();
		opts.allow_nonatomic = !pll_call_atomic() && pll_plan_is_call(ex->plan);
		opts.tcount = tcount;

		ex->rc = SPI_execute_plan_extended(ex->plan, &opts);
		if (ex->rc < 0)
		{
			if (ex->rc == SPI_ERROR_COPY)
				ereport(ERROR,
						(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						 errmsg("cannot COPY to/from client in PL/Lua")));
			else if (ex->rc == SPI_ERROR_TRANSACTION)
				ereport(ERROR,
						(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						 errmsg("unsupported transaction command in PL/Lua"),
						 errhint("Use spi.commit() or spi.rollback() to end a transaction.")));
			else
				elog(ERROR, "SPI execution failed: %s",
					 SPI_result_code_string(ex->rc));
		}
		ex->tuptable = SPI_tuptable;
		ex->processed = SPI_processed;
	}
	PLL_CATCH(L);
}

/*
 * Open a cursor on the plan.  A null name means a system-assigned one.  The
 * name of the portal is left in ex->portalname.
 */
static void
pll_exec_open_cursor(lua_State * L, pll_exec *ex, const char *name)
{
	PLL_TRY();
	{
		ParamListInfo pli = pll_exec_paramlist(ex);
		Portal		portal;

		if (name != NULL && name[0] != '\0' && SPI_cursor_find(name) != NULL)
			ereport(ERROR,
					(errcode(ERRCODE_DUPLICATE_CURSOR),
					 errmsg("cursor \"%s\" is already open", name)));

		portal = SPI_cursor_open_with_paramlist(name, ex->plan, pli,
												pll_call_readonly());
		/* the portal's name lives as long as the portal */
		ex->portalname = pstrdup(portal->name);
	}
	PLL_CATCH(L);
}

/* Free a tuple table (that is not necessarily SPI_tuptable) */
static void
pll_free_tuptable(lua_State * L, SPITupleTable *tuptable)
{
	if (tuptable == NULL)
		return;

	PLL_TRY();
	{
		SPI_freetuptable(tuptable);
	}
	PLL_CATCH(L);
}

/* Push a table of the rows in the tuple table */
static void
pll_push_tuptable(lua_State * L, SPITupleTable *tuptable, uint64 processed)
{
	uint64		i;

	luaL_checkstack(L, 4, "out of stack space");
	lua_createtable(L, (int) Min(processed, 1024), 0);
	for (i = 0; tuptable != NULL && i < processed; i++)
	{
		pll_row_push(L, tuptable->vals[i], tuptable->tupdesc);
		lua_rawseti(L, -2, (lua_Integer) (i + 1));
	}
}

/*
 * Push the result of a query: a table of rows if it returned rows, and
 * otherwise the number of rows processed.  Frees the tuple table.
 */
static void
pll_push_exec_result(lua_State * L, pll_exec *ex)
{
	if (ex->tuptable != NULL)
		pll_push_tuptable(L, ex->tuptable, ex->processed);
	else
		lua_pushinteger(L, (lua_Integer) ex->processed);
	pll_free_tuptable(L, ex->tuptable);
	ex->tuptable = NULL;
}

/*
 * The common part of spi.execute, spi.execute_count and the statement
 * methods.  If 'method', the statement object is at index 1 and the
 * arguments follow it (after maxrows, if execute_count); otherwise the
 * query string is at qidx.
 */
static int
pll_spi_execute_common(lua_State * L, int qidx, int firstarg, long maxrows,
					   bool method)
{
	pll_exec   *ex;
	int			nargs = lua_gettop(L) - firstarg + 1;
	const char *query;

	ex = pll_exec_new(L);
	if (nargs < 0)
		nargs = 0;

	if (method)
	{
		ex->sd = pll_check_stmt(L, qidx);
		pll_exec_begin(L, ex);
	}
	else
	{
		pll_check_query_string(L, qidx, &query);
		pll_exec_prepare_adhoc(L, ex, query, firstarg, nargs);
	}

	pll_exec_params(L, ex, firstarg, nargs);
	pll_exec_run(L, ex, maxrows);
	pll_push_exec_result(L, ex);
	pll_exec_end(L, ex);
	return 1;
}

/* Get the maximum row count argument of execute_count */
static long
pll_get_maxrows(lua_State * L, int idx)
{
	lua_Integer n;

	if (lua_isnoneornil(L, idx))
		return 0;
	n = luaL_checkinteger(L, idx);
	if (n < 0)
		luaL_error(L, "the maximum number of rows cannot be negative");
	return (long) n;
}

static int
pll_spi_execute(lua_State * L)
{
	return pll_spi_execute_common(L, 1, 2, 0, false);
}

static int
pll_spi_execute_count(lua_State * L)
{
	long		maxrows = pll_get_maxrows(L, 2);

	return pll_spi_execute_common(L, 1, 3, maxrows, false);
}

static int
pll_stmt_execute(lua_State * L)
{
	return pll_spi_execute_common(L, 1, 2, 0, true);
}

static int
pll_stmt_execute_count(lua_State * L)
{
	long		maxrows = pll_get_maxrows(L, 2);

	return pll_spi_execute_common(L, 1, 3, maxrows, true);
}

/* ------------------------------------------------------------------------
 * Statements
 * ------------------------------------------------------------------------
 */

static pll_stmt_data *
pll_check_stmt(lua_State * L, int idx)
{
	pll_stmt   *st = (pll_stmt *) luaL_checkudata(L, idx, PLL_MT_STMT);

	if (st->data == NULL)
		luaL_error(L, "the statement is not usable");
	return st->data;
}

static int
pll_stmt_gc(lua_State * L)
{
	pll_stmt   *st = (pll_stmt *) luaL_checkudata(L, 1, PLL_MT_STMT);
	pll_stmt_data *sd = st->data;
	MemoryContext cxt;

	if (sd == NULL)
		return 0;
	st->data = NULL;

	/* never raise an error from a finalizer */
	cxt = CurrentMemoryContext;
	PG_TRY();
	{
		if (sd->plan)
			SPI_freeplan(sd->plan);
		MemoryContextDelete(sd->mcxt);
	}
	PG_CATCH();
	{
		pll_discard_error(cxt);
	}
	PG_END_TRY();
	return 0;
}

/* Does the options table at idx have a true value for the key? */
static bool
pll_option_true(lua_State * L, int idx, const char *key)
{
	bool		result;

	lua_getfield(L, idx, key);
	result = lua_toboolean(L, -1);
	lua_pop(L, 1);
	return result;
}

static void
pll_read_options(lua_State * L, int idx, pll_stmt_data *sd)
{
	int			opts = 0;

	sd->fetch_count = PLL_DEFAULT_FETCH_COUNT;
	if (lua_isnoneornil(L, idx))
		return;
	luaL_checktype(L, idx, LUA_TTABLE);

	if (pll_option_true(L, idx, "scroll"))
		opts |= CURSOR_OPT_SCROLL;
	if (pll_option_true(L, idx, "no_scroll"))
		opts |= CURSOR_OPT_NO_SCROLL;
	if (pll_option_true(L, idx, "hold"))
		opts |= CURSOR_OPT_HOLD;
	if (pll_option_true(L, idx, "fast_start"))
		opts |= CURSOR_OPT_FAST_PLAN;
	if (pll_option_true(L, idx, "custom_plan"))
		opts |= CURSOR_OPT_CUSTOM_PLAN;
	if (pll_option_true(L, idx, "generic_plan"))
		opts |= CURSOR_OPT_GENERIC_PLAN;
	if ((opts & CURSOR_OPT_SCROLL) && (opts & CURSOR_OPT_NO_SCROLL))
		luaL_error(L, "scroll and no_scroll cannot both be specified");
	if ((opts & CURSOR_OPT_CUSTOM_PLAN) && (opts & CURSOR_OPT_GENERIC_PLAN))
		luaL_error(L, "custom_plan and generic_plan cannot both be specified");
	sd->cursor_options = opts;

	lua_getfield(L, idx, "fetch_count");
	if (!lua_isnil(L, -1))
	{
		lua_Integer n = luaL_checkinteger(L, -1);

		if (n < 1 || n > INT_MAX)
			luaL_error(L, "fetch_count must be positive");
		sd->fetch_count = (int) n;
	}
	lua_pop(L, 1);
}

/*
 * Get the parameter types from the table at idx (type names or typeinfo
 * objects) as an array of OIDs allocated as a Lua userdata, left on the
 * stack.
 */
static void
pll_collect_argtypes(lua_State * L, int idx, Oid **types, int *ntypes)
{
	lua_Integer n = 0;
	lua_Integer i;

	if (!lua_isnoneornil(L, idx))
	{
		luaL_checktype(L, idx, LUA_TTABLE);
		n = luaL_len(L, idx);
	}
	if (n > MaxAllocSize / sizeof(Oid) / 2)
		luaL_error(L, "too many parameter types");

	*types = (Oid *) lua_newuserdatauv(L, sizeof(Oid) * Max(n, 1), 0);
	*ntypes = (int) n;

	for (i = 1; i <= n; i++)
	{
		pll_typeinfo *ti;

		(*types)[i - 1] = InvalidOid;
		lua_geti(L, idx, i);
		if (lua_isnil(L, -1))
		{
			lua_pop(L, 1);
			continue;
		}
		ti = pll_typeinfo_test(L, -1);
		if (ti != NULL)
			(*types)[i - 1] = ti->typoid;
		else if (lua_type(L, -1) == LUA_TSTRING)
		{
			const char *name = lua_tostring(L, -1);

			PLL_TRY();
			{
				Oid			typid;
				int32		typmod;

				(void) parseTypeString(name, &typid, &typmod, NULL);
				(*types)[i - 1] = typid;
			}
			PLL_CATCH(L);
		}
		else
			luaL_error(L, "parameter types must be type names or typeinfo objects");
		lua_pop(L, 1);
	}
}

/* spi.prepare(query [, argtypes [, options]]) */
static int
pll_spi_prepare(lua_State * L)
{
	const char *query;
	Oid		   *types;
	int			ntypes;
	pll_stmt   *st;
	pll_stmt_data tmp;
	Size		plan_bytes = 0;

	pll_check_query_string(L, 1, &query);
	memset(&tmp, 0, sizeof(tmp));
	pll_read_options(L, 3, &tmp);
	pll_collect_argtypes(L, 2, &types, &ntypes);

	st = (pll_stmt *) lua_newuserdatauv(L, sizeof(pll_stmt), 0);
	st->data = NULL;
	luaL_setmetatable(L, PLL_MT_STMT);

	PLL_TRY();
	{
		MemoryContext mcxt;
		pll_stmt_data *sd;
		SPIPrepareOptions popts;
		SPIPlanPtr	plan;

		mcxt = AllocSetContextCreate(TopMemoryContext,
									 "PL/Lua prepared statement",
									 ALLOCSET_SMALL_SIZES);
		sd = MemoryContextAllocZero(mcxt, sizeof(pll_stmt_data));
		sd->mcxt = mcxt;
		sd->cursor_options = tmp.cursor_options;
		sd->fetch_count = tmp.fetch_count;
		sd->nargs = ntypes;
		sd->argtypes = MemoryContextAlloc(mcxt, sizeof(Oid) * Max(ntypes, 1));
		memcpy(sd->argtypes, types, sizeof(Oid) * Max(ntypes, 1));

		popts.parserSetup = pll_parser_setup;
		popts.parserSetupArg = sd;
		popts.parseMode = RAW_PARSE_DEFAULT;
		popts.cursorOptions = sd->cursor_options;

		PG_TRY(2);
		{
			Size		before = MemoryContextMemAllocated(CacheMemoryContext, true);

			plan = SPI_prepare_extended(query, &popts);
			if (plan == NULL)
				elog(ERROR, "SPI_prepare failed: %s",
					 SPI_result_code_string(SPI_result));
			if (SPI_keepplan(plan))
				elog(ERROR, "SPI_keepplan failed");

			/* the saved plan lives in the plan cache, outside Lua's memory */
			plan_bytes = MemoryContextMemAllocated(CacheMemoryContext, true) - before;
		}
		PG_CATCH(2);
		{
			MemoryContextDelete(mcxt);
			PG_RE_THROW();
		}
		PG_END_TRY(2);

		sd->plan = plan;
		st->data = sd;
	}
	PLL_CATCH(L);

	/* for the extra garbage collection that the settings ask for */
	pll_note_external_alloc(L, plan_bytes);

	return 1;
}

static int
pll_stmt_numargs(lua_State * L)
{
	pll_stmt_data *sd = pll_check_stmt(L, 1);

	lua_pushinteger(L, sd->nargs);
	return 1;
}

/* The type of the n'th parameter, or nil if it is not known */
static int
pll_stmt_argtype(lua_State * L)
{
	pll_stmt_data *sd = pll_check_stmt(L, 1);
	lua_Integer n = luaL_checkinteger(L, 2);

	if (n < 1 || n > sd->nargs || sd->argtypes[n - 1] == InvalidOid)
		lua_pushnil(L);
	else
		(void) pll_typeinfo_push(L, sd->argtypes[n - 1], -1);
	return 1;
}

/* ------------------------------------------------------------------------
 * Cursors
 * ------------------------------------------------------------------------
 */

static pll_cursor *
pll_check_cursor(lua_State * L, int idx)
{
	return (pll_cursor *) luaL_checkudata(L, idx, PLL_MT_CURSOR);
}

/*
 * The name of the cursor's portal, or NULL if it has none yet.  The string
 * is valid as long as the cursor object is.
 */
static const char *
pll_cursor_getname(lua_State * L, int idx)
{
	const char *name = NULL;

	lua_getiuservalue(L, idx, 1);
	if (lua_type(L, -1) == LUA_TSTRING)
		name = lua_tostring(L, -1);
	lua_pop(L, 1);
	return name;
}

/* Make the cursor at idx the registered cursor for the portal name */
static void
pll_cursor_register(lua_State * L, int idx, const char *name)
{
	idx = lua_absindex(L, idx);
	lua_rawgetp(L, LUA_REGISTRYINDEX, &pll_spi_cursors_key);
	lua_pushvalue(L, idx);
	lua_setfield(L, -2, name);
	lua_pop(L, 1);
}

/* Push a new cursor object, with the name if not NULL */
static void
pll_cursor_make(lua_State * L, const char *name, bool owned)
{
	pll_cursor *c = (pll_cursor *) lua_newuserdatauv(L, sizeof(pll_cursor), 1);

	c->owned = owned;
	if (name != NULL)
		lua_pushstring(L, name);
	else
		lua_pushnil(L);
	lua_setiuservalue(L, -2, 1);
	luaL_setmetatable(L, PLL_MT_CURSOR);
}

/* Push the registered cursor object for the portal name, making one if needed */
static void
pll_cursor_find_or_make(lua_State * L, const char *name)
{
	lua_rawgetp(L, LUA_REGISTRYINDEX, &pll_spi_cursors_key);
	lua_getfield(L, -1, name);
	if (lua_isnil(L, -1))
	{
		lua_pop(L, 1);
		pll_cursor_make(L, name, false);
		lua_pushvalue(L, -1);
		lua_setfield(L, -3, name);
	}
	lua_remove(L, -2);
}

/* After opening a portal: record its name in the cursor object at idx */
static void
pll_cursor_set_opened(lua_State * L, int idx, const char *portalname)
{
	pll_cursor *c = pll_check_cursor(L, idx);

	idx = lua_absindex(L, idx);
	lua_pushstring(L, portalname);
	lua_setiuservalue(L, idx, 1);
	c->owned = true;
	pll_cursor_register(L, idx, portalname);
}

/* Close the portal with the given name, if it is open */
static void
pll_cursor_close_portal(lua_State * L, const char *name)
{
	PLL_TRY();
	{
		Portal		portal = SPI_cursor_find(name);

		if (portal != NULL)
			SPI_cursor_close(portal);
	}
	PLL_CATCH(L);
}

/* The finalizer closes a cursor that it owns; it must not raise errors */
static int
pll_cursor_gc(lua_State * L)
{
	pll_cursor *c = (pll_cursor *) luaL_checkudata(L, 1, PLL_MT_CURSOR);
	const char *name = pll_cursor_getname(L, 1);
	MemoryContext cxt = CurrentMemoryContext;

	if (c->owned && name != NULL)
	{
		PG_TRY();
		{
			Portal		portal = SPI_cursor_find(name);

			if (portal != NULL)
				SPI_cursor_close(portal);
		}
		PG_CATCH();
		{
			pll_discard_error(cxt);
		}
		PG_END_TRY();
	}
	return 0;
}

/* spi.findcursor(name) */
static int
pll_spi_findcursor(lua_State * L)
{
	const char *name = luaL_checkstring(L, 1);

	if (!pll_portal_is_open(L, name))
		lua_pushnil(L);
	else
		pll_cursor_find_or_make(L, name);
	return 1;
}

/* spi.newcursor([name]) */
static int
pll_spi_newcursor(lua_State * L)
{
	const char *name = luaL_optstring(L, 1, NULL);

	if (name == NULL)
		pll_cursor_make(L, NULL, false);
	else
		pll_cursor_find_or_make(L, name);
	return 1;
}

void
pll_cursor_push(lua_State * L, const char *portalname)
{
	pll_cursor_find_or_make(L, portalname);
}

const char *
pll_cursor_name(lua_State * L, int idx)
{
	if (luaL_testudata(L, idx, PLL_MT_CURSOR) == NULL)
		return NULL;
	return pll_cursor_getname(L, idx);
}

/* cursor:open(statement-or-query, args...) */
static int
pll_cursor_open(lua_State * L)
{
	pll_cursor *c = pll_check_cursor(L, 1);
	const char *name = pll_cursor_getname(L, 1);
	pll_exec   *ex;
	int			nargs = lua_gettop(L) - 2;
	const char *query;
	bool		is_stmt = (luaL_testudata(L, 2, PLL_MT_STMT) != NULL);

	(void) c;
	ex = pll_exec_new(L);
	if (nargs < 0)
		nargs = 0;

	if (is_stmt)
	{
		ex->sd = pll_check_stmt(L, 2);
		pll_exec_begin(L, ex);
	}
	else
	{
		pll_check_query_string(L, 2, &query);
		pll_exec_prepare_adhoc(L, ex, query, 3, nargs);
	}

	pll_exec_params(L, ex, 3, nargs);
	pll_exec_open_cursor(L, ex, name);
	pll_cursor_set_opened(L, 1, ex->portalname);
	pll_exec_end(L, ex);

	lua_settop(L, 1);
	return 1;
}

static int
pll_cursor_rows(lua_State * L)
{
	(void) pll_check_cursor(L, 1);
	lua_settop(L, 1);
	lua_pushvalue(L, 1);
	/* one row at a time, so that WHERE CURRENT OF works in the loop body */
	lua_pushinteger(L, 1);
	lua_pushnil(L);
	lua_pushinteger(L, 0);
	lua_pushboolean(L, 0);		/* leave the cursor open at the end */
	lua_pushcclosure(L, pll_rows_iter, 5);
	lua_pushnil(L);
	lua_pushnil(L);
	lua_pushnil(L);				/* nothing to close after a break either */
	return 4;
}

/*
 * Fetch from or move in the cursor, and push the table of rows (nothing for
 * a move).
 */
static void
pll_cursor_fetch_push(lua_State * L, const char *name, FetchDirection dir,
					  long count, bool move)
{
	SPITupleTable *volatile tuptable = NULL;
	volatile uint64 processed = 0;

	PLL_TRY();
	{
		Portal		portal = SPI_cursor_find(name);

		if (portal == NULL)
			ereport(ERROR,
					(errcode(ERRCODE_UNDEFINED_CURSOR),
					 errmsg("cursor \"%s\" is not open", name)));

		if (move)
			SPI_scroll_cursor_move(portal, dir, count);
		else
			SPI_scroll_cursor_fetch(portal, dir, count);
		tuptable = SPI_tuptable;
		processed = SPI_processed;
	}
	PLL_CATCH(L);

	if (move)
		return;

	pll_push_tuptable(L, tuptable, processed);
	pll_free_tuptable(L, tuptable);
}

static int
pll_cursor_fetch_or_move(lua_State * L, bool move)
{
	const char *name;
	lua_Integer count = luaL_optinteger(L, 2, 1);
	const char *dirname = luaL_optstring(L, 3, "forward");
	FetchDirection dir = FETCH_FORWARD;
	int			i;

	(void) pll_check_cursor(L, 1);
	name = pll_cursor_getname(L, 1);
	if (name == NULL)
		luaL_error(L, "the cursor is not open");

	for (i = 0; pll_directions[i].name; i++)
	{
		if (strcmp(dirname, pll_directions[i].name) == 0)
		{
			dir = pll_directions[i].dir;
			break;
		}
	}
	if (pll_directions[i].name == NULL)
		luaL_error(L, "invalid fetch direction \"%s\"", dirname);
	if (count < 0)
		luaL_error(L, "the count cannot be negative");

	pll_cursor_fetch_push(L, name, dir, (long) count, move);
	return move ? 0 : 1;
}

static int
pll_cursor_fetch(lua_State * L)
{
	return pll_cursor_fetch_or_move(L, false);
}

static int
pll_cursor_move(lua_State * L)
{
	return pll_cursor_fetch_or_move(L, true);
}

/* Is there a portal with this name? */
static bool
pll_portal_is_open(lua_State * L, const char *name)
{
	volatile bool open = false;

	PLL_TRY();
	{
		open = (SPI_cursor_find(name) != NULL);
	}
	PLL_CATCH(L);
	return open;
}

static int
pll_cursor_isopen(lua_State * L)
{
	const char *name;

	(void) pll_check_cursor(L, 1);
	name = pll_cursor_getname(L, 1);
	lua_pushboolean(L, name != NULL && pll_portal_is_open(L, name));
	return 1;
}

static int
pll_cursor_close(lua_State * L)
{
	const char *name;

	(void) pll_check_cursor(L, 1);
	name = pll_cursor_getname(L, 1);
	if (name != NULL)
		pll_cursor_close_portal(L, name);
	return 0;
}

/* __close: for the closing value of the generic for loop of rows() */
static int
pll_cursor_close_hook(lua_State * L)
{
	pll_cursor *c = pll_check_cursor(L, 1);

	if (c->owned)
		return pll_cursor_close(L);
	return 0;
}

static int
pll_cursor_name_method(lua_State * L)
{
	const char *name;

	(void) pll_check_cursor(L, 1);
	name = pll_cursor_getname(L, 1);
	if (name == NULL)
		lua_pushnil(L);
	else
		lua_pushstring(L, name);
	return 1;
}

static int
pll_cursor_isowned(lua_State * L)
{
	pll_cursor *c = pll_check_cursor(L, 1);

	lua_pushboolean(L, c->owned);
	return 1;
}

static int
pll_cursor_own(lua_State * L)
{
	pll_cursor *c = pll_check_cursor(L, 1);

	c->owned = true;
	lua_settop(L, 1);
	return 1;
}

static int
pll_cursor_disown(lua_State * L)
{
	pll_cursor *c = pll_check_cursor(L, 1);

	c->owned = false;
	lua_settop(L, 1);
	return 1;
}

/*
 * The iterator function of rows().  Its upvalues are the cursor, the number
 * of rows to fetch at a time, the current batch (nil before the first fetch,
 * false after the last), the index of the last row returned from it, and
 * whether to close the cursor when the rows run out.
 */
static int
pll_rows_iter(lua_State * L)
{
	lua_Integer idx;
	const char *name;

	if (lua_isboolean(L, lua_upvalueindex(3)))
		return 0;

	/* a cursor closed behind our back, by a commit for instance */
	name = pll_cursor_getname(L, lua_upvalueindex(1));
	if (name == NULL || !pll_portal_is_open(L, name))
		return luaL_error(L, "cannot iterate a closed cursor");

	idx = lua_tointeger(L, lua_upvalueindex(4)) + 1;
	if (!lua_isnil(L, lua_upvalueindex(3)) &&
		idx <= (lua_Integer) lua_rawlen(L, lua_upvalueindex(3)))
	{
		lua_pushinteger(L, idx);
		lua_replace(L, lua_upvalueindex(4));
		lua_rawgeti(L, lua_upvalueindex(3), idx);
		return 1;
	}

	/* fetch another batch */
	pll_cursor_fetch_push(L, name, FETCH_FORWARD,
						  (long) lua_tointeger(L, lua_upvalueindex(2)), false);
	if (lua_rawlen(L, -1) == 0)
	{
		/* all done: close the cursor if the loop made it, and remember that */
		if (lua_toboolean(L, lua_upvalueindex(5)))
			pll_cursor_close_portal(L, name);
		lua_pushboolean(L, 0);
		lua_replace(L, lua_upvalueindex(3));
		return 0;
	}
	lua_pushvalue(L, -1);
	lua_replace(L, lua_upvalueindex(3));
	lua_pushinteger(L, 1);
	lua_replace(L, lua_upvalueindex(4));
	lua_rawgeti(L, -1, 1);
	return 1;
}

/*
 * The cursor for a rows() call is open and described by ex; push the
 * iterator function and the other values for the generic for loop.
 */
static void
pll_push_rows_iterator(lua_State * L, pll_exec *ex, int fetch_count)
{
	pll_cursor_make(L, NULL, true);
	pll_cursor_set_opened(L, -1, ex->portalname);

	lua_pushvalue(L, -1);		/* the cursor, as upvalue 1 */
	lua_pushinteger(L, fetch_count);
	lua_pushnil(L);
	lua_pushinteger(L, 0);
	lua_pushboolean(L, 1);		/* close the cursor at the end */
	lua_pushcclosure(L, pll_rows_iter, 5);
	lua_pushnil(L);
	lua_pushnil(L);
	lua_pushvalue(L, -4);		/* the cursor, as the closing value */
}

/* spi.rows(query, args...) */
static int
pll_spi_rows(lua_State * L)
{
	pll_exec   *ex;
	int			nargs = lua_gettop(L) - 1;
	const char *query;

	ex = pll_exec_new(L);
	pll_check_query_string(L, 1, &query);
	pll_exec_prepare_adhoc(L, ex, query, 2, Max(nargs, 0));
	pll_exec_params(L, ex, 2, Max(nargs, 0));
	pll_exec_open_cursor(L, ex, NULL);
	pll_push_rows_iterator(L, ex, PLL_DEFAULT_FETCH_COUNT);
	pll_exec_end(L, ex);
	return 4;
}

/* statement:rows(args...) */
static int
pll_stmt_rows(lua_State * L)
{
	pll_exec   *ex;
	int			nargs = lua_gettop(L) - 1;

	ex = pll_exec_new(L);
	ex->sd = pll_check_stmt(L, 1);
	pll_exec_begin(L, ex);
	pll_exec_params(L, ex, 2, Max(nargs, 0));
	pll_exec_open_cursor(L, ex, NULL);
	pll_push_rows_iterator(L, ex, ex->sd->fetch_count);
	pll_exec_end(L, ex);
	return 4;
}

/* statement:getcursor(args...) */
static int
pll_stmt_getcursor(lua_State * L)
{
	pll_exec   *ex;
	int			nargs = lua_gettop(L) - 1;

	ex = pll_exec_new(L);
	ex->sd = pll_check_stmt(L, 1);
	pll_exec_begin(L, ex);
	pll_exec_params(L, ex, 2, Max(nargs, 0));
	pll_exec_open_cursor(L, ex, NULL);
	pll_cursor_make(L, NULL, true);
	pll_cursor_set_opened(L, -1, ex->portalname);
	pll_exec_end(L, ex);
	return 1;
}

/* ------------------------------------------------------------------------
 * Miscellaneous functions
 * ------------------------------------------------------------------------
 */

static int
pll_spi_readonly(lua_State * L)
{
	lua_pushboolean(L, pll_call_readonly());
	return 1;
}

static int
pll_spi_is_atomic(lua_State * L)
{
	lua_pushboolean(L, pll_call_atomic());
	return 1;
}

static int
pll_spi_commit(lua_State * L)
{
	PLL_TRY();
	{
		SPI_commit();
		SPI_start_transaction();
	}
	PLL_CATCH(L);
	return 0;
}

static int
pll_spi_rollback(lua_State * L)
{
	PLL_TRY();
	{
		SPI_rollback();
		SPI_start_transaction();
	}
	PLL_CATCH(L);
	return 0;
}

/*
 * Get the string argument of a quoting function, raising an error if it has
 * a zero byte or is not valid in the database encoding.
 */
static const char *
pll_quote_arg(lua_State * L, int idx)
{
	size_t		len;
	const char *s = luaL_checklstring(L, idx, &len);

	if (strlen(s) != len)
		luaL_error(L, "string contains a zero byte");
	pll_verify_encoding(L, s, len);
	return s;
}

static int
pll_spi_quote_ident(lua_State * L)
{
	const char *s = pll_quote_arg(L, 1);
	const char *volatile quoted = NULL;

	PLL_TRY();
	{
		quoted = quote_identifier(s);
	}
	PLL_CATCH(L);
	lua_pushstring(L, quoted);
	return 1;
}

/* Quote a literal; shared by quote_literal and quote_nullable */
static void
pll_push_quoted_literal(lua_State * L, const char *s)
{
	char	   *volatile quoted = NULL;

	PLL_TRY();
	{
		quoted = quote_literal_cstr(s);
	}
	PLL_CATCH(L);
	lua_pushstring(L, quoted);
}

static int
pll_spi_quote_literal(lua_State * L)
{
	pll_push_quoted_literal(L, pll_quote_arg(L, 1));
	return 1;
}

static int
pll_spi_quote_nullable(lua_State * L)
{
	if (lua_isnoneornil(L, 1))
		lua_pushliteral(L, "NULL");
	else
		pll_push_quoted_literal(L, pll_quote_arg(L, 1));
	return 1;
}
