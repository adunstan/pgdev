/*-------------------------------------------------------------------------
 *
 * pl_lua_numeric.c
 *	  numeric datum objects and the pl_lua.numeric module.
 *
 * A numeric value is an ordinary datum object (see pl_lua_datum.c) with the
 * metatable PLL_MT_NUMERIC, which is created here.  Arithmetic and
 * comparison use PostgreSQL's own numeric functions, so results are exact,
 * and follow SQL semantics rather than Lua's (// truncates toward zero, and
 * % has the sign of the dividend).  Lua numbers and numeric strings are
 * accepted as the other operand and converted to numeric first.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua_numeric.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "catalog/pg_type.h"
#include "nodes/miscnodes.h"
#include "utils/builtins.h"
#include "utils/fmgrprotos.h"
#include "utils/numeric.h"

#include "pl_lua_int.h"

/* The kind of function a table entry describes */
typedef enum num_optype
{
	NUM_BINARY,					/* numeric result from two numerics */
	NUM_COMPARE,				/* boolean result from two numerics */
	NUM_UNARY					/* numeric result from one numeric */
} num_optype;

typedef struct num_op
{
	const char *name;			/* Lua name: metamethod or function name */
	num_optype	type;
	PGFunction	func;
	bool		method;			/* also a function of the module */
} num_op;

static Datum num_arg(lua_State * L, int idx);
static bool num_is_numeric(lua_State * L, int idx);
static void num_push(lua_State * L, Datum value);
static void num_push_int64(lua_State * L, int64 val);
static int	num_op_cfunc(lua_State * L);
static int	num_log_cfunc(lua_State * L);
static int	num_round_cfunc(lua_State * L);
static int	num_isnan_cfunc(lua_State * L);
static int	num_tointeger_cfunc(lua_State * L);
static int	num_tonumber_cfunc(lua_State * L);
static int	num_new_cfunc(lua_State * L);
static int	num_round_common(lua_State * L, PGFunction func);
static int	num_trunc_cfunc(lua_State * L);

/*
 * The operations that are table-driven.  The metamethods are first, then the
 * functions that are also methods.
 */
static const num_op num_ops[] = {
	{"__add", NUM_BINARY, numeric_add, false},
	{"__sub", NUM_BINARY, numeric_sub, false},
	{"__mul", NUM_BINARY, numeric_mul, false},
	{"__div", NUM_BINARY, numeric_div, false},
	{"__mod", NUM_BINARY, numeric_mod, false},
	{"__idiv", NUM_BINARY, numeric_div_trunc, false},
	{"__pow", NUM_BINARY, numeric_power, false},
	{"__unm", NUM_UNARY, numeric_uminus, false},
	{"__eq", NUM_COMPARE, numeric_eq, false},
	{"__lt", NUM_COMPARE, numeric_lt, false},
	{"__le", NUM_COMPARE, numeric_le, false},
	{"equal", NUM_COMPARE, numeric_eq, true},
	{"abs", NUM_UNARY, numeric_abs, true},
	{"ceil", NUM_UNARY, numeric_ceil, true},
	{"floor", NUM_UNARY, numeric_floor, true},
	{"exp", NUM_UNARY, numeric_exp, true},
	{"sqrt", NUM_UNARY, numeric_sqrt, true},
	{"sign", NUM_UNARY, numeric_sign, true},
	{NULL, NUM_UNARY, NULL, false}
};

/* The numeric Datum for the value at idx: a numeric, number or string */
static Datum
num_arg(lua_State * L, int idx)
{
	pll_typeinfo *ti;
	Datum		result;
	bool		isnull;

	idx = lua_absindex(L, idx);
	if (lua_isnil(L, idx) || lua_isnone(L, idx))
		luaL_error(L, "numeric argument expected, got %s", luaL_typename(L, idx));

	ti = pll_typeinfo_push(L, NUMERICOID, -1);
	result = pll_value_todatum(L, idx, ti, &isnull);
	lua_pop(L, 1);
	return result;
}

/* Push a numeric datum object for a value */
static void
num_push(lua_State * L, Datum value)
{
	pll_typeinfo *ti = pll_typeinfo_push(L, NUMERICOID, -1);

	pll_datum_push(L, ti, value);
	lua_remove(L, -2);
}

/* Push a numeric datum object for an integer */
static void
num_push_int64(lua_State * L, int64 val)
{
	Datum		d = (Datum) 0;

	PLL_TRY();
	{
		d = DirectFunctionCall1(int8_numeric, Int64GetDatum(val));
	}
	PLL_CATCH(L);
	num_push(L, d);
}

/* Is the value at idx a numeric datum object? */
static bool
num_is_numeric(lua_State * L, int idx)
{
	pll_datum  *d = pll_datum_test(L, idx);

	return d != NULL && d->ti->basetypoid == NUMERICOID;
}

/* The functions in num_ops; upvalue 1 is the index into the table */
static int
num_op_cfunc(lua_State * L)
{
	const num_op *op = &num_ops[lua_tointeger(L, lua_upvalueindex(1))];
	Datum		a;
	Datum		b = (Datum) 0;
	Datum		result = (Datum) 0;

	/*
	 * The __eq metamethod is called for any two datum objects, but a numeric
	 * is only ever equal to another numeric, and == must not raise errors.
	 */
	if (strcmp(op->name, "__eq") == 0 &&
		!(num_is_numeric(L, 1) && num_is_numeric(L, 2)))
	{
		lua_pushboolean(L, false);
		return 1;
	}

	a = num_arg(L, 1);
	if (op->type != NUM_UNARY)
		b = num_arg(L, 2);

	PLL_TRY();
	{
		if (op->type == NUM_UNARY)
			result = DirectFunctionCall1Coll(op->func, InvalidOid, a);
		else
			result = DirectFunctionCall2Coll(op->func, InvalidOid, a, b);
	}
	PLL_CATCH(L);

	if (op->type == NUM_COMPARE)
		lua_pushboolean(L, DatumGetBool(result));
	else
		num_push(L, result);
	return 1;
}

/* log(x [, base]) */
static int
num_log_cfunc(lua_State * L)
{
	Datum		x = num_arg(L, 1);
	Datum		base = (Datum) 0;
	bool		have_base = !lua_isnoneornil(L, 2);
	Datum		result = (Datum) 0;

	if (have_base)
		base = num_arg(L, 2);

	PLL_TRY();
	{
		if (have_base)
			result = DirectFunctionCall2(numeric_log, base, x);
		else
			result = DirectFunctionCall1(numeric_ln, x);
	}
	PLL_CATCH(L);

	num_push(L, result);
	return 1;
}

/* round and trunc, with an optional number of decimal places */
static int
num_round_common(lua_State * L, PGFunction func)
{
	Datum		x = num_arg(L, 1);
	int32		places = 0;
	Datum		result = (Datum) 0;

	if (!lua_isnoneornil(L, 2))
		places = (int32) luaL_checkinteger(L, 2);

	PLL_TRY();
	{
		result = DirectFunctionCall2(func, x, Int32GetDatum(places));
	}
	PLL_CATCH(L);

	num_push(L, result);
	return 1;
}

static int
num_round_cfunc(lua_State * L)
{
	return num_round_common(L, numeric_round);
}

static int
num_trunc_cfunc(lua_State * L)
{
	return num_round_common(L, numeric_trunc);
}

static int
num_isnan_cfunc(lua_State * L)
{
	Datum		x = num_arg(L, 1);
	bool		result = false;

	PLL_TRY();
	{
		result = numeric_is_nan(DatumGetNumeric(x));
	}
	PLL_CATCH(L);

	lua_pushboolean(L, result);
	return 1;
}

/* The value as a Lua integer, or nil if it is not exactly one */
static int
num_tointeger_cfunc(lua_State * L)
{
	Datum		x = num_arg(L, 1);
	bool		ok = false;
	int64		val = 0;

	PLL_TRY();
	{
		Numeric		n = DatumGetNumeric(x);

		if (!numeric_is_nan(n) && !numeric_is_inf(n))
		{
			Datum		t = DirectFunctionCall2(numeric_trunc, x, Int32GetDatum(0));

			if (DatumGetBool(DirectFunctionCall2(numeric_eq, t, x)))
			{
				ErrorSaveContext escontext = {T_ErrorSaveContext};
				char	   *str = DatumGetCString(DirectFunctionCall1(numeric_out, t));

				val = pg_strtoint64_safe(str, (Node *) &escontext);
				ok = !escontext.error_occurred;
			}
		}
	}
	PLL_CATCH(L);

	if (ok)
		lua_pushinteger(L, (lua_Integer) val);
	else
		lua_pushnil(L);
	return 1;
}

/* An approximation of the value as a Lua float */
static int
num_tonumber_cfunc(lua_State * L)
{
	Datum		x = num_arg(L, 1);
	double		val = 0;

	PLL_TRY();
	{
		val = DatumGetFloat8(DirectFunctionCall1(numeric_float8, x));
	}
	PLL_CATCH(L);

	lua_pushnumber(L, (lua_Number) val);
	return 1;
}

/* new(x): a numeric from a number, string or numeric */
static int
num_new_cfunc(lua_State * L)
{
	Datum		x = num_arg(L, 1);

	num_push(L, x);
	return 1;
}

int
pll_open_numeric(lua_State * L)
{
	int			i;

	/* the module table, which is also the method table */
	lua_newtable(L);

	luaL_newmetatable(L, PLL_MT_NUMERIC);
	lua_pushcfunction(L, pll_datum_tostring);
	lua_setfield(L, -2, "__tostring");
	lua_pushvalue(L, -2);
	lua_setfield(L, -2, "__index");

	for (i = 0; num_ops[i].name; i++)
	{
		lua_pushinteger(L, i);
		lua_pushcclosure(L, num_op_cfunc, 1);
		if (num_ops[i].method)
		{
			lua_pushvalue(L, -1);
			lua_setfield(L, -4, num_ops[i].name);	/* the module table */
		}
		if (num_ops[i].name[0] == '_')
			lua_setfield(L, -2, num_ops[i].name);	/* the metatable */
		else
			lua_pop(L, 1);
	}
	lua_pop(L, 1);				/* the metatable */

	/* the limits of Lua integers, as numerics */
	num_push_int64(L, PG_INT64_MAX);
	lua_setfield(L, -2, "maxinteger");
	num_push_int64(L, PG_INT64_MIN);
	lua_setfield(L, -2, "mininteger");

	lua_pushcfunction(L, num_log_cfunc);
	lua_setfield(L, -2, "log");
	lua_pushcfunction(L, num_round_cfunc);
	lua_setfield(L, -2, "round");
	lua_pushcfunction(L, num_trunc_cfunc);
	lua_setfield(L, -2, "trunc");
	lua_pushcfunction(L, num_isnan_cfunc);
	lua_setfield(L, -2, "isnan");
	lua_pushcfunction(L, num_tointeger_cfunc);
	lua_setfield(L, -2, "tointeger");
	lua_pushcfunction(L, num_tonumber_cfunc);
	lua_setfield(L, -2, "tonumber");
	lua_pushcfunction(L, num_new_cfunc);
	lua_setfield(L, -2, "new");
	return 1;
}
