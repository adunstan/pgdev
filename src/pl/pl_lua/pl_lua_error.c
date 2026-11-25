/*-------------------------------------------------------------------------
 *
 * pl_lua_error.c
 *	  Error objects, protected calls, and message functions.
 *
 * PostgreSQL errors that occur in C functions called from Lua are turned
 * into Lua error objects (tables with a private metatable) by PLL_CATCH, and
 * propagate as ordinary Lua errors.  The replacement pcall() wraps the call
 * in a subtransaction so that catching one is safe.  When an error reaches a
 * PostgreSQL frame, pll_rethrow() turns it back into a PostgreSQL error.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua_error.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "access/xact.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "utils/memutils.h"
#include "utils/resowner.h"

#include "pl_lua_int.h"

/* the registry key of the metatable of error objects */
static const char pll_errmt_key = 0;

/* condition names, from errcodes.txt */
typedef struct pll_condition
{
	const char *label;
	int			sqlerrstate;
} pll_condition;

static const pll_condition pll_conditions[] = {
#include "plerrcodes.h"
	{NULL, 0}
};

/* elog level names */
typedef struct pll_level
{
	const char *name;
	int			elevel;
} pll_level;

static const pll_level pll_levels[] = {
	{"debug", DEBUG1},
	{"debug1", DEBUG1},
	{"debug2", DEBUG2},
	{"debug3", DEBUG3},
	{"debug4", DEBUG4},
	{"debug5", DEBUG5},
	{"log", LOG},
	{"info", INFO},
	{"notice", NOTICE},
	{"warning", WARNING},
	{"error", ERROR},
	{NULL, 0}
};

/* the fields of an error, as extracted from an error object or a message */
typedef struct pll_errinfo
{
	int			sqlerrcode;
	char	   *message;
	char	   *detail;
	char	   *hint;
	char	   *context;
	char	   *schema;
	char	   *table;
	char	   *column;
	char	   *datatype;
	char	   *constraint;
	char	   *internalquery;
	char	   *file;
	char	   *func;
	int			cursorpos;
	int			internalpos;
	int			line;
	bool		from_object;
} pll_errinfo;

/* the elog fields of the table form of elog(), in order of the struct */
typedef struct pll_elog_args
{
	int			elevel;
	int			sqlerrcode;		/* 0 if none given */
	const char *message;
	const char *detail;
	const char *hint;
	const char *schema;
	const char *table;
	const char *column;
	const char *datatype;
	const char *constraint;
} pll_elog_args;

static void pll_push_errmt(lua_State * L);
static const char *pll_error_class_name(int sqlstate, char *buf);
static void pll_set_string_field(lua_State * L, const char *name,
								 const char *value);
static void pll_set_int_field(lua_State * L, const char *name, int value);
static int	pll_error_tostring(lua_State * L);
static char *pll_get_string_field(lua_State * L, int idx, const char *name);
static int	pll_get_int_field(lua_State * L, int idx, const char *name);
static int	pll_extract_cfunc(lua_State * L);
static bool pll_error_is_uncatchable(lua_State * L, int idx);
static int	pll_subxact_pcall(lua_State * L, bool use_handler);
static int	pll_lua_pcall(lua_State * L);
static int	pll_lua_xpcall(lua_State * L);
static int	pll_lua_lpcall(lua_State * L);
static int	pll_lua_resume(lua_State * L);
static int	pll_lua_print(lua_State * L);
static int	pll_lua_warn(lua_State * L);
static void pll_emit_message(lua_State * L, int elevel, const char *msg);
static int	pll_level_by_name(lua_State * L, const char *name);
static const char *pll_opt_string(lua_State * L, int idx);
static const char *pll_field_string(lua_State * L, int tbl, const char *name);
static void pll_do_elog(lua_State * L, int elevel, int first);
static int	pll_elog_level_func(lua_State * L);
static int	pll_elog_func(lua_State * L);
static int	pll_err_type(lua_State * L);
static int	pll_err_field(lua_State * L);

/* ------------------------------------------------------------------------
 * Protected calls
 * ------------------------------------------------------------------------
 */

int
pll_pcall(lua_State * L, int nargs, int nresults, int msgh)
{
	sigjmp_buf *save_exception_stack = PG_exception_stack;
	ErrorContextCallback *save_context_stack = error_context_stack;
	int			status;

	status = lua_pcall(L, nargs, nresults, msgh);
	if (status != LUA_OK)
	{
		PG_exception_stack = save_exception_stack;
		error_context_stack = save_context_stack;
	}
	return status;
}

void
pll_pcall_rethrow(lua_State * L, int nargs, int nresults)
{
	if (pll_pcall(L, nargs, nresults, 0) != LUA_OK)
		pll_rethrow(L);
}

int
pll_resume(lua_State * co, lua_State * L, int nargs, int *nresults)
{
	sigjmp_buf *save_exception_stack = PG_exception_stack;
	ErrorContextCallback *save_context_stack = error_context_stack;
	int			status;

	status = lua_resume(co, L, nargs, nresults);
	if (status != LUA_OK && status != LUA_YIELD)
	{
		PG_exception_stack = save_exception_stack;
		error_context_stack = save_context_stack;
	}
	return status;
}

/* ------------------------------------------------------------------------
 * Conditions
 * ------------------------------------------------------------------------
 */

const char *
pll_condition_name(int sqlstate)
{
	const pll_condition *c;

	for (c = pll_conditions; c->label != NULL; c++)
	{
		if (c->sqlerrstate == sqlstate)
			return c->label;
	}
	return NULL;
}

int
pll_sqlstate_parse(lua_State * L, const char *s)
{
	const pll_condition *c;
	size_t		len = strlen(s);
	size_t		i;
	bool		ok = (len == 5);

	for (i = 0; ok && i < 5; i++)
	{
		if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'A' && s[i] <= 'Z')))
			ok = false;
	}
	if (ok)
		return MAKE_SQLSTATE(s[0], s[1], s[2], s[3], s[4]);

	for (c = pll_conditions; c->label != NULL; c++)
	{
		if (strcmp(c->label, s) == 0)
			return c->sqlerrstate;
	}

	PLL_TRY();
	{
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("unrecognized SQLSTATE or condition name: \"%s\"", s)));
	}
	PLL_CATCH(L);
	return 0;					/* not reached */
}

/* The name of the error class of a SQLSTATE, or else the class code */
static const char *
pll_error_class_name(int sqlstate, char *buf)
{
	const char *name = pll_condition_name(ERRCODE_TO_CATEGORY(sqlstate));

	if (name != NULL)
		return name;
	strlcpy(buf, unpack_sql_state(sqlstate), 3);
	return buf;
}

/* ------------------------------------------------------------------------
 * Error objects
 * ------------------------------------------------------------------------
 */

/* Push the metatable of error objects, creating it if necessary */
static void
pll_push_errmt(lua_State * L)
{
	if (lua_rawgetp(L, LUA_REGISTRYINDEX, &pll_errmt_key) != LUA_TNIL)
		return;
	lua_pop(L, 1);

	lua_createtable(L, 0, 3);
	lua_pushstring(L, "pl_lua.error");
	lua_setfield(L, -2, "__name");
	lua_pushcfunction(L, pll_error_tostring);
	lua_setfield(L, -2, "__tostring");
	/* keep the metatable private */
	lua_pushboolean(L, 0);
	lua_setfield(L, -2, "__metatable");
	lua_pushvalue(L, -1);
	lua_rawsetp(L, LUA_REGISTRYINDEX, &pll_errmt_key);
}

bool
pll_is_error_object(lua_State * L, int idx)
{
	bool		result;

	if (lua_type(L, idx) != LUA_TTABLE)
		return false;
	if (!lua_getmetatable(L, idx))
		return false;
	lua_rawgetp(L, LUA_REGISTRYINDEX, &pll_errmt_key);
	result = lua_rawequal(L, -1, -2);
	lua_pop(L, 2);
	return result;
}

static void
pll_set_string_field(lua_State * L, const char *name, const char *value)
{
	if (value == NULL || value[0] == '\0')
		return;
	lua_pushstring(L, value);
	lua_setfield(L, -2, name);
}

static void
pll_set_int_field(lua_State * L, const char *name, int value)
{
	if (value <= 0)
		return;
	lua_pushinteger(L, value);
	lua_setfield(L, -2, name);
}

void
pll_push_edata(lua_State * L, ErrorData *edata)
{
	const char *sqlstate = unpack_sql_state(edata->sqlerrcode);
	const char *name = pll_condition_name(edata->sqlerrcode);
	char		classbuf[3];

	luaL_checkstack(L, 6, "out of stack space");
	lua_createtable(L, 0, 12);

	pll_set_string_field(L, "sqlstate", sqlstate);
	pll_set_string_field(L, "errcode", name ? name : sqlstate);
	pll_set_string_field(L, "category",
						 pll_error_class_name(edata->sqlerrcode, classbuf));
	pll_set_string_field(L, "severity", "error");
	lua_pushstring(L, edata->message ? edata->message : "");
	lua_setfield(L, -2, "message");
	pll_set_string_field(L, "detail", edata->detail);
	pll_set_string_field(L, "hint", edata->hint);
	pll_set_string_field(L, "context", edata->context);
	pll_set_string_field(L, "schema", edata->schema_name);
	pll_set_string_field(L, "table", edata->table_name);
	pll_set_string_field(L, "column", edata->column_name);
	pll_set_string_field(L, "datatype", edata->datatype_name);
	pll_set_string_field(L, "constraint", edata->constraint_name);
	pll_set_int_field(L, "position", edata->cursorpos);
	pll_set_int_field(L, "internal_position", edata->internalpos);
	pll_set_string_field(L, "internal_query", edata->internalquery);
	pll_set_string_field(L, "message_id", edata->message_id);
	pll_set_string_field(L, "pg_source_file", edata->filename);
	pll_set_int_field(L, "pg_source_line", edata->lineno);
	pll_set_string_field(L, "pg_source_function", edata->funcname);

	pll_push_errmt(L);
	lua_setmetatable(L, -2);
}

void
pll_throw_edata(lua_State * L, ErrorData *edata)
{
	pll_push_edata(L, edata);
	FreeErrorData(edata);
	lua_error(L);
	pg_unreachable();
}

/*
 * __tostring of error objects: the severity, SQLSTATE and message, then the
 * detail and hint.
 */
static int
pll_error_tostring(lua_State * L)
{
	int			n = 0;

	luaL_checktype(L, 1, LUA_TTABLE);
	luaL_checkstack(L, 12, "out of stack space");

	/*
	 * Push the pieces and join them with lua_concat, which can be done with
	 * anything else on the stack, unlike a luaL_Buffer.  The fields that are
	 * optional have their label pushed first and are dropped, along with it,
	 * if they are missing.
	 */
	lua_pushliteral(L, "ERROR: ");
	n++;

	lua_getfield(L, 1, "sqlstate");
	if (lua_isstring(L, -1))
	{
		lua_pushliteral(L, " ");
		n += 2;
	}
	else
		lua_pop(L, 1);

	lua_getfield(L, 1, "message");
	if (lua_isstring(L, -1))
		n++;
	else
		lua_pop(L, 1);

	lua_pushliteral(L, "\nDETAIL: ");
	lua_getfield(L, 1, "detail");
	if (lua_isstring(L, -1))
		n += 2;
	else
		lua_pop(L, 2);

	lua_pushliteral(L, "\nHINT: ");
	lua_getfield(L, 1, "hint");
	if (lua_isstring(L, -1))
		n += 2;
	else
		lua_pop(L, 2);

	lua_concat(L, n);
	return 1;
}

/* ------------------------------------------------------------------------
 * Turning Lua errors into PostgreSQL errors
 * ------------------------------------------------------------------------
 */

/* A string field of the table at idx, as a copy, or NULL; no metamethods */
static char *
pll_get_string_field(lua_State * L, int idx, const char *name)
{
	char	   *result = NULL;

	idx = lua_absindex(L, idx);
	lua_pushstring(L, name);
	if (lua_rawget(L, idx) == LUA_TSTRING)
		result = pstrdup(lua_tostring(L, -1));
	lua_pop(L, 1);
	return result;
}

static int
pll_get_int_field(lua_State * L, int idx, const char *name)
{
	int			result = 0;

	idx = lua_absindex(L, idx);
	lua_pushstring(L, name);
	if (lua_rawget(L, idx) == LUA_TNUMBER)
		result = (int) lua_tointeger(L, -1);
	lua_pop(L, 1);
	return result;
}

/*
 * Lua C function: fill in a pll_errinfo (argument 1) from an arbitrary error
 * value (argument 2).  Runs in protected mode so that nothing here can raise
 * an error into PostgreSQL code.
 */
static int
pll_extract_cfunc(lua_State * L)
{
	pll_errinfo *ei = (pll_errinfo *) lua_touserdata(L, 1);
	char	   *sqlstate;

	luaL_checkstack(L, 8, "out of stack space");

	if (pll_is_error_object(L, 2))
	{
		ei->from_object = true;
		sqlstate = pll_get_string_field(L, 2, "sqlstate");
		ei->sqlerrcode = ERRCODE_INTERNAL_ERROR;
		if (sqlstate != NULL && strlen(sqlstate) == 5)
			ei->sqlerrcode = MAKE_SQLSTATE(sqlstate[0], sqlstate[1],
										   sqlstate[2], sqlstate[3],
										   sqlstate[4]);
		ei->message = pll_get_string_field(L, 2, "message");
		ei->detail = pll_get_string_field(L, 2, "detail");
		ei->hint = pll_get_string_field(L, 2, "hint");
		ei->context = pll_get_string_field(L, 2, "context");
		ei->schema = pll_get_string_field(L, 2, "schema");
		ei->table = pll_get_string_field(L, 2, "table");
		ei->column = pll_get_string_field(L, 2, "column");
		ei->datatype = pll_get_string_field(L, 2, "datatype");
		ei->constraint = pll_get_string_field(L, 2, "constraint");
		ei->internalquery = pll_get_string_field(L, 2, "internal_query");
		ei->file = pll_get_string_field(L, 2, "pg_source_file");
		ei->func = pll_get_string_field(L, 2, "pg_source_function");
		ei->cursorpos = pll_get_int_field(L, 2, "position");
		ei->internalpos = pll_get_int_field(L, 2, "internal_position");
		ei->line = pll_get_int_field(L, 2, "pg_source_line");
		return 0;
	}

	ei->sqlerrcode = ERRCODE_EXTERNAL_ROUTINE_EXCEPTION;
	if (lua_type(L, 2) == LUA_TSTRING || lua_type(L, 2) == LUA_TNUMBER)
		ei->message = pstrdup(lua_tostring(L, 2));
	else if (lua_type(L, 2) == LUA_TNIL || luaL_getmetafield(L, 2, "__tostring") == LUA_TNIL)
		ei->message = psprintf("(error object is a %s value)",
							   luaL_typename(L, 2));
	else
	{
		lua_pop(L, 1);
		ei->message = pstrdup(luaL_tolstring(L, 2, NULL));
	}
	return 0;
}

void
pll_rethrow(lua_State * L)
{
	pll_errinfo ei;
	ErrorData	edata;
	int			top = lua_gettop(L);

	memset(&ei, 0, sizeof(ei));
	ei.sqlerrcode = ERRCODE_EXTERNAL_ROUTINE_EXCEPTION;

	lua_pushcfunction(L, pll_extract_cfunc);
	lua_pushlightuserdata(L, &ei);
	lua_pushvalue(L, top);
	if (pll_pcall(L, 2, 0, 0) != LUA_OK)
		ei.message = pstrdup("could not convert a Lua error to a PostgreSQL error");
	lua_settop(L, top - 1);

	if (ei.message == NULL)
		ei.message = pstrdup("");

	/* messages that came from Lua code must be valid in the encoding */
	if (!ei.from_object)
	{
		pg_verify_mbstr(GetDatabaseEncoding(), ei.message,
						strlen(ei.message), false);
	}

	memset(&edata, 0, sizeof(edata));
	edata.elevel = ERROR;
	edata.sqlerrcode = ei.sqlerrcode;
	edata.message = ei.message;
	edata.detail = ei.detail;
	edata.hint = ei.hint;
	edata.context = ei.context;
	edata.schema_name = ei.schema;
	edata.table_name = ei.table;
	edata.column_name = ei.column;
	edata.datatype_name = ei.datatype;
	edata.constraint_name = ei.constraint;
	edata.internalquery = ei.internalquery;
	edata.cursorpos = ei.cursorpos;
	edata.internalpos = ei.internalpos;
	edata.filename = ei.file;
	edata.lineno = ei.line;
	edata.funcname = ei.func;

	ThrowErrorData(&edata);
	pg_unreachable();
}

void
pll_verify_encoding(lua_State * L, const char *s, size_t len)
{
	PLL_TRY();
	{
		pg_verify_mbstr(GetDatabaseEncoding(), s, (int) len, false);
	}
	PLL_CATCH(L);
}

/* ------------------------------------------------------------------------
 * pcall and friends
 * ------------------------------------------------------------------------
 */

/*
 * Errors that a script is not allowed to catch: those that report operator
 * intervention (query cancel, shutdown), and assertion failures.  The error
 * object is at idx.
 */
static bool
pll_error_is_uncatchable(lua_State * L, int idx)
{
	char	   *sqlstate;
	bool		result;

	if (!pll_is_error_object(L, idx))
		return false;
	idx = lua_absindex(L, idx);
	sqlstate = pll_get_string_field(L, idx, "sqlstate");
	if (sqlstate == NULL)
		return false;
	result = (strncmp(sqlstate, "57", 2) == 0 ||
			  strcmp(sqlstate, unpack_sql_state(ERRCODE_ASSERT_FAILURE)) == 0);
	pfree(sqlstate);
	return result;
}

/*
 * Common code of pcall and xpcall.  The stack is function, [handler,]
 * arguments; for xpcall, the handler is moved in front so the layout is
 * handler, function, arguments.  The call is made in a subtransaction.
 * Yielding out of the called function is not supported, since the
 * subtransaction would have to stay open while the coroutine is suspended.
 */
static int
pll_subxact_pcall(lua_State * L, bool use_handler)
{
	MemoryContext oldcxt = CurrentMemoryContext;
	ResourceOwner oldowner = CurrentResourceOwner;
	int			hbase = use_handler ? 1 : 0;
	int			nargs;
	int			status;

	luaL_checkany(L, 1);
	if (use_handler)
	{
		luaL_checktype(L, 2, LUA_TFUNCTION);
		lua_pushvalue(L, 2);
		lua_remove(L, 2);
		lua_insert(L, 1);
	}
	nargs = lua_gettop(L) - hbase - 1;

	/* before the first transaction, there can be no subtransaction */
	if (pll_interp_of(L)->initializing)
		status = pll_pcall(L, nargs, LUA_MULTRET, 0);
	else
	{
		PLL_TRY();
		{
			BeginInternalSubTransaction(NULL);
			MemoryContextSwitchTo(oldcxt);
		}
		PLL_CATCH(L);

		status = pll_pcall(L, nargs, LUA_MULTRET, 0);

		if (status == LUA_OK)
		{
			PLL_TRY();
			{
				ReleaseCurrentSubTransaction();
				MemoryContextSwitchTo(oldcxt);
				CurrentResourceOwner = oldowner;
			}
			PLL_CATCH(L);
		}
		else
		{
			PLL_TRY();
			{
				RollbackAndReleaseCurrentSubTransaction();
				MemoryContextSwitchTo(oldcxt);
				CurrentResourceOwner = oldowner;
			}
			PLL_CATCH(L);
		}
	}

	if (status == LUA_OK)
	{
		lua_pushboolean(L, 1);
		lua_insert(L, hbase + 1);
		return lua_gettop(L) - hbase;
	}

	/* the error value is on top of the stack */
	if (pll_error_is_uncatchable(L, -1))
		return lua_error(L);

	if (use_handler)
	{
		lua_pushvalue(L, 1);
		lua_insert(L, -2);
		if (pll_pcall(L, 1, 1, 0) != LUA_OK)
		{
			/* a SQL error in the handler goes to our caller */
			if (pll_is_error_object(L, -1))
				return lua_error(L);
			lua_pop(L, 1);
			lua_pushliteral(L, "error in error handling");
		}
	}
	lua_pushboolean(L, 0);
	lua_insert(L, -2);
	return 2;
}

static int
pll_lua_pcall(lua_State * L)
{
	return pll_subxact_pcall(L, false);
}

static int
pll_lua_xpcall(lua_State * L)
{
	return pll_subxact_pcall(L, true);
}

/* pcall without a subtransaction: only Lua errors are caught */
static int
pll_lua_lpcall(lua_State * L)
{
	int			status;

	luaL_checkany(L, 1);
	lua_pushboolean(L, 1);
	lua_insert(L, 1);
	status = pll_pcall(L, lua_gettop(L) - 2, LUA_MULTRET, 0);
	if (status == LUA_OK)
		return lua_gettop(L);

	if (pll_is_error_object(L, -1))
		return lua_error(L);

	lua_pushboolean(L, 0);
	lua_insert(L, -2);
	return 2;
}

/*
 * coroutine.resume that propagates PostgreSQL errors instead of returning
 * them, since they must not be caught without a subtransaction.
 */
static int
pll_lua_resume(lua_State * L)
{
	lua_State  *co = lua_tothread(L, 1);
	int			narg;
	int			nres;
	int			status;

	luaL_argexpected(L, co, 1, "coroutine");
	narg = lua_gettop(L) - 1;
	if (!lua_checkstack(co, narg))
	{
		lua_pushboolean(L, 0);
		lua_pushliteral(L, "too many arguments to resume");
		return 2;
	}
	lua_xmove(L, co, narg);
	status = pll_resume(co, L, narg, &nres);
	if (status == LUA_OK || status == LUA_YIELD)
	{
		if (!lua_checkstack(L, nres + 1))
		{
			lua_pop(co, nres);
			lua_pushboolean(L, 0);
			lua_pushliteral(L, "too many results to resume");
			return 2;
		}
		lua_xmove(co, L, nres);
		lua_pushboolean(L, 1);
		lua_rotate(L, -(nres + 1), 1);
		return nres + 1;
	}

	lua_xmove(co, L, 1);
	if (pll_is_error_object(L, -1))
		return lua_error(L);
	lua_pushboolean(L, 0);
	lua_insert(L, -2);
	return 2;
}

/* ------------------------------------------------------------------------
 * print and warn
 * ------------------------------------------------------------------------
 */

/* Report a message that has been verified; may raise a Lua error */
static void
pll_emit_message(lua_State * L, int elevel, const char *msg)
{
	PLL_TRY();
	{
		ereport(elevel,
				(errmsg_internal("%s", msg)));
	}
	PLL_CATCH(L);
}

static int
pll_lua_print(lua_State * L)
{
	int			n = lua_gettop(L);
	int			i;
	luaL_Buffer b;
	size_t		len;
	const char *s;

	luaL_buffinit(L, &b);
	for (i = 1; i <= n; i++)
	{
		/* the separator goes in before the value is pushed, not after */
		if (i > 1)
			luaL_addchar(&b, '\t');
		(void) luaL_tolstring(L, i, NULL);
		luaL_addvalue(&b);
	}
	luaL_pushresult(&b);
	s = lua_tolstring(L, -1, &len);
	pll_verify_encoding(L, s, len);
	pll_emit_message(L, pll_interp_of(L)->initializing ? LOG : INFO, s);
	return 0;
}

static int
pll_lua_warn(lua_State * L)
{
	int			n = lua_gettop(L);
	int			i;
	luaL_Buffer b;
	size_t		len;
	const char *s;

	luaL_checkstring(L, 1);
	for (i = 2; i <= n; i++)
		luaL_checkstring(L, i);

	/* control messages ("@on" and "@off") have no meaning here */
	if (n == 1 && lua_tostring(L, 1)[0] == '@')
		return 0;

	luaL_buffinit(L, &b);
	for (i = 1; i <= n; i++)
	{
		lua_pushvalue(L, i);
		luaL_addvalue(&b);
	}
	luaL_pushresult(&b);
	s = lua_tolstring(L, -1, &len);
	pll_verify_encoding(L, s, len);
	pll_emit_message(L, WARNING, s);
	return 0;
}

void
pll_install_error_functions(lua_State * L)
{
	lua_pushcfunction(L, pll_lua_print);
	lua_setglobal(L, "print");
	lua_pushcfunction(L, pll_lua_warn);
	lua_setglobal(L, "warn");
	lua_pushcfunction(L, pll_lua_pcall);
	lua_setglobal(L, "pcall");
	lua_pushcfunction(L, pll_lua_xpcall);
	lua_setglobal(L, "xpcall");
	lua_pushcfunction(L, pll_lua_lpcall);
	lua_setglobal(L, "lpcall");

	lua_getglobal(L, "coroutine");
	lua_pushcfunction(L, pll_lua_resume);
	lua_setfield(L, -2, "resume");
	lua_pop(L, 1);
}

/* ------------------------------------------------------------------------
 * The pl_lua.elog module
 * ------------------------------------------------------------------------
 */

static int
pll_level_by_name(lua_State * L, const char *name)
{
	const pll_level *l;

	for (l = pll_levels; l->name != NULL; l++)
	{
		if (strcmp(l->name, name) == 0)
			return l->elevel;
	}
	return luaL_error(L, "unknown message level \"%s\"", name);
}

/* A string argument that may be absent or nil, checked for its encoding */
static const char *
pll_opt_string(lua_State * L, int idx)
{
	const char *s;
	size_t		len;

	if (lua_isnoneornil(L, idx))
		return NULL;
	s = luaL_checklstring(L, idx, &len);
	pll_verify_encoding(L, s, len);
	return s;
}

/* Likewise for field 'name' of the table at tbl; leaves the value pushed */
static const char *
pll_field_string(lua_State * L, int tbl, const char *name)
{
	const char *s;
	size_t		len;

	lua_getfield(L, tbl, name);
	if (lua_isnil(L, -1))
		return NULL;
	s = lua_tolstring(L, -1, &len);
	if (s == NULL)
		luaL_error(L, "field \"%s\" must be a string", name);
	pll_verify_encoding(L, s, len);
	return s;
}

/*
 * Report a message.  The arguments, starting at 'first', are in one of the
 * forms (message), (sqlstate, message [, detail [, hint]]) or (table).
 */
static void
pll_do_elog(lua_State * L, int elevel, int first)
{
	pll_elog_args a;
	int			nargs = lua_gettop(L) - first + 1;
	const char *state = NULL;

	memset(&a, 0, sizeof(a));
	a.elevel = elevel;

	luaL_checkstack(L, 16, "out of stack space");

	if (nargs >= 1 && lua_type(L, first) == LUA_TTABLE)
	{
		state = pll_field_string(L, first, "sqlstate");
		a.message = pll_field_string(L, first, "message");
		a.detail = pll_field_string(L, first, "detail");
		a.hint = pll_field_string(L, first, "hint");
		a.table = pll_field_string(L, first, "table");
		a.column = pll_field_string(L, first, "column");
		a.datatype = pll_field_string(L, first, "datatype");
		a.constraint = pll_field_string(L, first, "constraint");
		a.schema = pll_field_string(L, first, "schema");
		if (a.message == NULL)
			a.message = "";
	}
	else if (nargs == 1)
		a.message = pll_opt_string(L, first);
	else if (nargs >= 2)
	{
		state = pll_opt_string(L, first);
		a.message = pll_opt_string(L, first + 1);
		a.detail = pll_opt_string(L, first + 2);
		a.hint = pll_opt_string(L, first + 3);
	}
	if (a.message == NULL)
		luaL_error(L, "a message is required");

	if (state != NULL)
		a.sqlerrcode = pll_sqlstate_parse(L, state);

	PLL_TRY();
	{
		ereport(a.elevel,
				(a.sqlerrcode != 0 ? errcode(a.sqlerrcode) : 0,
				 errmsg_internal("%s", a.message),
				 a.detail ? errdetail_internal("%s", a.detail) : 0,
				 a.hint ? errhint_internal("%s", a.hint) : 0,
				 a.schema ? err_generic_string(PG_DIAG_SCHEMA_NAME, a.schema) : 0,
				 a.table ? err_generic_string(PG_DIAG_TABLE_NAME, a.table) : 0,
				 a.column ? err_generic_string(PG_DIAG_COLUMN_NAME, a.column) : 0,
				 a.datatype ? err_generic_string(PG_DIAG_DATATYPE_NAME, a.datatype) : 0,
				 a.constraint ? err_generic_string(PG_DIAG_CONSTRAINT_NAME, a.constraint) : 0));
	}
	PLL_CATCH(L);
}

/* spi.elog(level, ...) */
static int
pll_elog_func(lua_State * L)
{
	const char *name = luaL_checkstring(L, 1);

	pll_do_elog(L, pll_level_by_name(L, name), 2);
	return 0;
}

/* a function for one level, which is upvalue 1 */
static int
pll_elog_level_func(lua_State * L)
{
	pll_do_elog(L, (int) lua_tointeger(L, lua_upvalueindex(1)), 1);
	return 0;
}

int
pll_open_elog(lua_State * L)
{
	static const char *const names[] = {"debug", "log", "info", "notice",
	"warning", "error", NULL};
	int			i;

	lua_createtable(L, 0, 8);
	lua_pushcfunction(L, pll_elog_func);
	lua_setfield(L, -2, "elog");
	for (i = 0; names[i] != NULL; i++)
	{
		lua_pushinteger(L, pll_level_by_name(L, names[i]));
		lua_pushcclosure(L, pll_elog_level_func, 1);
		lua_setfield(L, -2, names[i]);
	}
	return 1;
}

/* ------------------------------------------------------------------------
 * The pl_lua.error module
 * ------------------------------------------------------------------------
 */

/* error.type(e): "error" for error objects, nothing otherwise */
static int
pll_err_type(lua_State * L)
{
	if (!pll_is_error_object(L, 1))
		return 0;
	lua_pushliteral(L, "error");
	return 1;
}

/* errcode and category: the field named by upvalue 1 */
static int
pll_err_field(lua_State * L)
{
	if (!pll_is_error_object(L, 1))
		return 0;
	lua_pushvalue(L, lua_upvalueindex(1));
	lua_rawget(L, 1);
	return 1;
}

int
pll_open_error(lua_State * L)
{
	lua_createtable(L, 0, 3);
	lua_pushcfunction(L, pll_err_type);
	lua_setfield(L, -2, "type");
	lua_pushliteral(L, "errcode");
	lua_pushcclosure(L, pll_err_field, 1);
	lua_setfield(L, -2, "errcode");
	lua_pushliteral(L, "category");
	lua_pushcclosure(L, pll_err_field, 1);
	lua_setfield(L, -2, "category");
	return 1;
}
