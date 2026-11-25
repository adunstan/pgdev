/*-------------------------------------------------------------------------
 *
 * pl_lua_trigger.c
 *	  Arguments and results of trigger and event trigger functions.
 *
 * The trigger object passed to a trigger function is a userdata whose only
 * uservalue is a table of its fields.  Reading looks the field up in that
 * table; writing is allowed only for "row".  The functions here know where
 * to find the table, so they never depend on what Lua code has done with the
 * object's metatable.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua_trigger.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "access/htup_details.h"
#include "catalog/pg_type.h"
#include "commands/event_trigger.h"
#include "commands/trigger.h"
#include "executor/spi.h"
#include "tcop/cmdtag.h"
#include "utils/lsyscache.h"
#include "utils/rel.h"

#include "pl_lua_int.h"

#define PLL_MT_TRIGGER	"pl_lua.trigger"

static bool pll_trigger_metatable_ready(lua_State * L);
static int	pll_trigger_index(lua_State * L);
static int	pll_trigger_newindex(lua_State * L);
static int	pll_trigger_pairs(lua_State * L);
static void pll_push_trigger_object(lua_State * L);
static void pll_push_relation_table(lua_State * L, Relation rel,
									const char *nspname);
static const char *pll_operation_name(TriggerEvent event);
static HeapTuple pll_value_to_tuple(lua_State * L, int idx, Relation rel);

/* Create the trigger metatable, if it does not exist.  Returns true if it did */
static bool
pll_trigger_metatable_ready(lua_State * L)
{
	if (!luaL_newmetatable(L, PLL_MT_TRIGGER))
	{
		lua_pop(L, 1);
		return true;
	}
	lua_pushcfunction(L, pll_trigger_index);
	lua_setfield(L, -2, "__index");
	lua_pushcfunction(L, pll_trigger_newindex);
	lua_setfield(L, -2, "__newindex");
	lua_pushcfunction(L, pll_trigger_pairs);
	lua_setfield(L, -2, "__pairs");
	/* made when first needed, after the other metatables were protected */
	lua_pushboolean(L, 0);
	lua_setfield(L, -2, "__metatable");
	lua_pop(L, 1);
	return false;
}

static int
pll_trigger_index(lua_State * L)
{
	luaL_checkudata(L, 1, PLL_MT_TRIGGER);
	lua_getiuservalue(L, 1, 1);
	lua_pushvalue(L, 2);
	lua_rawget(L, -2);
	return 1;
}

static int
pll_trigger_newindex(lua_State * L)
{
	luaL_checkudata(L, 1, PLL_MT_TRIGGER);
	if (lua_type(L, 2) != LUA_TSTRING || strcmp(lua_tostring(L, 2), "row") != 0)
		return luaL_error(L, "the fields of the trigger object are read-only, except \"row\"");

	lua_getiuservalue(L, 1, 1);
	lua_pushvalue(L, 3);
	lua_setfield(L, -2, "row");
	return 0;
}

static int
pll_trigger_pairs(lua_State * L)
{
	luaL_checkudata(L, 1, PLL_MT_TRIGGER);
	lua_getglobal(L, "next");
	lua_getiuservalue(L, 1, 1);
	lua_pushnil(L);
	return 3;
}

/* Push a new trigger object with an empty table of fields */
static void
pll_push_trigger_object(lua_State * L)
{
	(void) lua_newuserdatauv(L, 1, 1);
	pll_trigger_metatable_ready(L);
	luaL_setmetatable(L, PLL_MT_TRIGGER);
	lua_createtable(L, 0, 10);
	lua_setiuservalue(L, -2, 1);
}

static const char *
pll_operation_name(TriggerEvent event)
{
	if (TRIGGER_FIRED_BY_INSERT(event))
		return "insert";
	if (TRIGGER_FIRED_BY_UPDATE(event))
		return "update";
	if (TRIGGER_FIRED_BY_DELETE(event))
		return "delete";
	return "truncate";
}

/* Push the table describing the relation */
static void
pll_push_relation_table(lua_State * L, Relation rel, const char *nspname)
{
	TupleDesc	tupdesc = RelationGetDescr(rel);
	int			i;

	luaL_checkstack(L, 6, "out of stack space");
	lua_createtable(L, 0, 4);
	lua_pushstring(L, RelationGetRelationName(rel));
	lua_setfield(L, -2, "name");
	lua_pushstring(L, nspname);
	lua_setfield(L, -2, "namespace");
	lua_pushinteger(L, (lua_Integer) RelationGetRelid(rel));
	lua_setfield(L, -2, "oid");

	lua_createtable(L, 0, tupdesc->natts);
	for (i = 0; i < tupdesc->natts; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);

		if (att->attisdropped)
			continue;
		lua_pushinteger(L, att->attnum);
		lua_setfield(L, -2, NameStr(att->attname));
	}
	lua_setfield(L, -2, "attributes");
}

/*
 * Push the arguments of a trigger function: the trigger object, the old and
 * new rows (nil where they do not exist), and the arguments from CREATE
 * TRIGGER.
 */
int
pll_trigger_push_args(lua_State * L, TriggerData *tdata)
{
	Relation	rel = tdata->tg_relation;
	TupleDesc	tupdesc = RelationGetDescr(rel);
	TriggerEvent event = tdata->tg_event;
	Trigger    *trigger = tdata->tg_trigger;
	char	   *volatile nspname = NULL;
	int			base = lua_gettop(L);
	int			nargs;
	int			trigidx;
	int			fields;
	int			newidx = 0;
	int			oldidx = 0;
	bool		for_row = TRIGGER_FIRED_FOR_ROW(event);
	int			i;

	PLL_TRY();
	{
		nspname = get_namespace_name(RelationGetNamespace(rel));

		/* make any transition tables visible to queries */
		if (SPI_register_trigger_data(tdata) != SPI_OK_TD_REGISTER)
			elog(ERROR, "SPI_register_trigger_data failed");
	}
	PLL_CATCH(L);

	luaL_checkstack(L, 8 + trigger->tgnargs, "too many trigger arguments");

	pll_push_trigger_object(L);
	trigidx = lua_gettop(L);
	lua_getiuservalue(L, trigidx, 1);
	fields = lua_gettop(L);

	/* the rows, which go on the stack after the trigger object */
	if (for_row)
	{
		if (TRIGGER_FIRED_BY_INSERT(event))
		{
			pll_row_push(L, tdata->tg_trigtuple, tupdesc);
			newidx = lua_gettop(L);
		}
		else if (TRIGGER_FIRED_BY_DELETE(event))
		{
			pll_row_push(L, tdata->tg_trigtuple, tupdesc);
			oldidx = lua_gettop(L);
		}
		else
		{
			pll_row_push(L, tdata->tg_trigtuple, tupdesc);
			oldidx = lua_gettop(L);
			pll_row_push(L, tdata->tg_newtuple, tupdesc);
			newidx = lua_gettop(L);
		}
	}

	if (newidx)
	{
		lua_pushvalue(L, newidx);
		lua_setfield(L, fields, "new");
	}
	if (oldidx)
	{
		lua_pushvalue(L, oldidx);
		lua_setfield(L, fields, "old");
	}
	if (newidx || oldidx)
	{
		lua_pushvalue(L, TRIGGER_FIRED_BY_DELETE(event) ? oldidx : newidx);
		lua_setfield(L, fields, "row");
	}

	lua_pushstring(L, trigger->tgname);
	lua_setfield(L, fields, "name");

	if (TRIGGER_FIRED_BEFORE(event))
		lua_pushliteral(L, "before");
	else if (TRIGGER_FIRED_AFTER(event))
		lua_pushliteral(L, "after");
	else
		lua_pushliteral(L, "instead");
	lua_setfield(L, fields, "when");

	lua_pushstring(L, pll_operation_name(event));
	lua_setfield(L, fields, "operation");
	lua_pushstring(L, pll_operation_name(event));
	lua_setfield(L, fields, "op");

	lua_pushstring(L, for_row ? "row" : "statement");
	lua_setfield(L, fields, "level");

	pll_push_relation_table(L, rel, nspname ? nspname : "");
	lua_setfield(L, fields, "relation");

	/*
	 * Put the arguments, trigger, old, new and the CREATE TRIGGER arguments,
	 * above the function that the caller pushed, discarding the scratch
	 * values between.
	 */
	lua_pushvalue(L, trigidx);
	if (oldidx)
		lua_pushvalue(L, oldidx);
	else
		lua_pushnil(L);
	if (newidx)
		lua_pushvalue(L, newidx);
	else
		lua_pushnil(L);
	for (i = 0; i < trigger->tgnargs; i++)
		lua_pushstring(L, trigger->tgargs[i]);

	nargs = 3 + trigger->tgnargs;
	for (i = 0; i < nargs; i++)
		lua_copy(L, lua_gettop(L) - nargs + 1 + i, base + 1 + i);
	lua_settop(L, base + nargs);
	return nargs;
}

/*
 * Convert the value at idx, a row or a table of column values, to a heap
 * tuple of the relation's row type, allocated in the current context.
 */
static HeapTuple
pll_value_to_tuple(lua_State * L, int idx, Relation rel)
{
	TupleDesc	tupdesc = RelationGetDescr(rel);
	pll_typeinfo *ti;
	Datum		d;
	bool		isnull;
	HeapTuple volatile result = NULL;

	idx = lua_absindex(L, idx);
	ti = pll_typeinfo_push(L, tupdesc->tdtypeid, -1);
	d = pll_value_todatum(L, idx, ti, &isnull);
	lua_pop(L, 1);
	if (isnull)
		return NULL;

	PLL_TRY();
	{
		HeapTupleHeader td = DatumGetHeapTupleHeader(d);
		HeapTupleData tmp;
		Datum	   *values = palloc(sizeof(Datum) * tupdesc->natts);
		bool	   *nulls = palloc(sizeof(bool) * tupdesc->natts);

		tmp.t_len = HeapTupleHeaderGetDatumLength(td);
		ItemPointerSetInvalid(&tmp.t_self);
		tmp.t_tableOid = InvalidOid;
		tmp.t_data = td;
		heap_deform_tuple(&tmp, tupdesc, values, nulls);
		result = heap_form_tuple(tupdesc, values, nulls);
		pfree(values);
		pfree(nulls);
	}
	PLL_CATCH(L);
	return result;
}

/*
 * Work out what a trigger function returns, given the nret values it
 * returned, which are on the stack after the copy of the trigger object at
 * trigidx.  Only row-level BEFORE and INSTEAD OF triggers have a result.
 */
HeapTuple
pll_trigger_result(lua_State * L, TriggerData *tdata, int trigidx, int nret)
{
	TriggerEvent event = tdata->tg_event;
	int			fields;
	int			validx;

	if (!TRIGGER_FIRED_FOR_ROW(event) ||
		!(TRIGGER_FIRED_BEFORE(event) || TRIGGER_FIRED_INSTEAD(event)))
		return NULL;

	luaL_checkstack(L, 4, "out of stack space");
	trigidx = lua_absindex(L, trigidx);
	lua_getiuservalue(L, trigidx, 1);
	fields = lua_gettop(L);

	if (nret == 0)
	{
		/* proceed with the row, which might have been changed or replaced */
		lua_getfield(L, fields, "row");
		validx = lua_gettop(L);
	}
	else
		validx = trigidx + 1;

	if (lua_isnil(L, validx))
		return NULL;

	return pll_value_to_tuple(L, validx, tdata->tg_relation);
}

/* Push the argument of an event trigger function: the trigger table */
int
pll_evtrigger_push_args(lua_State * L, EventTriggerData *edata)
{
	luaL_checkstack(L, 4, "out of stack space");
	lua_createtable(L, 0, 2);
	lua_pushstring(L, edata->event);
	lua_setfield(L, -2, "event");
	lua_pushstring(L, GetCommandTagName(edata->tag));
	lua_setfield(L, -2, "tag");
	return 1;
}
