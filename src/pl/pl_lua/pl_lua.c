/*-------------------------------------------------------------------------
 *
 * pl_lua.c
 *	  PL/Lua: handlers, configuration, interpreter management, and the
 *	  compilation and calling of functions.
 *
 * Structure of a call
 *
 * The call handler (a PostgreSQL frame) finds or creates the interpreter,
 * finds or compiles the function, connects to SPI, and then runs a Lua C
 * function with pll_pcall_rethrow().  That C function converts the
 * arguments, calls the Lua function, and converts the result; being inside
 * a Lua frame, it can use the Lua API freely, and Lua errors in it are caught
 * by the protected call and rethrown as PostgreSQL errors.  See the rules in
 * pl_lua_int.h for how PostgreSQL errors are kept from unwinding Lua frames.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <time.h>

#include "access/htup_details.h"
#include "access/xact.h"
#include "catalog/pg_language.h"
#include "catalog/pg_proc.h"
#include "catalog/pg_type.h"
#include "commands/event_trigger.h"
#include "commands/trigger.h"
#include "executor/executor.h"
#include "executor/spi.h"
#include "funcapi.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "nodes/miscnodes.h"
#include "nodes/parsenodes.h"
#include "tcop/tcopprot.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/syscache.h"

#include "pl_lua_int.h"

PG_MODULE_MAGIC_EXT(
					.name = "pl_lua",
					.version = PG_VERSION
);

/* Registry keys: only their addresses matter */
const char	pll_regkey_sandbox = 0;
const char	pll_regkey_typeinfos = 0;
const char	pll_regkey_trusted_mods = 0;
const char	pll_regkey_misc = 0;

/* GUC variables */
bool		pll_strict = false;
bool		pll_install_globals = true;
bool		pll_check_for_interrupts = true;
double		pll_extra_gc_multiplier = 0.0;
double		pll_extra_gc_threshold = 0.0;
char	   *pll_interpreter_reload_ident = NULL;

static char *pll_on_init = NULL;
static char *pll_on_trusted_init = NULL;
static char *pll_on_untrusted_init = NULL;
static char *pll_on_common_init = NULL;
static int	pll_prebuilt_interpreters = 1;

#define PLL_MAX_PREBUILT	10
#define PLL_HOOK_COUNT		1000

/*
 * A compiled function.  These live in the function table of an interpreter,
 * in the interpreter's memory context.  A function that is replaced while
 * calls to it are still active (a set-returning function in progress) is
 * unlinked from the table and freed when its last active call finishes.
 */
typedef struct pll_func
{
	Oid			fn_oid;
	TransactionId fn_xmin;
	ItemPointerData fn_tid;
	pll_interp *interp;
	char	   *name;
	Oid			langoid;
	bool		is_trigger;
	bool		is_evtrigger;
	bool		is_procedure;
	bool		retset;
	bool		readonly;		/* not volatile */
	bool		polymorphic;	/* resolve argument types at each call */
	bool		variadic_any;	/* final argument is VARIADIC "any" */
	int			nargs;			/* number of input arguments */
	Oid		   *argtypes;		/* declared types of input arguments */
	Oid			rettype;		/* declared result type */
	List	   *trftypes;
	int			ref;			/* registry reference to the Lua function */
	int			nactive;		/* calls in progress that need this */
	bool		obsolete;		/* replaced; free when nactive drops to 0 */
} pll_func;

/*
 * State of one call.  The innermost one is available through
 * pll_curcall, and used by the pll_call_xxx accessors.
 */
typedef struct pll_call
{
	pll_func   *func;
	FunctionCallInfo fcinfo;
	bool		atomic;
	int			nargs;			/* actual number of input arguments */
	Oid		   *argtypes;		/* actual types of the input arguments */
	Oid			rettype;		/* actual result type */
	TupleDesc	rettupdesc;		/* result descriptor, if composite and known */
	MemoryContext resultcxt;	/* where results are to be allocated */
	Datum		result;
	bool		isnull;
	/* inline code */
	const char *src;
	/* SRF progress */
	struct pll_srf *srf;
	int			nres;			/* values on the coroutine's stack */
	struct pll_call *prev;
} pll_call;

/*
 * State of a set-returning function in progress.  Allocated in
 * TopMemoryContext, and registered on a list so that it can be released if
 * the transaction ends without the executor shutting the function down.
 */
typedef struct pll_srf
{
	struct pll_srf *next;
	struct pll_srf *prev;
	pll_interp *interp;
	pll_func   *func;
	lua_State  *co;
	int			co_ref;
	bool		yielded;		/* has the coroutine produced a row? */
	bool		finished;
	ExprContext *econtext;
	pll_call	call;			/* persistent copy of the call state */
} pll_srf;

static pll_call *pll_curcall = NULL;
static pll_srf *pll_live_srfs = NULL;
static bool pll_xact_callback_registered = false;

/* interpreters: trusted ones keyed by role, the untrusted one by InvalidOid */
typedef struct pll_interp_entry
{
	Oid			roleid;
	pll_interp *interp;
} pll_interp_entry;

static HTAB *pll_interp_table = NULL;

/* interpreters built in the postmaster, with initialization stage 1 done */
static pll_interp *pll_prebuilt[PLL_MAX_PREBUILT];
static int	pll_nprebuilt = 0;
static bool pll_prebuilt_ready = false;

typedef struct pll_func_entry
{
	Oid			fn_oid;
	pll_func   *func;
} pll_func_entry;

/* arguments for the creation stages, passed to Lua C functions */
typedef struct pll_stage_args
{
	pll_interp *interp;
	bool		trusted;
} pll_stage_args;

/* arguments for compiling a function body */
typedef struct pll_compile_args
{
	pll_func   *func;
	const char *chunkname;
	const char *src;
	size_t		srclen;
	bool		parse_only;
	bool		syntax_error;	/* output: source did not parse */
	char	   *errmsg;			/* output: the message, palloc'd */
} pll_compile_args;

void		_PG_init(void);

PGDLLEXPORT Datum pl_lua_call_handler(PG_FUNCTION_ARGS);
PGDLLEXPORT Datum pl_lua_inline_handler(PG_FUNCTION_ARGS);
PGDLLEXPORT Datum pl_lua_validator(PG_FUNCTION_ARGS);
PGDLLEXPORT Datum pl_luau_call_handler(PG_FUNCTION_ARGS);
PGDLLEXPORT Datum pl_luau_inline_handler(PG_FUNCTION_ARGS);
PGDLLEXPORT Datum pl_luau_validator(PG_FUNCTION_ARGS);

PG_FUNCTION_INFO_V1(pl_lua_call_handler);
PG_FUNCTION_INFO_V1(pl_lua_inline_handler);
PG_FUNCTION_INFO_V1(pl_lua_validator);
PG_FUNCTION_INFO_V1(pl_luau_call_handler);
PG_FUNCTION_INFO_V1(pl_luau_inline_handler);
PG_FUNCTION_INFO_V1(pl_luau_validator);

static int	pll_panic(lua_State * L);
static void pll_warnf(void *ud, const char *msg, int tocont);
static void pll_hook(lua_State * L, lua_Debug * ar);
static int	pll_run_string(lua_State * L, const char *src, const char *name,
						   bool in_sandbox);
static int	pll_stage1_cfunc(lua_State * L);
static int	pll_stage2_cfunc(lua_State * L);
static pll_interp *pll_interp_new(bool trusted, Oid roleid, bool prebuild);
static void pll_interp_stage2(pll_interp *interp);
static void pll_interp_free(pll_interp *interp);
static pll_interp *pll_get_interp(bool trusted);
static void pll_build_prebuilt(void);
static void pll_discard_prebuilt(void);
static void pll_reload_assign_hook(const char *newval, void *extra);
static void pll_on_init_assign_hook(const char *newval, void *extra);
static void pll_prebuilt_count_assign_hook(int newval, void *extra);
static bool pll_is_lua_identifier(const char *s);
static char *pll_sanitize_name(const char *name);
static pll_func *pll_func_lookup(pll_interp *interp, FunctionCallInfo fcinfo,
								 bool atomic);
static pll_func *pll_func_compile(pll_interp *interp, Oid fn_oid,
								  HeapTuple procTup, bool is_trigger,
								  bool is_evtrigger);
static pll_func *pll_func_compile_connected(pll_interp *interp, Oid fn_oid,
											HeapTuple procTup, bool is_trigger,
											bool is_evtrigger, bool atomic);
static void pll_func_free(pll_func *func);
static void pll_func_release(pll_func *func);
static void pll_check_function_types(HeapTuple procTup, Form_pg_proc procStruct,
									 bool *is_trigger, bool *is_evtrigger);
static int	pll_compile_cfunc(lua_State * L);
static void pll_make_env(lua_State * L, pll_interp *interp, bool strict);
static int	pll_strict_index(lua_State * L);
static void pll_prepare_call(pll_call *call, pll_func *func,
							 FunctionCallInfo fcinfo, bool atomic);
static int	pll_call_cfunc(lua_State * L);
static int	pll_inline_cfunc(lua_State * L);
static int	pll_srf_start_cfunc(lua_State * L);
static int	pll_srf_row_cfunc(lua_State * L);
static void pll_convert_result(lua_State * L, pll_call *call, int idx, int nret);
static void pll_push_args(lua_State * L, pll_call *call);
static Datum pll_handle_call(PG_FUNCTION_ARGS, bool trusted);
static Datum pll_handle_srf(PG_FUNCTION_ARGS, pll_interp *interp,
							pll_func *func, bool atomic);
static void pll_srf_release(pll_srf *srf);
static void pll_srf_shutdown(Datum arg);
static void pll_xact_callback(XactEvent event, void *arg);
static void pll_after_call(pll_interp *interp);
static Datum pll_handle_inline(PG_FUNCTION_ARGS, bool trusted);
static Datum pll_handle_validator(PG_FUNCTION_ARGS, bool trusted);
static Oid *pll_resolve_argtypes(pll_func *func, FunctionCallInfo fcinfo,
								 int *nargs);
static void pll_rebuild_prebuilt_nofail(void);
static void pll_check_ident(pll_interp *interp);
static int	pll_set_ident_new_cfunc(lua_State * L);
static pll_func *pll_compile_function_common(pll_interp *interp, Oid fn_oid,
											 HeapTuple procTup,
											 bool is_trigger,
											 bool is_evtrigger,
											 bool parse_only);

/* ------------------------------------------------------------------------
 * Module initialization and configuration
 * ------------------------------------------------------------------------
 */

void
_PG_init(void)
{
	/* Be sure we do initialization only once (should be redundant now) */
	static bool inited = false;

	if (inited)
		return;

	DefineCustomStringVariable("pl_lua.on_init",
							   "Lua code to run when an interpreter is created, before specialization.",
							   NULL,
							   &pll_on_init,
							   NULL,
							   PGC_SUSET, 0,
							   NULL, pll_on_init_assign_hook, NULL);

	DefineCustomStringVariable("pl_lua.on_trusted_init",
							   "Lua code to run when a trusted interpreter is created.",
							   NULL,
							   &pll_on_trusted_init,
							   NULL,
							   PGC_SUSET, 0,
							   NULL, NULL, NULL);

	DefineCustomStringVariable("pl_lua.on_untrusted_init",
							   "Lua code to run when the untrusted interpreter is created.",
							   NULL,
							   &pll_on_untrusted_init,
							   NULL,
							   PGC_SUSET, 0,
							   NULL, NULL, NULL);

	DefineCustomStringVariable("pl_lua.on_common_init",
							   "Lua code to run at the end of the creation of every interpreter.",
							   NULL,
							   &pll_on_common_init,
							   NULL,
							   PGC_SUSET, 0,
							   NULL, NULL, NULL);

	DefineCustomBoolVariable("pl_lua.install_globals",
							 "Store the spi and pgtype modules in global variables.",
							 NULL,
							 &pll_install_globals,
							 true,
							 PGC_SUSET, 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pl_lua.strict",
							 "Raise an error when reading an undefined variable.",
							 NULL,
							 &pll_strict,
							 false,
							 PGC_USERSET, 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pl_lua.check_for_interrupts",
							 "Check for query cancel and other interrupts while Lua code runs.",
							 NULL,
							 &pll_check_for_interrupts,
							 true,
							 PGC_SUSET, 0,
							 NULL, NULL, NULL);

	DefineCustomIntVariable("pl_lua.prebuilt_interpreters",
							"Number of interpreters to build in the postmaster.",
							"Only effective if pl_lua is in shared_preload_libraries.",
							&pll_prebuilt_interpreters,
							1, 0, PLL_MAX_PREBUILT,
							PGC_SIGHUP, 0,
							NULL, pll_prebuilt_count_assign_hook, NULL);

	DefineCustomStringVariable("pl_lua.interpreter_reload_ident",
							   "Changing this causes prebuilt interpreters to be rebuilt.",
							   NULL,
							   &pll_interpreter_reload_ident,
							   "",
							   PGC_SIGHUP, 0,
							   NULL, pll_reload_assign_hook, NULL);

	DefineCustomRealVariable("pl_lua.extra_gc_multiplier",
							 "Effort of the extra garbage collection for memory allocated outside Lua.",
							 NULL,
							 &pll_extra_gc_multiplier,
							 0.0, 0.0, 1000000.0,
							 PGC_USERSET, 0,
							 NULL, NULL, NULL);

	DefineCustomRealVariable("pl_lua.extra_gc_threshold",
							 "Amount of memory allocated outside Lua that triggers extra garbage collection.",
							 NULL,
							 &pll_extra_gc_threshold,
							 0.0, 0.0, (double) INT_MAX,
							 PGC_USERSET, GUC_UNIT_KB,
							 NULL, NULL, NULL);

	MarkGUCPrefixReserved("pl_lua");

	inited = true;

	/*
	 * In the postmaster, build interpreters up to the first stage.  Where
	 * server processes are started afresh instead of being forked (Windows),
	 * they could not inherit them, so don't.
	 */
#ifndef EXEC_BACKEND
	if (process_shared_preload_libraries_in_progress)
	{
		pll_prebuilt_ready = true;
		pll_build_prebuilt();
	}
#endif
}

/* ------------------------------------------------------------------------
 * Interpreters
 * ------------------------------------------------------------------------
 */

static int
pll_panic(lua_State * L)
{
	const char *msg = lua_tostring(L, -1);

	ereport(FATAL,
			(errcode(ERRCODE_INTERNAL_ERROR),
			 errmsg("unprotected error in PL/Lua: %s",
					msg ? msg : "(not a string)")));
	return 0;					/* not reached */
}

/*
 * The Lua warning function is used by the garbage collector to report
 * errors in finalizers, and by nothing else (the Lua function warn() is
 * replaced).  Report such warnings in the log.  This must not raise errors.
 */
static void
pll_warnf(void *ud, const char *msg, int tocont)
{
	static StringInfoData buf;
	static bool inited = false;

	if (!inited)
	{
		MemoryContext oldcxt = MemoryContextSwitchTo(TopMemoryContext);

		initStringInfo(&buf);
		MemoryContextSwitchTo(oldcxt);
		inited = true;
	}

	appendStringInfoString(&buf, msg);
	if (tocont)
		return;

	if (buf.len > 0 && buf.data[0] != '@')
		ereport(LOG,
				(errmsg_internal("PL/Lua warning: %s", buf.data)));
	resetStringInfo(&buf);
}

static void
pll_hook(lua_State * L, lua_Debug * ar)
{
	PLL_TRY();
	{
		CHECK_FOR_INTERRUPTS();
	}
	PLL_CATCH(L);
}

/*
 * Compile and run a string of Lua code, from a configuration setting.  For
 * on_common_init in a trusted interpreter, the code runs with the sandbox as
 * its environment.  Raises Lua errors.
 */
static int
pll_run_string(lua_State * L, const char *src, const char *name, bool in_sandbox)
{
	if (src == NULL || src[0] == '\0')
		return 0;

	if (luaL_loadbufferx(L, src, strlen(src), name, "t") != LUA_OK)
		return lua_error(L);

	if (in_sandbox)
	{
		lua_rawgetp(L, LUA_REGISTRYINDEX, PLL_REGKEY_SANDBOX);
		if (lua_setupvalue(L, -2, 1) == NULL)
			lua_pop(L, 1);
	}

	lua_call(L, 0, 0);
	return 0;
}

/*
 * Stage 1 of the creation of an interpreter: the standard libraries, the
 * replacements of functions that need them, and the pl_lua.trusted and
 * pl_lua.paths modules; then pl_lua.on_init.  This can be done in the
 * postmaster.
 */
static int
pll_stage1_cfunc(lua_State * L)
{
	pll_interp *interp = pll_interp_of(L);

	luaL_openlibs(L);
	pll_install_error_functions(L);

	luaL_requiref(L, PLL_MOD_TRUSTED, pll_open_trusted, 0);
	lua_pop(L, 1);
	luaL_requiref(L, PLL_MOD_PATHS, pll_open_paths, 0);
	lua_pop(L, 1);

	interp->initializing = true;
	pll_run_string(L, pll_on_init, "=pl_lua.on_init", false);
	interp->initializing = false;

	return 0;
}

/*
 * Protect the metatables of the objects that PL/Lua makes (typeinfo, datum,
 * statement, cursor and trigger objects) from being read, by giving them a
 * __metatable field, so that getmetatable() returns false.  A metatable that
 * could be reached could also be changed: replacing its __gc field, for
 * instance, would make Lua run the user's code when the objects are
 * collected, and stop our own finalizers freeing plans and cursors, which
 * trusted code is not to do.  The metatables registered under names that
 * start with pl_lua. are the ones, except for the marker metatables that
 * jsonb conversion sets on plain tables, which belong to the user's tables.
 */
static void
pll_protect_metatables(lua_State * L)
{
	luaL_checkstack(L, 6, "out of stack space");
	lua_pushnil(L);
	while (lua_next(L, LUA_REGISTRYINDEX))
	{
		/* the key is at -2 and the value at -1 */
		if (lua_type(L, -2) == LUA_TSTRING && lua_istable(L, -1))
		{
			const char *name = lua_tostring(L, -2);

			if (strncmp(name, "pl_lua.", 7) == 0 &&
				strcmp(name, "pl_lua.jsonb.object") != 0 &&
				strcmp(name, "pl_lua.jsonb.array") != 0)
			{
				lua_pushliteral(L, "__metatable");
				if (lua_rawget(L, -2) == LUA_TNIL)
				{
					lua_pop(L, 1);
					lua_pushliteral(L, "__metatable");
					lua_pushboolean(L, 0);
					lua_rawset(L, -3);
				}
				else
					lua_pop(L, 1);
			}
		}
		lua_pop(L, 1);
	}
}

/*
 * Stage 2: the remaining modules, global variables, the sandbox, and the
 * rest of the initialization strings.
 */
static int
pll_stage2_cfunc(lua_State * L)
{
	pll_stage_args *args = (pll_stage_args *) lua_touserdata(L, 1);
	pll_interp *interp = args->interp;
	bool		trusted = args->trusted;
	time_t		now = time(NULL);

	luaL_requiref(L, PLL_MOD_ERROR, pll_open_error, 0);
	lua_pop(L, 1);
	luaL_requiref(L, PLL_MOD_ELOG, pll_open_elog, 0);
	lua_pop(L, 1);
	luaL_requiref(L, PLL_MOD_PGTYPE, pll_open_pgtype, 0);
	if (pll_install_globals)
		lua_setglobal(L, "pgtype");
	else
		lua_pop(L, 1);
	luaL_requiref(L, PLL_MOD_NUMERIC, pll_open_numeric, 0);
	lua_pop(L, 1);
	luaL_requiref(L, PLL_MOD_JSONB, pll_open_jsonb, 0);
	lua_pop(L, 1);
	luaL_requiref(L, PLL_MOD_TIME, pll_open_time, 0);
	lua_pop(L, 1);
	luaL_requiref(L, PLL_MOD_SPI, pll_open_spi, 0);
	if (pll_install_globals)
		lua_setglobal(L, "spi");
	else
		lua_pop(L, 1);

	pll_protect_metatables(L);

	if (trusted && pll_install_globals)
	{
		lua_getfield(L, LUA_REGISTRYINDEX, LUA_LOADED_TABLE);
		lua_getfield(L, -1, PLL_MOD_TRUSTED);
		lua_setglobal(L, "trusted");
		lua_pop(L, 1);
	}

	lua_pushstring(L, PL_LUA_VERSION_STRING);
	lua_setglobal(L, "_PLVERSION");
	lua_pushstring(L, PL_LUA_VERSION_STRING);
	lua_setglobal(L, "_PLREVISION");
	lua_pushstring(L, PG_VERSION);
	lua_setglobal(L, "_PG_VERSION");
	lua_pushinteger(L, PG_VERSION_NUM);
	lua_setglobal(L, "_PG_VERSION_NUM");
	lua_pushstring(L, interp->ident);
	lua_setglobal(L, "_PL_IDENT");
	lua_pushinteger(L, (lua_Integer) now);
	lua_setglobal(L, "_PL_LOAD_TIME");

	if (trusted)
	{
		pll_sandbox_build(L, pll_install_globals);
		pll_remove_string_dump(L);
	}

	if (pll_check_for_interrupts)
		lua_sethook(L, pll_hook, LUA_MASKCOUNT, PLL_HOOK_COUNT);

	if (trusted)
		pll_run_string(L, pll_on_trusted_init, "=pl_lua.on_trusted_init", false);
	else
		pll_run_string(L, pll_on_untrusted_init, "=pl_lua.on_untrusted_init", false);
	pll_run_string(L, pll_on_common_init, "=pl_lua.on_common_init", trusted);

	(void) interp;
	return 0;
}

/*
 * Make an interpreter and run stage 1.  If 'prebuild', this is for the
 * postmaster, and the interpreter is not yet assigned to a role.
 */
static pll_interp *
pll_interp_new(bool trusted, Oid roleid, bool prebuild)
{
	MemoryContext oldcxt;
	MemoryContext mcxt;
	pll_interp *interp;
	lua_State  *L;
	HASHCTL		ctl;

	mcxt = AllocSetContextCreate(TopMemoryContext, "PL/Lua interpreter",
								 ALLOCSET_DEFAULT_SIZES);
	oldcxt = MemoryContextSwitchTo(mcxt);
	interp = palloc0(sizeof(pll_interp));
	interp->mcxt = mcxt;
	interp->trusted = trusted;
	interp->roleid = roleid;
	interp->ident = pstrdup(pll_interpreter_reload_ident ?
							pll_interpreter_reload_ident : "");

	ctl.keysize = sizeof(Oid);
	ctl.entrysize = sizeof(pll_func_entry);
	ctl.hcxt = mcxt;
	interp->funcs = hash_create("PL/Lua functions", 32, &ctl,
								HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	MemoryContextSwitchTo(oldcxt);

	L = luaL_newstate();
	if (L == NULL)
	{
		MemoryContextDelete(mcxt);
		ereport(ERROR,
				(errcode(ERRCODE_OUT_OF_MEMORY),
				 errmsg("could not create a Lua interpreter: out of memory")));
	}
	lua_atpanic(L, pll_panic);
	lua_setwarnf(L, pll_warnf, NULL);
	interp->L = L;
	*(pll_interp **) lua_getextraspace(L) = interp;

	PG_TRY();
	{
		/* the extra space of the main state is copied to new coroutines */
		lua_pushcfunction(L, pll_stage1_cfunc);
		pll_pcall_rethrow(L, 0, 0);
	}
	PG_CATCH();
	{
		pll_interp_free(interp);
		PG_RE_THROW();
	}
	PG_END_TRY();

	(void) prebuild;
	return interp;
}

/* Run stage 2 for an interpreter that has been through stage 1 */
static void
pll_interp_stage2(pll_interp *interp)
{
	lua_State  *L = interp->L;
	pll_stage_args args;

	args.interp = interp;
	args.trusted = interp->trusted;

	lua_pushcfunction(L, pll_stage2_cfunc);
	lua_pushlightuserdata(L, &args);
	pll_pcall_rethrow(L, 1, 0);
}

static void
pll_interp_free(pll_interp *interp)
{
	lua_close(interp->L);
	MemoryContextDelete(interp->mcxt);
}

/*
 * Build the prebuilt interpreters, in the postmaster.  Errors here prevent
 * the server from starting, as documented.
 */
static void
pll_build_prebuilt(void)
{
	int			i;

	for (i = 0; i < pll_prebuilt_interpreters; i++)
		pll_prebuilt[pll_nprebuilt++] = pll_interp_new(false, InvalidOid, true);
}

static void
pll_discard_prebuilt(void)
{
	while (pll_nprebuilt > 0)
		pll_interp_free(pll_prebuilt[--pll_nprebuilt]);
}

/*
 * Configuration reload in the postmaster: rebuild the interpreters if the
 * ident changed, or if on_init changed and an ident is set.  A failure to
 * rebuild is reported but doesn't stop the server.
 */
static void
pll_rebuild_prebuilt_nofail(void)
{
	MemoryContext cxt;

	if (!pll_prebuilt_ready || IsUnderPostmaster)
		return;

	cxt = CurrentMemoryContext;
	PG_TRY();
	{
		pll_discard_prebuilt();
		pll_build_prebuilt();
	}
	PG_CATCH();
	{
		ErrorData  *edata;

		/* CopyErrorData must not be called in ErrorContext */
		MemoryContextSwitchTo(cxt);
		edata = CopyErrorData();
		FlushErrorState();
		ereport(WARNING,
				(errmsg("could not rebuild PL/Lua interpreters: %s",
						edata->message)));
		FreeErrorData(edata);
	}
	PG_END_TRY();
}

/*
 * Assign hooks for the settings that rebuild the prebuilt interpreters.  A
 * hook is called before the setting's variable is assigned, so make the new
 * value the current one while the interpreters are rebuilt.  The variables of
 * the other settings changed in the same reload are either already assigned,
 * or are handled by their own hook, which rebuilds the interpreters again.
 */
static void
pll_reload_assign_hook(const char *newval, void *extra)
{
	char	   *save = pll_interpreter_reload_ident;

	if (!pll_prebuilt_ready || IsUnderPostmaster)
		return;
	if (strcmp(newval ? newval : "", save ? save : "") == 0)
		return;

	pll_interpreter_reload_ident = (char *) newval;
	pll_rebuild_prebuilt_nofail();
	pll_interpreter_reload_ident = save;
}

static void
pll_on_init_assign_hook(const char *newval, void *extra)
{
	char	   *save = pll_on_init;

	if (!pll_prebuilt_ready || IsUnderPostmaster)
		return;
	if (pll_interpreter_reload_ident == NULL ||
		pll_interpreter_reload_ident[0] == '\0')
		return;
	if (strcmp(newval ? newval : "", save ? save : "") == 0)
		return;

	pll_on_init = (char *) newval;
	pll_rebuild_prebuilt_nofail();
	pll_on_init = save;
}

/* Likewise for the number of prebuilt interpreters */
static void
pll_prebuilt_count_assign_hook(int newval, void *extra)
{
	int			save = pll_prebuilt_interpreters;

	if (!pll_prebuilt_ready || IsUnderPostmaster || newval == save)
		return;

	pll_prebuilt_interpreters = newval;
	pll_rebuild_prebuilt_nofail();
	pll_prebuilt_interpreters = save;
}

/*
 * Find or create the interpreter for the current context: the untrusted one
 * or the trusted one for the current user.
 */
static pll_interp *
pll_get_interp(bool trusted)
{
	Oid			roleid = trusted ? GetUserId() : InvalidOid;
	pll_interp_entry *entry;
	bool		found;
	pll_interp *interp;

	if (pll_interp_table == NULL)
	{
		HASHCTL		ctl;

		ctl.keysize = sizeof(Oid);
		ctl.entrysize = sizeof(pll_interp_entry);
		ctl.hcxt = TopMemoryContext;
		pll_interp_table = hash_create("PL/Lua interpreters", 16, &ctl,
									   HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	}

	entry = hash_search(pll_interp_table, &roleid, HASH_FIND, &found);
	if (found)
	{
		pll_check_ident(entry->interp);
		return entry->interp;
	}

	if (pll_nprebuilt > 0)
	{
		interp = pll_prebuilt[--pll_nprebuilt];
		interp->trusted = trusted;
		interp->roleid = roleid;
	}
	else
		interp = pll_interp_new(trusted, roleid, false);

	PG_TRY();
	{
		pll_interp_stage2(interp);
	}
	PG_CATCH();
	{
		pll_interp_free(interp);
		PG_RE_THROW();
	}
	PG_END_TRY();

	entry = hash_search(pll_interp_table, &roleid, HASH_ENTER, &found);
	entry->interp = interp;
	return interp;
}

/*
 * Lua C function: store the string argument 2 as _PL_IDENT_NEW, in the
 * global environment, and in the sandbox of a trusted interpreter.
 */
static int
pll_set_ident_new_cfunc(lua_State * L)
{
	pll_interp *interp = pll_interp_of(L);

	lua_pushvalue(L, 2);
	lua_setglobal(L, "_PL_IDENT_NEW");
	if (interp->trusted)
	{
		lua_rawgetp(L, LUA_REGISTRYINDEX, PLL_REGKEY_SANDBOX);
		lua_pushvalue(L, 2);
		lua_setfield(L, -2, "_PL_IDENT_NEW");
	}
	return 0;
}

/*
 * If the reload ident has changed since the interpreter was made (it was
 * changed in postgresql.conf), tell the interpreter's code about it.
 */
static void
pll_check_ident(pll_interp *interp)
{
	const char *current = pll_interpreter_reload_ident ?
		pll_interpreter_reload_ident : "";

	if (strcmp(current, interp->ident) == 0)
		return;

	pfree(interp->ident);
	interp->ident = MemoryContextStrdup(interp->mcxt, current);

	lua_pushstring(interp->L, current);
	pl_lua_cpcall(interp->L, pll_set_ident_new_cfunc, NULL, 1);
}

void
pll_note_external_alloc(lua_State * L, size_t bytes)
{
	pll_interp_of(L)->extra_alloc += bytes;
}

/* After a call: do the extra garbage collection, if configured */
static void
pll_after_call(pll_interp *interp)
{
	if (pll_extra_gc_multiplier <= 0.0)
		return;
	if (interp->extra_alloc < (uint64) (pll_extra_gc_threshold * 1024.0))
		return;

	if (pll_extra_gc_multiplier >= 1000000.0)
		lua_gc(interp->L, LUA_GCCOLLECT);
	else
		lua_gc(interp->L, LUA_GCSTEP,
			   (int) Min((double) (interp->extra_alloc / 1024) *
						 pll_extra_gc_multiplier, (double) INT_MAX));
	interp->extra_alloc = 0;
}

/* ------------------------------------------------------------------------
 * Exports for transform modules
 * ------------------------------------------------------------------------
 */

void
pl_lua_cpcall(lua_State * L, lua_CFunction func, void *arg, int nvalues)
{
	lua_pushcfunction(L, func);
	lua_pushlightuserdata(L, arg);
	if (nvalues > 0)
	{
		/* rotate the function and argument below the values */
		lua_rotate(L, lua_gettop(L) - nvalues - 1, 2);
	}
	pll_pcall_rethrow(L, 1 + nvalues, 0);
}

void
pl_lua_pg_call(lua_State * L, void (*fn) (void *arg), void *arg)
{
	PLL_TRY();
	{
		fn(arg);
	}
	PLL_CATCH(L);
}

/* ------------------------------------------------------------------------
 * Accessors for the current call
 * ------------------------------------------------------------------------
 */

bool
pll_in_call(void)
{
	return pll_curcall != NULL;
}

bool
pll_call_readonly(void)
{
	return pll_curcall != NULL && pll_curcall->func->readonly;
}

bool
pll_call_atomic(void)
{
	return pll_curcall == NULL || pll_curcall->atomic;
}

List *
pll_call_trftypes(void)
{
	return pll_curcall ? pll_curcall->func->trftypes : NIL;
}

const char *
pll_call_name(void)
{
	return pll_curcall ? pll_curcall->func->name : "(none)";
}

Oid
pll_call_langoid(void)
{
	return pll_curcall ? pll_curcall->func->langoid : InvalidOid;
}

void
pll_call_push_argtype(lua_State * L, int argno)
{
	pll_call   *call = pll_curcall;

	if (call == NULL)
		lua_pushnil(L);
	else if (argno == 0)
	{
		if (OidIsValid(call->rettype) && call->rettype != VOIDOID)
		{
			(void) pll_typeinfo_push(L, call->rettype, -1);
		}
		else
			lua_pushnil(L);
	}
	else if (argno >= 1 && argno <= call->nargs &&
			 OidIsValid(call->argtypes[argno - 1]))
		(void) pll_typeinfo_push(L, call->argtypes[argno - 1], -1);
	else
		lua_pushnil(L);
}

/* ------------------------------------------------------------------------
 * Compiling functions
 * ------------------------------------------------------------------------
 */

static const char *const pll_lua_keywords[] = {
	"and", "break", "do", "else", "elseif", "end", "false", "for",
	"function", "goto", "if", "in", "local", "nil", "not", "or",
	"repeat", "return", "then", "true", "until", "while", NULL
};

static bool
pll_is_lua_identifier(const char *s)
{
	const char *p;
	int			i;

	if (s == NULL || s[0] == '\0')
		return false;
	if (!(isalpha((unsigned char) s[0]) || s[0] == '_') ||
		IS_HIGHBIT_SET(s[0]))
		return false;
	for (p = s + 1; *p; p++)
	{
		if (IS_HIGHBIT_SET(*p) || !(isalnum((unsigned char) *p) || *p == '_'))
			return false;
	}
	for (i = 0; pll_lua_keywords[i]; i++)
	{
		if (strcmp(s, pll_lua_keywords[i]) == 0)
			return false;
	}
	return true;
}

/* A valid Lua identifier derived from a function name */
static char *
pll_sanitize_name(const char *name)
{
	StringInfoData buf;
	const char *p;
	char	   *result;

	initStringInfo(&buf);
	for (p = name; *p; p++)
	{
		if (!IS_HIGHBIT_SET(*p) && (isalnum((unsigned char) *p) || *p == '_'))
			appendStringInfoChar(&buf, *p);
		else
			appendStringInfoChar(&buf, '_');
	}
	if (buf.len == 0 || isdigit((unsigned char) buf.data[0]))
		result = psprintf("_%s", buf.data);
	else
		result = pstrdup(buf.data);
	if (!pll_is_lua_identifier(result))
		result = psprintf("%s_", result);
	pfree(buf.data);
	return result;
}

/*
 * Make the environment table for a function: empty, with a metatable whose
 * __index is the global environment (the sandbox, in a trusted interpreter).
 * In strict mode, __index is a function that complains about missing names.
 * Pushes the table.
 */
static void
pll_make_env(lua_State * L, pll_interp *interp, bool strict)
{
	lua_newtable(L);			/* env */
	lua_newtable(L);			/* its metatable */
	if (interp->trusted)
		lua_rawgetp(L, LUA_REGISTRYINDEX, PLL_REGKEY_SANDBOX);
	else
		lua_pushglobaltable(L);
	if (strict)
	{
		lua_pushcclosure(L, pll_strict_index, 1);
		lua_setfield(L, -2, "__index");
	}
	else
		lua_setfield(L, -2, "__index");
	lua_setmetatable(L, -2);
}

static int
pll_strict_index(lua_State * L)
{
	/* upvalue 1 is the parent; arguments are the table and the key */
	lua_pushvalue(L, 2);
	lua_gettable(L, lua_upvalueindex(1));
	if (lua_isnil(L, -1))
	{
		if (lua_type(L, 2) == LUA_TSTRING)
			return luaL_error(L, "variable \"%s\" is not declared",
							  lua_tostring(L, 2));
		return luaL_error(L, "variable is not declared");
	}
	return 1;
}

/*
 * Lua C function: compile a function body (or just parse it) according to a
 * pll_compile_args, and leave a reference to the resulting function in
 * func->ref.  Source that does not parse is reported through args instead of
 * raising an error.
 */
static int
pll_compile_cfunc(lua_State * L)
{
	pll_compile_args *args = (pll_compile_args *) lua_touserdata(L, 1);
	pll_interp *interp = pll_interp_of(L);
	pll_func   *func = args->func;
	int			status;

	status = luaL_loadbufferx(L, args->src, args->srclen, args->chunkname, "t");
	if (status != LUA_OK)
	{
		const char *msg = lua_tostring(L, -1);

		args->syntax_error = true;
		args->errmsg = MemoryContextStrdup(CurrentMemoryContext,
										   msg ? msg : "syntax error");
		return 0;
	}

	if (args->parse_only)
		return 0;

	/* set the chunk's environment, then run it with the environment as arg */
	pll_make_env(L, interp, pll_strict);	/* chunk, env */
	lua_pushvalue(L, -1);
	if (lua_setupvalue(L, -3, 1) == NULL)
		lua_pop(L, 1);
	/* stack: chunk, env */
	lua_call(L, 1, 1);			/* the chunk returns the function */
	if (!lua_isfunction(L, -1))
		return luaL_error(L, "function body did not yield a function");
	func->ref = luaL_ref(L, LUA_REGISTRYINDEX);
	return 0;
}

/*
 * Check the argument and result types of a function, and decide whether it
 * is a trigger or event trigger function.
 */
static void
pll_check_function_types(HeapTuple procTup, Form_pg_proc procStruct,
						 bool *is_trigger, bool *is_evtrigger)
{
	Oid			rettype = procStruct->prorettype;
	Oid		   *argtypes;
	char	  **argnames;
	char	   *argmodes;
	int			numargs;
	int			i;

	*is_trigger = (rettype == TRIGGEROID);
	*is_evtrigger = (rettype == EVENT_TRIGGEROID);

	if (!*is_trigger && !*is_evtrigger)
	{
		if (rettype != VOIDOID && rettype != RECORDOID &&
			get_typtype(rettype) == TYPTYPE_PSEUDO &&
			!IsPolymorphicType(rettype))
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("PL/Lua functions cannot return type %s",
							format_type_be(rettype))));
	}

	numargs = get_func_arg_info(procTup, &argtypes, &argnames, &argmodes);
	for (i = 0; i < numargs; i++)
	{
		Oid			argtype = argtypes[i];

		if (argtype != RECORDOID && argtype != ANYOID &&
			get_typtype(argtype) == TYPTYPE_PSEUDO &&
			!IsPolymorphicType(argtype) && argtype != CSTRINGOID)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("PL/Lua functions cannot accept type %s",
							format_type_be(argtype))));
	}

	if ((*is_trigger || *is_evtrigger) && procStruct->pronargs != 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_FUNCTION_DEFINITION),
				 errmsg("trigger functions cannot have declared arguments"),
				 errhint("The arguments of the trigger are available as the extra arguments of the Lua function.")));
}

/*
 * Compile a function.  procTup is the pg_proc tuple.  If parse_only, only
 * check that the body parses, and return NULL.
 */
static pll_func *
pll_compile_function_common(pll_interp *interp, Oid fn_oid, HeapTuple procTup,
							bool is_trigger, bool is_evtrigger,
							bool parse_only)
{
	Form_pg_proc procStruct = (Form_pg_proc) GETSTRUCT(procTup);
	MemoryContext oldcxt = CurrentMemoryContext;
	MemoryContext tmpcxt;
	pll_func   *func;
	pll_compile_args cargs;
	Datum		prosrc;
	char	   *body;
	char	   *luaname;
	StringInfoData src;
	Oid		   *argtypes;
	char	  **argnames;
	char	   *argmodes;
	int			numargs;
	int			i;
	bool		saw_unnamed = false;
	bool		need_varargs = false;
	int			ninputs = 0;
	Oid		   *inputtypes;
	Oid		   *trftypes;
	int			ntrf;
	bool		first = true;

	/* all temporary allocations go in a context we can delete */
	tmpcxt = AllocSetContextCreate(CurrentMemoryContext, "PL/Lua compile",
								   ALLOCSET_SMALL_SIZES);
	MemoryContextSwitchTo(tmpcxt);

	prosrc = SysCacheGetAttrNotNull(PROCOID, procTup, Anum_pg_proc_prosrc);
	body = TextDatumGetCString(prosrc);
	luaname = pll_sanitize_name(NameStr(procStruct->proname));

	numargs = get_func_arg_info(procTup, &argtypes, &argnames, &argmodes);
	inputtypes = palloc(sizeof(Oid) * Max(numargs, 1));

	initStringInfo(&src);
	appendStringInfo(&src, "local self = (...) local function %s(", luaname);

	if (is_trigger)
		appendStringInfoString(&src, "trigger, old, new, ...");
	else if (is_evtrigger)
		appendStringInfoString(&src, "trigger");
	else
	{
		for (i = 0; i < numargs; i++)
		{
			char		mode = argmodes ? argmodes[i] : PROARGMODE_IN;
			const char *aname = argnames ? argnames[i] : NULL;
			bool		named = (aname != NULL && aname[0] != '\0');

			if (mode != PROARGMODE_IN && mode != PROARGMODE_INOUT &&
				mode != PROARGMODE_VARIADIC)
				continue;

			inputtypes[ninputs++] = argtypes[i];

			if (mode == PROARGMODE_VARIADIC && argtypes[i] == ANYOID)
			{
				if (named)
					ereport(ERROR,
							(errcode(ERRCODE_INVALID_FUNCTION_DEFINITION),
							 errmsg("a VARIADIC \"any\" argument of a PL/Lua function cannot be named")));
				saw_unnamed = true;
				need_varargs = true;
				continue;
			}

			if (!named)
			{
				saw_unnamed = true;
				need_varargs = true;
				continue;
			}

			if (saw_unnamed)
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_FUNCTION_DEFINITION),
						 errmsg("named argument \"%s\" follows an unnamed argument", aname),
						 errdetail("In a PL/Lua function, named arguments must precede unnamed ones.")));

			if (!pll_is_lua_identifier(aname))
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_FUNCTION_DEFINITION),
						 errmsg("argument name \"%s\" is not a valid Lua identifier", aname)));

			appendStringInfo(&src, "%s%s", first ? "" : ", ", aname);
			first = false;
		}
		if (need_varargs)
			appendStringInfo(&src, "%s...", first ? "" : ", ");
	}

	/* the body starts on line 1 of the chunk, so line numbers match */
	appendStringInfo(&src, ") %s\nend return %s", body, luaname);

	/* gather what we need to keep */
	MemoryContextSwitchTo(oldcxt);

	if (parse_only)
		func = NULL;
	else
	{
		MemoryContext fcxt = interp->mcxt;

		MemoryContextSwitchTo(fcxt);
		func = palloc0(sizeof(pll_func));
		func->fn_oid = fn_oid;
		func->fn_xmin = HeapTupleHeaderGetRawXmin(procTup->t_data);
		func->fn_tid = procTup->t_self;
		func->interp = interp;
		func->name = pstrdup(NameStr(procStruct->proname));
		func->langoid = procStruct->prolang;
		func->is_trigger = is_trigger;
		func->is_evtrigger = is_evtrigger;
		func->is_procedure = (procStruct->prokind == PROKIND_PROCEDURE);
		func->retset = procStruct->proretset;
		func->readonly = (procStruct->provolatile != PROVOLATILE_VOLATILE);
		func->nargs = ninputs;
		func->argtypes = palloc(sizeof(Oid) * Max(ninputs, 1));
		memcpy(func->argtypes, inputtypes, sizeof(Oid) * ninputs);
		func->rettype = procStruct->prorettype;
		func->ref = LUA_NOREF;
		func->polymorphic = IsPolymorphicType(procStruct->prorettype);
		for (i = 0; i < ninputs; i++)
		{
			if (IsPolymorphicType(inputtypes[i]) || inputtypes[i] == ANYOID)
				func->polymorphic = true;
			if (inputtypes[i] == ANYOID)
				func->variadic_any = true;	/* refined below */
		}
		if (func->variadic_any && numargs > 0)
		{
			/* is it the VARIADIC "any" one, i.e. the last input? */
			func->variadic_any = (inputtypes[ninputs - 1] == ANYOID &&
								  (argmodes == NULL ? false :
								   argmodes[numargs - 1] == PROARGMODE_VARIADIC));
		}
		ntrf = get_func_trftypes(procTup, &trftypes);
		func->trftypes = NIL;
		for (i = 0; i < ntrf; i++)
			func->trftypes = lappend_oid(func->trftypes, trftypes[i]);
		MemoryContextSwitchTo(oldcxt);
	}

	/* now compile in Lua */
	MemoryContextSwitchTo(tmpcxt);
	cargs.func = func;
	cargs.chunkname = pstrdup(NameStr(procStruct->proname));
	cargs.src = src.data;
	cargs.srclen = src.len;
	cargs.parse_only = parse_only;
	cargs.syntax_error = false;
	cargs.errmsg = NULL;

	PG_TRY();
	{
		pl_lua_cpcall(interp->L, pll_compile_cfunc, &cargs, 0);
	}
	PG_CATCH();
	{
		MemoryContextSwitchTo(oldcxt);
		if (func)
			pll_func_free(func);
		PG_RE_THROW();
	}
	PG_END_TRY();

	if (cargs.syntax_error)
	{
		char	   *msg = pstrdup(cargs.errmsg);

		MemoryContextSwitchTo(oldcxt);
		if (func)
			pll_func_free(func);
		ereport(ERROR,
				(errcode(ERRCODE_SYNTAX_ERROR),
				 errmsg("%s", msg)));
	}

	MemoryContextSwitchTo(oldcxt);
	MemoryContextDelete(tmpcxt);
	return func;
}

static pll_func *
pll_func_compile(pll_interp *interp, Oid fn_oid, HeapTuple procTup,
				 bool is_trigger, bool is_evtrigger)
{
	return pll_compile_function_common(interp, fn_oid, procTup, is_trigger,
									   is_evtrigger, false);
}

/*
 * Compile a function.  Initialization code in the function body can use SPI,
 * so run the compilation connected to SPI, with a call in progress that has
 * no arguments.
 */
static pll_func *
pll_func_compile_connected(pll_interp *interp, Oid fn_oid, HeapTuple procTup,
						   bool is_trigger, bool is_evtrigger, bool atomic)
{
	pll_call	call;
	pll_func	dummy;
	pll_call   *volatile save_call = pll_curcall;
	pll_func   *volatile func = NULL;

	memset(&dummy, 0, sizeof(dummy));
	dummy.interp = interp;
	dummy.name = "(compiling)";
	dummy.rettype = VOIDOID;
	dummy.ref = LUA_NOREF;
	memset(&call, 0, sizeof(call));
	call.func = &dummy;
	call.atomic = atomic;
	call.rettype = VOIDOID;
	call.resultcxt = CurrentMemoryContext;

	PG_TRY();
	{
		pll_curcall = &call;
		SPI_connect_ext(atomic ? 0 : SPI_OPT_NONATOMIC);
		func = pll_func_compile(interp, fn_oid, procTup, is_trigger,
								is_evtrigger);
		if (SPI_finish() != SPI_OK_FINISH)
			elog(ERROR, "SPI_finish failed");
	}
	PG_FINALLY();
	{
		pll_curcall = save_call;
	}
	PG_END_TRY();

	return func;
}

static void
pll_func_free(pll_func *func)
{
	if (func->ref != LUA_NOREF && func->ref != LUA_REFNIL)
		luaL_unref(func->interp->L, LUA_REGISTRYINDEX, func->ref);
	pfree(func->name);
	pfree(func->argtypes);
	list_free(func->trftypes);
	pfree(func);
}

/* An active call is done with the function */
static void
pll_func_release(pll_func *func)
{
	Assert(func->nactive > 0);
	if (--func->nactive == 0 && func->obsolete)
		pll_func_free(func);
}

/*
 * Find the compiled form of the function being called, compiling it if need
 * be.  The pg_proc tuple's xmin and tid are used to detect redefinition.
 */
static pll_func *
pll_func_lookup(pll_interp *interp, FunctionCallInfo fcinfo, bool atomic)
{
	Oid			fn_oid = fcinfo->flinfo->fn_oid;
	HeapTuple	procTup;
	Form_pg_proc procStruct;
	pll_func_entry *entry;
	bool		found;
	bool		is_trigger;
	bool		is_evtrigger;
	pll_func   *func;

	procTup = SearchSysCache1(PROCOID, ObjectIdGetDatum(fn_oid));
	if (!HeapTupleIsValid(procTup))
		elog(ERROR, "cache lookup failed for function %u", fn_oid);
	procStruct = (Form_pg_proc) GETSTRUCT(procTup);

	entry = hash_search(interp->funcs, &fn_oid, HASH_FIND, &found);
	if (found)
	{
		func = entry->func;
		if (func->fn_xmin == HeapTupleHeaderGetRawXmin(procTup->t_data) &&
			ItemPointerEquals(&func->fn_tid, &procTup->t_self))
		{
			ReleaseSysCache(procTup);
			return func;
		}
		/* replaced: unlink it */
		hash_search(interp->funcs, &fn_oid, HASH_REMOVE, NULL);
		if (func->nactive > 0)
			func->obsolete = true;
		else
			pll_func_free(func);
	}

	PG_TRY();
	{
		pll_check_function_types(procTup, procStruct, &is_trigger,
								 &is_evtrigger);
		func = pll_func_compile_connected(interp, fn_oid, procTup,
										  is_trigger, is_evtrigger, atomic);
	}
	PG_FINALLY();
	{
		ReleaseSysCache(procTup);
	}
	PG_END_TRY();

	entry = hash_search(interp->funcs, &fn_oid, HASH_ENTER, &found);
	entry->func = func;
	return func;
}

/* ------------------------------------------------------------------------
 * Calling functions
 * ------------------------------------------------------------------------
 */

/*
 * Work out the actual argument types of a call.  Returns an array in
 * CurrentMemoryContext, and sets *nargs.
 */
static Oid *
pll_resolve_argtypes(pll_func *func, FunctionCallInfo fcinfo, int *nargs)
{
	int			n = func->nargs;
	Oid		   *types;
	int			i;

	if (func->variadic_any)
		n = fcinfo->nargs;

	types = palloc(sizeof(Oid) * Max(n, 1));
	for (i = 0; i < n; i++)
	{
		Oid			declared = (i < func->nargs) ? func->argtypes[i] :
			func->argtypes[func->nargs - 1];

		if (func->polymorphic && fcinfo->flinfo != NULL &&
			(IsPolymorphicType(declared) || declared == ANYOID))
		{
			Oid			actual = get_fn_expr_argtype(fcinfo->flinfo, i);

			if (!OidIsValid(actual))
				ereport(ERROR,
						(errcode(ERRCODE_DATATYPE_MISMATCH),
						 errmsg("could not determine actual type of argument declared %s",
								format_type_be(declared))));
			types[i] = actual;
		}
		else
			types[i] = declared;
	}
	*nargs = n;
	return types;
}

static void
pll_prepare_call(pll_call *call, pll_func *func, FunctionCallInfo fcinfo,
				 bool atomic)
{
	memset(call, 0, sizeof(pll_call));
	call->func = func;
	call->fcinfo = fcinfo;
	call->atomic = atomic;
	call->resultcxt = CurrentMemoryContext;

	if (func->is_trigger || func->is_evtrigger)
	{
		call->nargs = 0;
		call->argtypes = NULL;
		call->rettype = func->rettype;
		return;
	}

	call->argtypes = pll_resolve_argtypes(func, fcinfo, &call->nargs);

	call->rettype = func->rettype;
	if (func->rettype == RECORDOID || IsPolymorphicType(func->rettype) ||
		func->is_procedure || get_typtype(func->rettype) == TYPTYPE_COMPOSITE)
	{
		TypeFuncClass tfc;
		Oid			rettype;
		TupleDesc	tupdesc;

		if (func->is_procedure && func->rettype != RECORDOID)
			tfc = TYPEFUNC_SCALAR;
		else
			tfc = get_call_result_type(fcinfo, &rettype, &tupdesc);

		switch (tfc)
		{
			case TYPEFUNC_COMPOSITE:
			case TYPEFUNC_COMPOSITE_DOMAIN:
				call->rettype = rettype;
				call->rettupdesc = CreateTupleDescCopy(tupdesc);
				if (call->rettupdesc->tdtypeid == RECORDOID &&
					call->rettupdesc->tdtypmod < 0)
					BlessTupleDesc(call->rettupdesc);
				break;
			case TYPEFUNC_SCALAR:
				if (OidIsValid(func->rettype) && !func->is_procedure)
					call->rettype = rettype;
				break;
			case TYPEFUNC_RECORD:
				{
					ReturnSetInfo *rsi = (ReturnSetInfo *) fcinfo->resultinfo;

					if (rsi && IsA(rsi, ReturnSetInfo) && rsi->expectedDesc)
					{
						call->rettupdesc = CreateTupleDescCopy(rsi->expectedDesc);
						BlessTupleDesc(call->rettupdesc);
						call->rettype = RECORDOID;
					}
					else
						ereport(ERROR,
								(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
								 errmsg("function returning record called in context that cannot accept type record")));
				}
				break;
			default:
				break;
		}
	}
}

/* Push the arguments of the call; the function is already pushed */
static void
pll_push_args(lua_State * L, pll_call *call)
{
	FunctionCallInfo fcinfo = call->fcinfo;
	int			i;

	luaL_checkstack(L, call->nargs + 8, "too many arguments");

	for (i = 0; i < call->nargs; i++)
	{
		if (fcinfo->args[i].isnull)
			lua_pushnil(L);
		else
		{
			pll_typeinfo *ti = pll_typeinfo_push(L, call->argtypes[i], -1);

			pll_value_push(L, fcinfo->args[i].value, false, ti);
			lua_remove(L, -2);	/* the typeinfo */
		}
	}
}

/*
 * Convert 'nret' result values starting at stack index idx to the result of
 * the call, in call->result and call->isnull, allocated in call->resultcxt.
 */
static void
pll_convert_result(lua_State * L, pll_call *call, int idx, int nret)
{
	pll_func   *func = call->func;
	MemoryContext oldcxt;
	pll_typeinfo *ti;
	bool		isnull;

	call->result = (Datum) 0;
	call->isnull = true;

	if (!call->rettupdesc)
	{
		if (call->rettype == VOIDOID || func->is_trigger || func->is_evtrigger)
			return;
		if (nret == 0)
			return;

		ti = pll_typeinfo_push(L, call->rettype, -1);
		oldcxt = MemoryContextSwitchTo(call->resultcxt);
		if (ti->kind == PLL_KIND_ARRAY && nret != 1)
		{
			/* several values make an array */
			call->result = pll_array_from_values(L, idx, nret, ti);
			isnull = false;
		}
		else if (ti->kind == PLL_KIND_JSONB && ti->typoid == JSONBOID &&
				 nret >= 2 && lua_istable(L, idx + 1))
		{
			/* a jsonb result may be accompanied by conversion options */
			call->result = pll_jsonb_todatum(L, idx, idx + 1);
			isnull = false;
		}
		else
			call->result = pll_value_todatum(L, idx, ti, &isnull);
		MemoryContextSwitchTo(oldcxt);
		lua_pop(L, 1);			/* the typeinfo */
		call->isnull = isnull;
		return;
	}

	/* composite result */
	if (nret == 0)
		return;

	if (!(nret == 1 &&
		  (lua_istable(L, idx) || pll_datum_test(L, idx) != NULL ||
		   lua_isnil(L, idx))))
	{
		int			ncols = 0;
		int			i;

		for (i = 0; i < call->rettupdesc->natts; i++)
		{
			if (!TupleDescCompactAttr(call->rettupdesc, i)->attisdropped)
				ncols++;
		}

		if (nret > ncols)
			luaL_error(L, "too many values returned: the result type has %d columns, but %d values were returned",
					   ncols, nret);
		/* the values must be contiguous, so pad before pushing anything */
		luaL_checkstack(L, ncols + 4, "too many columns");
		while (nret < ncols)
		{
			lua_pushnil(L);
			nret++;
		}
	}

	ti = pll_typeinfo_push(L, call->rettupdesc->tdtypeid,
						   call->rettupdesc->tdtypmod);
	oldcxt = MemoryContextSwitchTo(call->resultcxt);
	if (nret == 1 &&
		(lua_istable(L, idx) || pll_datum_test(L, idx) != NULL ||
		 lua_isnil(L, idx)))
	{
		call->result = pll_value_todatum(L, idx, ti, &isnull);
		call->isnull = isnull;
	}
	else
	{
		call->result = pll_values_tocomposite(L, idx, nret, ti);
		call->isnull = false;
	}
	MemoryContextSwitchTo(oldcxt);
	lua_pop(L, 1);				/* the typeinfo */
}

/*
 * Lua C function: the body of a call to an ordinary (non-SRF) function or
 * trigger.  Argument 1 is a light userdata pointing to the pll_call.
 */
static int
pll_call_cfunc(lua_State * L)
{
	pll_call   *call = (pll_call *) lua_touserdata(L, 1);
	pll_func   *func = call->func;
	int			base;
	int			nargs;
	int			nret;
	int			trigidx = 0;

	luaL_checkstack(L, 8, "stack overflow");
	lua_settop(L, 0);

	lua_rawgeti(L, LUA_REGISTRYINDEX, func->ref);

	if (func->is_trigger)
	{
		TriggerData *tdata = (TriggerData *) call->fcinfo->context;

		nargs = pll_trigger_push_args(L, tdata);
		/* keep a copy of the trigger object below the function */
		lua_pushvalue(L, 2);
		lua_insert(L, 1);
		trigidx = 1;
		base = 1;
	}
	else if (func->is_evtrigger)
	{
		nargs = pll_evtrigger_push_args(L, (EventTriggerData *) call->fcinfo->context);
		base = 0;
	}
	else
	{
		pll_push_args(L, call);
		nargs = call->nargs;
		base = 0;
	}

	lua_call(L, nargs, LUA_MULTRET);
	nret = lua_gettop(L) - base;

	if (func->is_trigger)
	{
		HeapTuple	rettup;
		MemoryContext oldcxt = MemoryContextSwitchTo(call->resultcxt);

		rettup = pll_trigger_result(L, (TriggerData *) call->fcinfo->context,
									trigidx, nret);
		MemoryContextSwitchTo(oldcxt);
		call->result = PointerGetDatum(rettup);
		call->isnull = false;
	}
	else
		pll_convert_result(L, call, base + 1, nret);

	return 0;
}

static Datum
pll_handle_call(PG_FUNCTION_ARGS, bool trusted)
{
	pll_interp *interp;
	pll_func   *func;
	pll_call	call;
	pll_call   *volatile save_call = pll_curcall;
	bool		atomic = true;
	Datum		retval;
	lua_State  *L;
	int			top;

	interp = pll_get_interp(trusted);
	L = interp->L;

	if (fcinfo->context && IsA(fcinfo->context, CallContext))
		atomic = castNode(CallContext, fcinfo->context)->atomic;

	func = pll_func_lookup(interp, fcinfo, atomic);

	if (func->is_trigger != CALLED_AS_TRIGGER(fcinfo) ||
		func->is_evtrigger != CALLED_AS_EVENT_TRIGGER(fcinfo))
	{
		if (func->is_trigger)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("trigger functions can only be called as triggers")));
		if (func->is_evtrigger)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("event trigger functions can only be called as event triggers")));
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("cannot call a function of this type directly")));
	}

	if (func->retset)
		return pll_handle_srf(fcinfo, interp, func, atomic);

	pll_prepare_call(&call, func, fcinfo, atomic);
	top = lua_gettop(L);

	PG_TRY();
	{
		pll_curcall = &call;
		func->nactive++;

		SPI_connect_ext(atomic ? 0 : SPI_OPT_NONATOMIC);

		lua_pushcfunction(L, pll_call_cfunc);
		lua_pushlightuserdata(L, &call);
		pll_pcall_rethrow(L, 1, 0);

		if (SPI_finish() != SPI_OK_FINISH)
			elog(ERROR, "SPI_finish failed");
	}
	PG_FINALLY();
	{
		pll_curcall = save_call;
		pll_func_release(func);
		lua_settop(L, top);
	}
	PG_END_TRY();

	pll_after_call(interp);

	retval = call.result;
	if (call.isnull)
		fcinfo->isnull = true;
	else
		fcinfo->isnull = false;

	return retval;
}

/* ------------------------------------------------------------------------
 * Set-returning functions
 * ------------------------------------------------------------------------
 */

static void
pll_xact_callback(XactEvent event, void *arg)
{
	pll_srf    *srf;

	switch (event)
	{
		case XACT_EVENT_COMMIT:
		case XACT_EVENT_PARALLEL_COMMIT:
		case XACT_EVENT_ABORT:
		case XACT_EVENT_PARALLEL_ABORT:
			break;
		default:
			return;
	}

	/* Anything still live belongs to a query that will not be finished */
	while ((srf = pll_live_srfs) != NULL)
		pll_srf_release(srf);
}

/*
 * Release a set-returning function's resources.  Does not run to-be-closed
 * variables (see pll_srf_shutdown for that).
 */
static void
pll_srf_release(pll_srf *srf)
{
	if (srf->next)
		srf->next->prev = srf->prev;
	if (srf->prev)
		srf->prev->next = srf->next;
	else
		pll_live_srfs = srf->next;

	if (srf->co_ref != LUA_NOREF)
		luaL_unref(srf->interp->L, LUA_REGISTRYINDEX, srf->co_ref);
	if (srf->func)
		pll_func_release(srf->func);
	pfree(srf);
}

/*
 * ExprContext callback: the query is done with the function.  If it has not
 * finished (a LIMIT, for example), close the coroutine so that its pending
 * to-be-closed variables are closed.
 */
static void
pll_srf_shutdown(Datum arg)
{
	pll_srf    *srf = (pll_srf *) DatumGetPointer(arg);
	pll_call   *volatile save_call = pll_curcall;

	if (!srf->finished && srf->co)
	{
		pll_curcall = &srf->call;
		/* errors are ignored: the status is returned */
		(void) lua_closethread(srf->co, srf->interp->L);
		pll_curcall = save_call;
	}
	pll_srf_release(srf);
}

/* Lua C function: start a set-returning function's coroutine */
static int
pll_srf_start_cfunc(lua_State * L)
{
	pll_srf    *srf = (pll_srf *) lua_touserdata(L, 1);
	pll_call   *call = &srf->call;
	lua_State  *co;

	luaL_checkstack(L, call->nargs + 8, "too many arguments");
	lua_settop(L, 0);

	co = lua_newthread(L);
	srf->co = co;
	srf->co_ref = luaL_ref(L, LUA_REGISTRYINDEX);	/* pops the thread */

	lua_rawgeti(L, LUA_REGISTRYINDEX, srf->func->ref);
	pll_push_args(L, call);
	lua_xmove(L, co, 1 + call->nargs);
	return 0;
}

/*
 * Lua C function: convert the call->nres values on the top of the
 * coroutine's stack to a result row.
 */
static int
pll_srf_row_cfunc(lua_State * L)
{
	pll_srf    *srf = (pll_srf *) lua_touserdata(L, 1);
	pll_call   *call = &srf->call;
	int			nres = call->nres;

	lua_settop(L, 0);
	luaL_checkstack(L, nres + 8, "too many values");
	if (nres > 0)
		lua_xmove(srf->co, L, nres);
	pll_convert_result(L, call, 1, nres);
	return 0;
}

static Datum
pll_handle_srf(PG_FUNCTION_ARGS, pll_interp *interp, pll_func *func,
			   bool atomic)
{
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	FuncCallContext *funcctx;
	pll_srf    *srf;
	pll_call   *volatile save_call = pll_curcall;
	lua_State  *L = interp->L;
	int			top = lua_gettop(L);
	int			status;
	int			nres = 0;
	Datum		result = (Datum) 0;
	bool		isnull = true;
	bool		done = false;
	MemoryContext callcxt = CurrentMemoryContext;

	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo) ||
		(rsinfo->allowedModes & SFRM_ValuePerCall) == 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("set-valued function called in context that cannot accept a set")));

	if (SRF_IS_FIRSTCALL())
	{
		funcctx = SRF_FIRSTCALL_INIT();

		if (!pll_xact_callback_registered)
		{
			RegisterXactCallback(pll_xact_callback, NULL);
			pll_xact_callback_registered = true;
		}

		srf = MemoryContextAllocZero(TopMemoryContext, sizeof(pll_srf));
		srf->interp = interp;
		srf->func = func;
		srf->co_ref = LUA_NOREF;
		srf->econtext = rsinfo->econtext;
		srf->next = pll_live_srfs;
		if (pll_live_srfs)
			pll_live_srfs->prev = srf;
		pll_live_srfs = srf;
		func->nactive++;
		funcctx->user_fctx = srf;

		/* argument types etc. must live as long as the SRF does */
		{
			MemoryContext oldcxt = MemoryContextSwitchTo(TopMemoryContext);

			pll_prepare_call(&srf->call, func, fcinfo, atomic);
			MemoryContextSwitchTo(oldcxt);
		}
		srf->call.srf = srf;
		srf->call.resultcxt = callcxt;
		RegisterExprContextCallback(rsinfo->econtext, pll_srf_shutdown,
									PointerGetDatum(srf));

		PG_TRY();
		{
			pll_curcall = &srf->call;
			SPI_connect_ext(atomic ? 0 : SPI_OPT_NONATOMIC);
			lua_pushcfunction(L, pll_srf_start_cfunc);
			lua_pushlightuserdata(L, srf);
			pll_pcall_rethrow(L, 1, 0);
			if (SPI_finish() != SPI_OK_FINISH)
				elog(ERROR, "SPI_finish failed");
		}
		PG_FINALLY();
		{
			pll_curcall = save_call;
			lua_settop(L, top);
		}
		PG_END_TRY();
	}

	funcctx = SRF_PERCALL_SETUP();
	srf = (pll_srf *) funcctx->user_fctx;
	srf->call.fcinfo = fcinfo;
	srf->call.resultcxt = callcxt;

	if (srf->finished)
	{
		done = true;
	}
	else
	{
		lua_State  *co = srf->co;
		int			nargs_resume = 0;

		/* the first resume passes the arguments already on the stack */
		if (!srf->yielded)
			nargs_resume = lua_gettop(co) - 1;
		else
			nargs_resume = 0;

		PG_TRY();
		{
			pll_curcall = &srf->call;
			SPI_connect_ext(atomic ? 0 : SPI_OPT_NONATOMIC);

			status = pll_resume(co, L, nargs_resume, &nres);
			if (status == LUA_YIELD || status == LUA_OK)
			{
				srf->call.nres = nres;
				if (status == LUA_OK)
				{
					/*
					 * The function returned.  If it has not yielded any rows,
					 * its return values (if any) are a single row.
					 */
					srf->finished = true;
					if (srf->yielded || nres == 0)
					{
						lua_settop(co, 0);
						done = true;
					}
				}
				if (!done)
				{
					lua_pushcfunction(L, pll_srf_row_cfunc);
					lua_pushlightuserdata(L, srf);
					pll_pcall_rethrow(L, 1, 0);
					lua_settop(co, 0);
					result = srf->call.result;
					isnull = srf->call.isnull;
					srf->yielded = true;
				}
			}
			else
			{
				/* error: the error value is on top of the coroutine's stack */
				srf->finished = true;
				lua_xmove(co, L, 1);
				pll_rethrow(L);
			}

			if (SPI_finish() != SPI_OK_FINISH)
				elog(ERROR, "SPI_finish failed");
		}
		PG_FINALLY();
		{
			pll_curcall = save_call;
			lua_settop(L, top);
		}
		PG_END_TRY();

		pll_after_call(interp);
	}

	if (done)
	{
		/* the set is complete: release the state and the callback */
		UnregisterExprContextCallback(srf->econtext, pll_srf_shutdown,
									  PointerGetDatum(srf));
		pll_srf_release(srf);
		funcctx->user_fctx = NULL;
		SRF_RETURN_DONE(funcctx);
	}

	rsinfo->isDone = ExprMultipleResult;
	fcinfo->isnull = isnull;
	return result;
}

/* ------------------------------------------------------------------------
 * Handlers
 * ------------------------------------------------------------------------
 */

/* Lua C function: run an inline code block */
static int
pll_inline_cfunc(lua_State * L)
{
	pll_call   *call = (pll_call *) lua_touserdata(L, 1);
	pll_interp *interp = pll_interp_of(L);
	int			status;

	lua_settop(L, 0);

	status = luaL_loadbufferx(L, call->src, strlen(call->src),
							  "DO-block", "t");
	if (status != LUA_OK)
	{
		/* rethrow as a syntax error: handled by the caller via errcode */
		return lua_error(L);
	}

	pll_make_env(L, interp, pll_strict);
	lua_pushvalue(L, -1);
	if (lua_setupvalue(L, 1, 1) == NULL)
		lua_pop(L, 1);
	lua_call(L, 1, 0);
	return 0;
}

static Datum
pll_handle_inline(PG_FUNCTION_ARGS, bool trusted)
{
	InlineCodeBlock *codeblock = (InlineCodeBlock *) PG_GETARG_POINTER(0);
	pll_interp *interp;
	pll_func	func;
	pll_call	call;
	pll_call   *volatile save_call = pll_curcall;

	LOCAL_FCINFO(fake_fcinfo, 0);
	FmgrInfo	flinfo;
	lua_State  *L;
	int			top;

	interp = pll_get_interp(trusted);
	L = interp->L;

	memset(&func, 0, sizeof(func));
	func.interp = interp;
	func.name = "inline_code_block";
	func.langoid = codeblock->langOid;
	func.ref = LUA_NOREF;
	func.rettype = VOIDOID;
	func.readonly = false;

	MemSet(fake_fcinfo, 0, SizeForFunctionCallInfo(0));
	MemSet(&flinfo, 0, sizeof(flinfo));
	fake_fcinfo->flinfo = &flinfo;
	flinfo.fn_oid = InvalidOid;
	flinfo.fn_mcxt = CurrentMemoryContext;

	memset(&call, 0, sizeof(call));
	call.func = &func;
	call.fcinfo = fake_fcinfo;
	call.atomic = codeblock->atomic;
	call.rettype = VOIDOID;
	call.resultcxt = CurrentMemoryContext;
	call.src = codeblock->source_text;

	top = lua_gettop(L);

	PG_TRY();
	{
		pll_curcall = &call;

		SPI_connect_ext(codeblock->atomic ? 0 : SPI_OPT_NONATOMIC);

		lua_pushcfunction(L, pll_inline_cfunc);
		lua_pushlightuserdata(L, &call);
		pll_pcall_rethrow(L, 1, 0);

		if (SPI_finish() != SPI_OK_FINISH)
			elog(ERROR, "SPI_finish failed");
	}
	PG_FINALLY();
	{
		pll_curcall = save_call;
		lua_settop(L, top);
	}
	PG_END_TRY();

	pll_after_call(interp);

	PG_RETURN_VOID();
}

/*
 * The validator checks the argument and result types and, if
 * check_function_bodies is on, that the body parses.
 */
static Datum
pll_handle_validator(PG_FUNCTION_ARGS, bool trusted)
{
	Oid			funcoid = PG_GETARG_OID(0);
	HeapTuple	procTup;
	Form_pg_proc procStruct;
	bool		is_trigger;
	bool		is_evtrigger;

	if (!CheckFunctionValidatorAccess(fcinfo->flinfo->fn_oid, funcoid))
		PG_RETURN_VOID();

	procTup = SearchSysCache1(PROCOID, ObjectIdGetDatum(funcoid));
	if (!HeapTupleIsValid(procTup))
		elog(ERROR, "cache lookup failed for function %u", funcoid);
	procStruct = (Form_pg_proc) GETSTRUCT(procTup);

	PG_TRY();
	{
		pll_check_function_types(procTup, procStruct, &is_trigger,
								 &is_evtrigger);

		if (check_function_bodies)
		{
			pll_interp *interp = pll_get_interp(trusted);

			(void) pll_compile_function_common(interp, funcoid, procTup,
											   is_trigger, is_evtrigger, true);
		}
	}
	PG_FINALLY();
	{
		ReleaseSysCache(procTup);
	}
	PG_END_TRY();

	PG_RETURN_VOID();
}

Datum
pl_lua_call_handler(PG_FUNCTION_ARGS)
{
	return pll_handle_call(fcinfo, true);
}

Datum
pl_luau_call_handler(PG_FUNCTION_ARGS)
{
	return pll_handle_call(fcinfo, false);
}

Datum
pl_lua_inline_handler(PG_FUNCTION_ARGS)
{
	return pll_handle_inline(fcinfo, true);
}

Datum
pl_luau_inline_handler(PG_FUNCTION_ARGS)
{
	return pll_handle_inline(fcinfo, false);
}

Datum
pl_lua_validator(PG_FUNCTION_ARGS)
{
	return pll_handle_validator(fcinfo, true);
}

Datum
pl_luau_validator(PG_FUNCTION_ARGS)
{
	return pll_handle_validator(fcinfo, false);
}
