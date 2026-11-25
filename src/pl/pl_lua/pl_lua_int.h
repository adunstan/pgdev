/*-------------------------------------------------------------------------
 *
 * pl_lua_int.h
 *	  Internal declarations shared by the PL/Lua source files.
 *
 * OVERVIEW
 *
 * PL/Lua embeds one Lua interpreter ("interp") per trusted role, plus one
 * for the untrusted language.  Everything that Lua code can see of the
 * database is implemented as C functions and userdata in the files listed
 * below.  Each module file exports a luaopen-style function, named in the
 * list, which is passed to luaL_requiref() at interpreter creation.
 *
 *	pl_lua.c		handlers, GUCs, interpreter management, compilation and
 *					calling of functions, set-returning functions
 *	pl_lua_error.c	error objects, pcall & friends, print, elog functions
 *	pl_lua_trusted.c  sandbox, pl_lua.trusted and pl_lua.paths modules
 *	pl_lua_datum.c	typeinfo objects, datum objects, SQL <-> Lua value
 *					conversion, composite/array/range support, pgtype module
 *	pl_lua_numeric.c  numeric datum objects, pl_lua.numeric module
 *	pl_lua_jsonb.c	jsonb datum objects, pl_lua.jsonb module
 *	pl_lua_time.c	date/time datum objects, pl_lua.time module
 *	pl_lua_spi.c	pl_lua.spi module: queries, statements, cursors, quoting,
 *					transaction control
 *	pl_lua_trigger.c  trigger and event trigger arguments and results
 *
 * ERROR HANDLING RULES
 *
 * PostgreSQL errors are implemented with sigsetjmp/siglongjmp, and so (in a
 * C build) are Lua errors.  The two must never be allowed to unwind through
 * each other's frames.  The rules are:
 *
 * 1. A Lua C function (lua_CFunction) must not let a PostgreSQL error escape
 *    from it.  Any code in it that might call ereport(ERROR) (or anything
 *    that can, such as palloc, SPI, type input functions...) must be inside
 *    PLL_TRY() ... PLL_CATCH(L), which converts the PostgreSQL error to a Lua
 *    error object and raises it with lua_error().
 *
 * 2. Nothing that can raise a Lua error may be called inside the body of
 *    PLL_TRY.  That includes lua_call, lua_gettable and friends (metamethods),
 *    lua_pushstring and the other functions that can allocate (memory errors),
 *    lua_error and luaL_error, luaL_check* and luaL_arg*.  Do Lua work
 *    outside of the PLL_TRY body, and keep the PG work inside it small and
 *    self-contained.  Variables that are modified inside the body and used
 *    after it must be volatile.  (When in doubt, the lua_pushxxx family is
 *    safe to use if the stack is known to have room (luaL_checkstack) and
 *    the allocation cannot fail in practice, but prefer to stay outside.)
 *
 * 3. To call Lua code from C (as opposed to from Lua), use pll_pcall(), which
 *    restores PG_exception_stack and error_context_stack if the call fails.
 *    Use pll_pcall_rethrow() to call it and turn Lua errors into PG errors.
 *
 * Since PG errors are always turned into Lua error objects at the C function
 * boundary, they are caught by Lua's own pcall machinery wherever a Lua
 * frame is in the way; the replacement pcall() (pl_lua_error.c) wraps the
 * call in a subtransaction so that this is safe.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * src/pl/pl_lua/pl_lua_int.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef PL_LUA_INT_H
#define PL_LUA_INT_H

#include "postgres.h"

#include "access/htup_details.h"
#include "access/tupdesc.h"
#include "commands/event_trigger.h"
#include "commands/trigger.h"
#include "executor/spi.h"
#include "fmgr.h"
#include "nodes/pg_list.h"
#include "utils/hsearch.h"
#include "utils/memutils.h"

#include "pl_lua.h"				/* Lua headers and version checks */

#define PL_LUA_VERSION_STRING	"1.0"

/*
 * Names of the modules, as seen by require().
 */
#define PLL_MOD_SPI			"pl_lua.spi"
#define PLL_MOD_PGTYPE		"pl_lua.pgtype"
#define PLL_MOD_ELOG		"pl_lua.elog"
#define PLL_MOD_ERROR		"pl_lua.error"
#define PLL_MOD_NUMERIC		"pl_lua.numeric"
#define PLL_MOD_JSONB		"pl_lua.jsonb"
#define PLL_MOD_TIME		"pl_lua.time"
#define PLL_MOD_TRUSTED		"pl_lua.trusted"
#define PLL_MOD_PATHS		"pl_lua.paths"

/*
 * Compatibility: lua_closethread is 5.4.6+, which we require.  Lua 5.5 keeps
 * it.  (Lua 5.5 also changed lua_newstate's signature; we use luaL_newstate
 * only.)
 */

/* ------------------------------------------------------------------------
 * Interpreters (pl_lua.c)
 * ------------------------------------------------------------------------
 */

typedef struct pll_interp
{
	lua_State  *L;				/* the main state */
	bool		trusted;		/* created for trusted use */
	Oid			roleid;			/* role it belongs to; InvalidOid if untrusted */
	MemoryContext mcxt;			/* lives as long as the interp does */
	HTAB	   *funcs;			/* compiled functions, private to pl_lua.c */
	bool		initializing;	/* running initialization strings: print goes
								 * to the server log, no call is active */
	uint64		extra_alloc;	/* see pll_note_external_alloc */
	char	   *ident;			/* value of the reload ident when created */
} pll_interp;

/*
 * Every lua_State (including coroutines) has a pointer to its pll_interp in
 * its "extra space"; coroutines inherit it from the main state.
 */
#define pll_interp_of(L)	(*(pll_interp **) lua_getextraspace(L))

/*
 * Registry keys.  Each is the address of a static variable, used as a light
 * userdata.  Use PLL_REGKEY_xxx with lua_rawgetp/lua_rawsetp.
 */
extern const char pll_regkey_sandbox;	/* trusted: the sandbox table */
extern const char pll_regkey_typeinfos; /* table: typeinfo cache */
extern const char pll_regkey_trusted_mods;	/* table: modules allowed in
											 * sandbox */
extern const char pll_regkey_misc;	/* table for private use by modules */
#define PLL_REGKEY_SANDBOX			(&pll_regkey_sandbox)
#define PLL_REGKEY_TYPEINFOS		(&pll_regkey_typeinfos)
#define PLL_REGKEY_TRUSTED_MODS		(&pll_regkey_trusted_mods)
#define PLL_REGKEY_MISC				(&pll_regkey_misc)

/*
 * Configuration (GUCs), defined in pl_lua.c
 */
extern PGDLLEXPORT bool pll_strict;
extern PGDLLEXPORT bool pll_install_globals;
extern PGDLLEXPORT bool pll_check_for_interrupts;
extern PGDLLEXPORT double pll_extra_gc_multiplier;
extern PGDLLEXPORT double pll_extra_gc_threshold;
extern PGDLLEXPORT char *pll_interpreter_reload_ident;

/*
 * Information about the function currently being run in this backend.
 * These return values for the innermost active call, and are meaningful
 * only while Lua code called from a handler is running.
 */
extern bool pll_in_call(void);	/* is there an active call? */
extern bool pll_call_readonly(void);	/* run SPI queries read-only? */
extern bool pll_call_atomic(void);	/* is transaction control unavailable? */
extern List *pll_call_trftypes(void);	/* OIDs from TRANSFORM FOR TYPE */
extern const char *pll_call_name(void);
extern Oid	pll_call_langoid(void); /* OID of the language of the function */

/*
 * Push the typeinfo of argument number argno (1-based) of the current
 * function, or of its result if argno == 0, or nil if there is no such
 * argument or no call is active.  For polymorphic arguments, this is the
 * actual type.
 */
extern void pll_call_push_argtype(lua_State * L, int argno);

/*
 * Run PL/Lua code outside any function (initialization strings).  Raises
 * a PG error on failure.  'chunkname' is for messages.
 */

/* Memory accounting hint for the extra GC feature: bytes allocated outside Lua */
extern void pll_note_external_alloc(lua_State * L, size_t bytes);

/* ------------------------------------------------------------------------
 * Error handling (pl_lua_error.c)
 * ------------------------------------------------------------------------
 */

/*
 * PLL_TRY / PLL_CATCH: see the rules above.
 *
 *	PLL_TRY()
 *	{
 *		... PG code only ...
 *	}
 *	PLL_CATCH(L);
 *
 * If the body raises a PG error, it is converted to a Lua error object and
 * raised in L, and PLL_CATCH does not return.  The current memory context is
 * restored to what it was at PLL_TRY first.  It is restored on normal exit
 * as well: SPI calls leave the SPI procedure context current, and anything
 * allocated there by the C code that called them is freed by SPI_finish,
 * which would matter when user code (a metamethod) runs SPI in the middle
 * of a conversion.
 */
#define PLL_TRY() \
	do { \
		MemoryContext pll_try_cxt_ = CurrentMemoryContext; \
		ErrorData * volatile pll_try_edata_ = NULL; \
		PG_TRY()

#define PLL_CATCH(L) \
		PG_CATCH(); \
		{ \
			MemoryContextSwitchTo(pll_try_cxt_); \
			pll_try_edata_ = CopyErrorData(); \
			FlushErrorState(); \
		} \
		PG_END_TRY(); \
		MemoryContextSwitchTo(pll_try_cxt_); \
		if (pll_try_edata_ != NULL) \
			pll_throw_edata((L), pll_try_edata_); \
	} while (0)

/*
 * For code that catches a PostgreSQL error with PG_TRY and PG_CATCH itself
 * and carries on, as the finalizers of objects must (they never raise
 * errors): leave ErrorContext, which is the current context after an error,
 * and drop the error.  cxt is CurrentMemoryContext at the PG_TRY.  Without
 * the switch, whatever runs next (and a finalizer runs at any Lua allocation)
 * finds ErrorContext current, and allocates in memory that FlushErrorState
 * frees at the next error.
 */
static inline void
pll_discard_error(MemoryContext cxt)
{
	MemoryContextSwitchTo(cxt);
	FlushErrorState();
}

/*
 * Push an error object for edata (which is not freed).
 */
extern void pll_push_edata(lua_State * L, ErrorData *edata);

/*
 * Push an error object for edata, free edata, and raise it as a Lua error.
 */
pg_noreturn extern void pll_throw_edata(lua_State * L, ErrorData *edata);

/*
 * Is the value at idx an error object (a PG error)?
 */
extern bool pll_is_error_object(lua_State * L, int idx);

/*
 * The Lua error value is on top of the stack (and is popped).  Turn it into
 * a PG error and throw it.  Error objects keep their SQLSTATE and fields;
 * strings and other values are reported with SQLSTATE
 * ERRCODE_EXTERNAL_ROUTINE_EXCEPTION.  The text of the message is verified
 * to be valid in the database encoding, as for elog().
 */
pg_noreturn extern void pll_rethrow(lua_State * L);

/*
 * Protected call from C.  Like lua_pcall (msgh is a stack index or 0), but
 * on failure PG_exception_stack and error_context_stack are restored to
 * their values at entry.  Returns the lua_pcall status.  The error value
 * (if any) is on top of the stack.
 */
extern int	pll_pcall(lua_State * L, int nargs, int nresults, int msgh);

/*
 * Like pll_pcall, but if the call fails, the error is turned into a PG
 * error and thrown (pll_rethrow); so this only returns on success.  Must NOT
 * be called from inside a Lua C function without PLL_TRY protection in
 * the sense that the PG error propagates out as a PG error: it is for use
 * from the handlers, which are called from PostgreSQL.
 */
extern void pll_pcall_rethrow(lua_State * L, int nargs, int nresults);

/*
 * Resume a coroutine with PG-error protection for the exception stack.
 * Same interface as lua_resume; the 'from' state is L.
 */
extern int	pll_resume(lua_State * co, lua_State * L, int nargs, int *nresults);

/*
 * Verify that a string is valid in the database encoding, raising a Lua
 * error (SQLSTATE 22021) if not.  May raise a Lua error.
 */
extern void pll_verify_encoding(lua_State * L, const char *s, size_t len);

/*
 * Replace print, pcall, xpcall and coroutine.resume in the global
 * environment of a fresh state with the PL/Lua versions, and add lpcall.
 */
extern void pll_install_error_functions(lua_State * L);

/*
 * Map a SQLSTATE to its condition name, or NULL if it has none.
 */
extern const char *pll_condition_name(int sqlstate);

/*
 * Parse a SQLSTATE given as a five-character code or as a condition name,
 * and return it as an ERRCODE_xxx value.  Raises a Lua error if it is not
 * valid.
 */
extern int	pll_sqlstate_parse(lua_State * L, const char *s);

extern int	pll_open_elog(lua_State * L);
extern int	pll_open_error(lua_State * L);

/* ------------------------------------------------------------------------
 * Sandbox and trusted modules (pl_lua_trusted.c)
 * ------------------------------------------------------------------------
 */

extern int	pll_open_trusted(lua_State * L);
extern int	pll_open_paths(lua_State * L);

/*
 * Build the sandbox table of a trusted interpreter, after all modules have
 * been loaded, and store it in the registry at PLL_REGKEY_SANDBOX.  Also
 * sets sandbox globals spi and pgtype if install_globals.
 */
extern void pll_sandbox_build(lua_State * L, bool install_globals);

/*
 * Add or remove string.dump from the global environment of an interpreter
 * (it is removed from trusted interpreters' global environment too, since
 * on_trusted_init code must not have it).
 */
extern void pll_remove_string_dump(lua_State * L);

/* ------------------------------------------------------------------------
 * Types and datum objects (pl_lua_datum.c)
 *
 * A typeinfo object is a full userdata (metatable "pl_lua.typeinfo")
 * wrapping a pll_typeinfo.  A datum object is a full userdata wrapping a
 * pll_datum.  Both are Lua-owned: freed by Lua's garbage collector.
 * ------------------------------------------------------------------------
 */

typedef enum pll_kind
{
	PLL_KIND_SCALAR,			/* anything with no special handling */
	PLL_KIND_COMPOSITE,			/* including RECORD */
	PLL_KIND_ARRAY,
	PLL_KIND_RANGE,
	PLL_KIND_NUMERIC,
	PLL_KIND_JSONB,
	PLL_KIND_TIME				/* date, time, timetz, timestamp, timestamptz,
								 * interval */
} pll_kind;

/*
 * Metatable names for the different kinds of datum object.  The SCALAR,
 * COMPOSITE, ARRAY and RANGE metatables are created by pl_lua_datum.c; the
 * others by the modules for those types.  They are created in the registry
 * (luaL_newmetatable) when the interpreter is created.
 */
#define PLL_MT_TYPEINFO		"pl_lua.typeinfo"
#define PLL_MT_DATUM		"pl_lua.datum"
#define PLL_MT_COMPOSITE	"pl_lua.datum.composite"
#define PLL_MT_ARRAY		"pl_lua.datum.array"
#define PLL_MT_RANGE		"pl_lua.datum.range"
#define PLL_MT_NUMERIC		"pl_lua.datum.numeric"
#define PLL_MT_JSONB		"pl_lua.datum.jsonb"
#define PLL_MT_TIME			"pl_lua.datum.time"

typedef struct pll_typeinfo
{
	Oid			typoid;			/* the type as named (may be a domain) */
	int32		typmod;			/* -1 if none */
	Oid			basetypoid;		/* after resolving domains */
	int32		basetypmod;
	pll_kind	kind;			/* kind of the base type */
	int16		typlen;
	bool		typbyval;
	char		typalign;
	char		typtype;
	Oid			elemtypoid;		/* element type, for array and range */
	/* the rest is private to pl_lua_datum.c; it may add members here */
	Oid			typinput;
	Oid			typoutput;
	Oid			typreceive;
	Oid			typsend;
	Oid			typioparam;
	FmgrInfo	finput;
	FmgrInfo	foutput;
	FmgrInfo	freceive;
	FmgrInfo	fsend;
	bool		funcs_valid;
	uint64		generation;		/* compared to catalog invalidation counter */
	int			native;			/* how pl_lua_datum.c maps the type */
	int16		elmlen;			/* element type properties (array) */
	bool		elmbyval;
	char		elmalign;
	TupleDesc	tupdesc;		/* composite: copy in the interp's context */
} pll_typeinfo;

/*
 * A datum object.  'value' is a Datum that is valid for as long as the
 * userdata is alive and has not been modified; by-reference values point to
 * storage owned by the userdata (stored in uservalue 2, a "blob" userdata).
 * uservalue 1 is the typeinfo userdata.
 */
typedef struct pll_datum
{
	pll_typeinfo *ti;
	Datum		value;			/* never a null: null SQL values are nil */
} pll_datum;

extern int	pll_open_pgtype(lua_State * L);

/*
 * A lua_CFunction implementing __tostring for datum objects of any kind, for
 * use in the metatables of the other datum modules.
 */
extern int	pll_datum_tostring(lua_State * L);

/*
 * Add __eq, __lt and __le metamethods to the metatable on top of the stack,
 * comparing datum objects of the same type with the equality operator and the
 * btree comparison function of the type.  Used for all datum kinds except
 * numeric (pl_lua_numeric.c has its own).
 */
extern void pll_datum_add_compare(lua_State * L);

/*
 * Push the typeinfo for (typoid, typmod), creating it if necessary.  The
 * returned pointer is valid while the pushed object is reachable.  The
 * object is cached in the registry, so repeated calls are cheap.  Raises a
 * Lua error for a nonexistent type.
 */
extern pll_typeinfo *pll_typeinfo_push(lua_State * L, Oid typoid, int32 typmod);

/* Returns the typeinfo at idx, or NULL if it is not a typeinfo object */
extern pll_typeinfo *pll_typeinfo_test(lua_State * L, int idx);

/* Likewise, but raises a Lua error if it isn't one */
extern pll_typeinfo *pll_typeinfo_check(lua_State * L, int idx);

/*
 * Convert a SQL value to a Lua value according to the rules in the
 * documentation (nil for null; string, boolean, integer, float for some
 * types; transform if one applies to the current function; otherwise a
 * datum object).  Pushes exactly one value.  The Datum is not modified or
 * retained: datum objects get their own copy.  May raise Lua errors.
 */
extern void pll_value_push(lua_State * L, Datum value, bool isnull,
						   pll_typeinfo *ti);

/*
 * Convert the Lua value at idx to a Datum of the type described by ti, by
 * the rules in the documentation.  *isnull is set for nil.  The result, if
 * pass-by-reference, is allocated in CurrentMemoryContext.  May raise Lua
 * errors, but never lets PG errors escape.
 */
extern Datum pll_value_todatum(lua_State * L, int idx, pll_typeinfo *ti,
							   bool *isnull);

/*
 * Always create a datum object (never a native Lua value) for the given
 * non-null value, copying it.  Pushes the object.
 */
extern void pll_datum_push(lua_State * L, pll_typeinfo *ti, Datum value);

/* Is the value at idx a datum object?  If so, return it (else NULL) */
extern pll_datum *pll_datum_test(lua_State * L, int idx);

/*
 * Push a composite datum object holding a copy of 'tuple' (rows returned by
 * SPI, trigger rows).  The typeinfo is derived from the tuple descriptor
 * (which need not be blessed).
 */
extern void pll_row_push(lua_State * L, HeapTuple tuple, TupleDesc tupdesc);

/*
 * Convert 'n' values on the Lua stack, starting at idx, to a composite
 * Datum of the type in rowti (a composite or RECORD with typmod), one per
 * non-dropped column in order.  n must equal the number of such columns.
 * The result is allocated in CurrentMemoryContext.
 */
extern Datum pll_values_tocomposite(lua_State * L, int idx, int n,
									pll_typeinfo *rowti);

/*
 * Convert 'n' values on the Lua stack, starting at idx, to a value of the
 * array type in ti, as the elements of a one-dimensional array.  For
 * functions that return several values to an array result.  The result is
 * allocated in CurrentMemoryContext.
 */
extern Datum pll_array_from_values(lua_State * L, int idx, int n,
								   pll_typeinfo *ti);

/*
 * Tuple descriptor for a composite typeinfo.  Valid until the end of the
 * transaction (a reference is not held); do not free.
 */
extern TupleDesc pll_typeinfo_tupdesc(lua_State * L, pll_typeinfo *ti);

/*
 * Transform support.  Transform functions are called with fcinfo->context
 * pointing to a pl_lua_node (see pl_lua.h).  fromsql: pushes the Lua value
 * for the datum and returns 0.  tosql: the argument is the stack index of
 * the value; returns the Datum.  Implemented in pl_lua_datum.c, which
 * consults pll_call_trftypes().
 */

/* Refcursor support, implemented in pl_lua_spi.c and used by pl_lua_datum.c */
extern void pll_cursor_push(lua_State * L, const char *portalname);
extern const char *pll_cursor_name(lua_State * L, int idx); /* NULL if the value is
															 * not a cursor object */

/* ------------------------------------------------------------------------
 * numeric (pl_lua_numeric.c)
 * ------------------------------------------------------------------------
 */
extern int	pll_open_numeric(lua_State * L);

/* ------------------------------------------------------------------------
 * jsonb (pl_lua_jsonb.c)
 * ------------------------------------------------------------------------
 */
extern int	pll_open_jsonb(lua_State * L);

/*
 * Convert the Lua value at idx to a jsonb Datum (CurrentMemoryContext).
 * optsidx is the stack index of a table of conversion options (null,
 * map, empty_object, array_thresh, array_frac), or 0 for the defaults.
 * May raise Lua errors, never lets PG errors escape.
 */
extern Datum pll_jsonb_todatum(lua_State * L, int idx, int optsidx);

/* ------------------------------------------------------------------------
 * date/time (pl_lua_time.c)
 * ------------------------------------------------------------------------
 */
extern int	pll_open_time(lua_State * L);

/*
 * Convert a Lua table of fields (at idx) to a Datum of the date/time type
 * described by ti (date, time, timetz, timestamp, timestamptz or interval).
 * CurrentMemoryContext receives any pass-by-reference result.
 */
extern Datum pll_time_todatum(lua_State * L, int idx, pll_typeinfo *ti);

/*
 * Raise a Lua error object with the given SQLSTATE and a message made by
 * printf-style formatting.  For use by Lua-side code that is outside PLL_TRY
 * (defined in pl_lua_time.c, also used by pl_lua_jsonb.c).
 */
pg_noreturn extern void pll_tj_error(lua_State * L, int sqlerrcode,
									 const char *fmt, ...) pg_attribute_printf(3, 4);

/* ------------------------------------------------------------------------
 * SPI (pl_lua_spi.c)
 * ------------------------------------------------------------------------
 */
extern int	pll_open_spi(lua_State * L);

/* ------------------------------------------------------------------------
 * Triggers (pl_lua_trigger.c)
 * ------------------------------------------------------------------------
 */

/*
 * Push the arguments for a trigger function: trigger, old, new, then the
 * CREATE TRIGGER arguments as strings.  Returns the number pushed.
 */
extern int	pll_trigger_push_args(lua_State * L, TriggerData *tdata);

/*
 * After the trigger function has returned 'nret' values (on top of the
 * stack), compute the trigger's return value: a HeapTuple (in
 * CurrentMemoryContext) or NULL to skip the operation.  Only meaningful for
 * row-level BEFORE and INSTEAD OF triggers; for others returns NULL.  The
 * trigger object that was passed to the function is at stack index
 * 'trigidx'.  Must be called inside PLL_TRY by the caller or from a context
 * where Lua errors are caught by pll_pcall.
 */
extern HeapTuple pll_trigger_result(lua_State * L, TriggerData *tdata,
									int trigidx, int nret);

/* Push the argument for an event trigger function.  Returns count pushed. */
extern int	pll_evtrigger_push_args(lua_State * L, EventTriggerData *edata);

#endif							/* PL_LUA_INT_H */
