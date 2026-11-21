/*-------------------------------------------------------------------------
 *
 * pl_lua_datum.c
 *	  Typeinfo and datum objects; conversion between SQL and Lua values;
 *	  composite, array and range support; the pgtype module.
 *
 * Representation
 *
 * A typeinfo object is a full userdata holding a pll_typeinfo.  Typeinfo
 * objects are cached, strongly, in a registry table keyed by type OID and
 * typmod; they are refreshed in place when catalog invalidations have
 * occurred since they were filled in.
 *
 * A datum object is a full userdata holding a pll_datum.  Its first user
 * value is the typeinfo object, and its second, for pass-by-reference types,
 * is a "blob" userdata that holds a flat copy of the value.  All of the
 * storage is thus owned by Lua, and nothing here keeps palloc'd memory
 * referenced from a Lua object.  Assigning to a column or element replaces
 * the blob.
 *
 * Error handling follows the rules in pl_lua_int.h: PostgreSQL code that can
 * raise errors runs inside PLL_TRY, and Lua operations never do.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua_datum.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <math.h>

#include "access/htup_details.h"
#include "access/tupdesc.h"
#include "catalog/pg_collation_d.h"
#include "catalog/pg_proc.h"
#include "catalog/pg_type.h"
#include "commands/proclang.h"
#include "funcapi.h"
#include "libpq/pqformat.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "nodes/miscnodes.h"
#include "parser/parse_coerce.h"
#include "parser/parse_type.h"
#include "utils/acl.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/datum.h"
#include "utils/inval.h"
#include "utils/lsyscache.h"
#include "utils/rangetypes.h"
#include "utils/syscache.h"
#include "utils/typcache.h"

#include "pl_lua_int.h"

/* How types are mapped to native Lua values (pll_typeinfo.native) */
#define PLL_NAT_NONE	0		/* always a datum object */
#define PLL_NAT_TEXT	1		/* text-like varlena: raw bytes */
#define PLL_NAT_IO		2		/* converted to string by its output function */
#define PLL_NAT_BYTEA	3
#define PLL_NAT_BOOL	4
#define PLL_NAT_INT		5
#define PLL_NAT_FLOAT	6
#define PLL_NAT_CURSOR	7

/* metatable for the intermediate objects of multidimensional array access */
#define PLL_MT_ARRAY_SLICE	"pl_lua.datum.array.slice"

/*
 * The user values of a datum object.  A composite or array value obtained
 * from a column or element of another datum (the parent) is linked to it,
 * so that assigning to the child writes through to the parent.
 */
#define DT_UV_TYPEINFO	1
#define DT_UV_BLOB		2
#define DT_UV_PARENT	3
#define DT_UV_KEY		4		/* attribute number, or table of subscripts */

/* An intermediate object for a[i][j] on a multidimensional array */
typedef struct dt_slice
{
	int			nfixed;			/* number of subscripts given so far */
	int			idx[MAXDIM];
} dt_slice;

/* Options for conversion of composites and arrays to tables */
typedef struct dt_opts
{
	int			mapidx;			/* stack index of the map function, or nil */
	int			nullidx;		/* stack index of the value for nulls */
	bool		discard;
} dt_opts;

/* catalog invalidation counter: typeinfos older than this must refresh */
static uint64 dt_generation = 1;
static bool dt_callbacks_registered = false;

/* the metatables of datum objects of any kind, for pll_datum_test */
static const char *const dt_datum_metatables[] = {
	PLL_MT_DATUM, PLL_MT_COMPOSITE, PLL_MT_ARRAY, PLL_MT_RANGE,
	PLL_MT_NUMERIC, PLL_MT_JSONB, PLL_MT_TIME, NULL
};

static void dt_syscache_callback(Datum arg, SysCacheIdentifier cacheid,
								 uint32 hashvalue);
static void dt_relcache_callback(Datum arg, Oid relid);
static void ti_fill(pll_typeinfo *ti);
static void ti_io(pll_typeinfo *ti);
static void ti_current(lua_State * L, pll_typeinfo *ti);
static TupleDesc dt_tupdesc_snapshot(lua_State * L, pll_typeinfo *ti);
static int	ti_name_cfunc(lua_State * L);
static int	ti_element_cfunc(lua_State * L);
static int	ti_fromstring_cfunc(lua_State * L);
static int	ti_frombinary_cfunc(lua_State * L);
static int	ti_call_cfunc(lua_State * L);
static int	ti_tostring_cfunc(lua_State * L);
static Datum dt_store(lua_State * L, pll_typeinfo *ti, Datum value);
static void dt_datum_replace(lua_State * L, int idx, Datum newvalue);
static pll_datum *dt_check_datum(lua_State * L, int idx);
static pll_datum *dt_check_kind(lua_State * L, int idx, int kind,
								const char *what);
static const char *dt_metatable_for(pll_typeinfo *ti);
static char *dt_output(lua_State * L, pll_typeinfo *ti, Datum value);
static void dt_push_text_bytes(lua_State * L, Datum value);
static Oid	dt_transform_func(lua_State * L, pll_typeinfo *ti, bool fromsql);
static Datum dt_call_transform(lua_State * L, Oid fn, Datum arg, bool *isnull);
static Datum dt_copy_datum(lua_State * L, pll_typeinfo *ti, Datum value);
static Datum dt_cast_datum(lua_State * L, pll_typeinfo *src, Datum value,
						   pll_typeinfo *dst);
static void dt_check_execute(Oid funcid);
static Datum dt_array_map(Datum value, Oid newelemtype,
						  Datum (*fn) (Datum elem, void *arg), void *arg);
static Datum dt_cast_value(Oid srctype, Oid dsttype, Datum value);
static Datum dt_apply_typmod(Oid basetype, int32 typmod, Datum value);
static bool dt_cast_exists(lua_State * L, pll_typeinfo *src, pll_typeinfo *dst);
static Datum dt_finish(lua_State * L, Datum value, pll_typeinfo *ti);
static Datum dt_to_datum(lua_State * L, int idx, pll_typeinfo *ti);
static Datum dt_string_to_datum(lua_State * L, int idx, pll_typeinfo *ti);
static Datum dt_number_to_datum(lua_State * L, int idx, pll_typeinfo *ti);
static Datum dt_input(lua_State * L, const char *s, pll_typeinfo *ti);
static lua_Integer dt_tointeger(lua_State * L, int idx, const char *what);
static bool dt_same_type(pll_typeinfo *a, pll_typeinfo *b);
static void dt_error_noconv(lua_State * L, int idx, pll_typeinfo *ti);
static pll_typeinfo *dt_element_ti(lua_State * L, pll_typeinfo *ti);
static HeapTupleHeader dt_row_header(pll_datum *d);
static int	dt_attno_by_name(TupleDesc td, const char *name);
static void dt_row_push_attr(lua_State * L, int rowidx, pll_datum *d,
							 TupleDesc td, int attno);
static void dt_link_parent(lua_State * L, int childidx, int parentidx);
static void dt_propagate(lua_State * L, int idx);
static void dt_row_set_attr(lua_State * L, int rowidx, int attno, Datum val,
							bool isnull);
static void dt_array_set(lua_State * L, int arridx, int nsub, int *indx,
						 Datum val, bool isnull);
static int	dt_compare_cfunc(lua_State * L);
static Datum dt_composite_from_indexable(lua_State * L, int idx,
										 pll_typeinfo *ti);
static bool dt_indexable(lua_State * L, int idx);
static int	row_index_cfunc(lua_State * L);
static int	row_newindex_cfunc(lua_State * L);
static int	row_pairs_cfunc(lua_State * L);
static int	row_next_cfunc(lua_State * L);
static int	row_call_cfunc(lua_State * L);
static void dt_parse_opts(lua_State * L, int idx, dt_opts *opts);
static Datum dt_array_construct(lua_State * L, pll_typeinfo *ti, int first,
								int nargs, bool as_list);
static int	dt_array_fill(lua_State * L, int tbl, int level, int ndims,
						  int *dims, Datum *elems, bool *nulls, int pos,
						  pll_typeinfo *eti);
static void dt_offsets_forget(lua_State * L, int idx);
static const uint32 *dt_array_offsets(lua_State * L, int arridx, ArrayType *arr,
									  pll_typeinfo *ti);
static void dt_array_get(lua_State * L, int arridx, pll_datum *d, int nsub,
						 int *indx, pll_typeinfo *ti);
static int	row_len_cfunc(lua_State * L);
static int	ti_kind_cfunc(lua_State * L);
static int	array_index_cfunc(lua_State * L);
static int	array_newindex_cfunc(lua_State * L);
static int	array_len_cfunc(lua_State * L);
static int	array_pairs_cfunc(lua_State * L);
static int	array_next_cfunc(lua_State * L);
static int	array_call_cfunc(lua_State * L);
static int	slice_index_cfunc(lua_State * L);
static int	slice_newindex_cfunc(lua_State * L);
static int	slice_len_cfunc(lua_State * L);
static int	dt_array_access(lua_State * L, bool assign);
static void dt_array_call_level(lua_State * L, pll_datum *d, pll_typeinfo *eti,
								Datum *elems, bool *nulls, int level,
								int *pos, int *sub, dt_opts *opts,
								int arrayidx);
static Datum dt_range_construct(lua_State * L, pll_typeinfo *ti, int first,
								int nargs);
static int	range_index_cfunc(lua_State * L);
static int	range_newindex_cfunc(lua_State * L);
static int	pgtype_call_cfunc(lua_State * L);
static int	pgtype_index_cfunc(lua_State * L);
static int	pgtype_array_index_cfunc(lua_State * L);
static void dt_push_parsed_type(lua_State * L, const char *name);
static void dt_add_methods(lua_State * L, const char *mtname,
						   const luaL_Reg * funcs, bool comparable);

/* ------------------------------------------------------------------------
 * Catalog invalidation
 * ------------------------------------------------------------------------
 */

static void
dt_syscache_callback(Datum arg, SysCacheIdentifier cacheid, uint32 hashvalue)
{
	dt_generation++;
}

static void
dt_relcache_callback(Datum arg, Oid relid)
{
	dt_generation++;
}

/* ------------------------------------------------------------------------
 * Typeinfo objects
 * ------------------------------------------------------------------------
 */

/*
 * Fill in the catalog-derived fields of a typeinfo.  Raises PG errors.
 */
static void
ti_fill(pll_typeinfo *ti)
{
	Oid			basetype;
	int32		basetypmod = -1;
	char		typtype;

	basetype = getBaseTypeAndTypmod(ti->typoid, &basetypmod);
	ti->basetypoid = basetype;
	ti->basetypmod = basetypmod;

	get_typlenbyvalalign(basetype, &ti->typlen, &ti->typbyval, &ti->typalign);
	typtype = get_typtype(basetype);
	ti->typtype = typtype;
	ti->elemtypoid = InvalidOid;
	ti->native = PLL_NAT_NONE;
	ti->funcs_valid = false;

	if (basetype == NUMERICOID)
		ti->kind = PLL_KIND_NUMERIC;
	else if (basetype == JSONBOID)
		ti->kind = PLL_KIND_JSONB;
	else if (basetype == DATEOID || basetype == TIMEOID ||
			 basetype == TIMETZOID || basetype == TIMESTAMPOID ||
			 basetype == TIMESTAMPTZOID || basetype == INTERVALOID)
		ti->kind = PLL_KIND_TIME;
	else if (basetype == RECORDOID || typtype == TYPTYPE_COMPOSITE)
		ti->kind = PLL_KIND_COMPOSITE;
	else if (typtype == TYPTYPE_RANGE)
	{
		ti->kind = PLL_KIND_RANGE;
		ti->elemtypoid = get_range_subtype(basetype);
	}
	else
	{
		Oid			elem = get_element_type(basetype);

		if (OidIsValid(elem))
		{
			ti->kind = PLL_KIND_ARRAY;
			ti->elemtypoid = elem;
			get_typlenbyvalalign(elem, &ti->elmlen, &ti->elmbyval,
								 &ti->elmalign);
		}
		else
			ti->kind = PLL_KIND_SCALAR;
	}

	switch (basetype)
	{
		case TEXTOID:
		case VARCHAROID:
		case BPCHAROID:
		case XMLOID:
		case JSONOID:
			ti->native = PLL_NAT_TEXT;
			break;
		case NAMEOID:
		case CSTRINGOID:
			ti->native = PLL_NAT_IO;
			break;
		case BYTEAOID:
			ti->native = PLL_NAT_BYTEA;
			break;
		case BOOLOID:
			ti->native = PLL_NAT_BOOL;
			break;
		case INT2OID:
		case INT4OID:
		case INT8OID:
		case OIDOID:
			ti->native = PLL_NAT_INT;
			break;
		case FLOAT4OID:
		case FLOAT8OID:
			ti->native = PLL_NAT_FLOAT;
			break;
		case REFCURSOROID:
			ti->native = PLL_NAT_CURSOR;
			break;
		default:
			if (typtype == TYPTYPE_ENUM)
				ti->native = PLL_NAT_IO;
			break;
	}

	ti->generation = dt_generation;
}

/* Look up the I/O functions of a type, if we haven't already */
static void
ti_io(pll_typeinfo *ti)
{
	bool		isvarlena;

	if (ti->funcs_valid)
		return;
	getTypeInputInfo(ti->typoid, &ti->typinput, &ti->typioparam);
	getTypeOutputInfo(ti->typoid, &ti->typoutput, &isvarlena);
	ti->typreceive = InvalidOid;
	ti->typsend = InvalidOid;
	ti->funcs_valid = true;
}

/* Refresh a typeinfo if catalog changes have happened since it was filled */
static void
ti_current(lua_State * L, pll_typeinfo *ti)
{
	if (ti->generation == dt_generation)
		return;

	PLL_TRY();
	{
		if (ti->tupdesc != NULL)
		{
			FreeTupleDesc(ti->tupdesc);
			ti->tupdesc = NULL;
		}
		ti_fill(ti);
	}
	PLL_CATCH(L);
}

pll_typeinfo *
pll_typeinfo_push(lua_State * L, Oid typoid, int32 typmod)
{
	lua_Integer key;
	pll_typeinfo *ti;
	int			tbl;

	key = ((lua_Integer) typoid << 32) | (lua_Integer) (uint32) typmod;

	luaL_checkstack(L, 6, "out of stack space");
	lua_rawgetp(L, LUA_REGISTRYINDEX, PLL_REGKEY_TYPEINFOS);
	tbl = lua_gettop(L);
	lua_rawgeti(L, tbl, key);
	if (lua_type(L, -1) == LUA_TUSERDATA)
	{
		ti = (pll_typeinfo *) lua_touserdata(L, -1);
		lua_remove(L, tbl);
		ti_current(L, ti);
		return ti;
	}
	lua_pop(L, 1);

	ti = (pll_typeinfo *) lua_newuserdatauv(L, sizeof(pll_typeinfo), 0);
	memset(ti, 0, sizeof(pll_typeinfo));
	ti->typoid = typoid;
	ti->typmod = typmod;
	luaL_setmetatable(L, PLL_MT_TYPEINFO);

	PLL_TRY();
	{
		ti_fill(ti);
	}
	PLL_CATCH(L);

	lua_pushvalue(L, -1);
	lua_rawseti(L, tbl, key);
	lua_remove(L, tbl);
	return ti;
}

pll_typeinfo *
pll_typeinfo_test(lua_State * L, int idx)
{
	return (pll_typeinfo *) luaL_testudata(L, idx, PLL_MT_TYPEINFO);
}

pll_typeinfo *
pll_typeinfo_check(lua_State * L, int idx)
{
	return (pll_typeinfo *) luaL_checkudata(L, idx, PLL_MT_TYPEINFO);
}

/* typeinfo:name([typmod]) */
static int
ti_name_cfunc(lua_State * L)
{
	pll_typeinfo *ti = pll_typeinfo_check(L, 1);
	int32		typmod = ti->typmod;
	char	   *name = NULL;

	if (!lua_isnoneornil(L, 2))
		typmod = (int32) luaL_checkinteger(L, 2);

	PLL_TRY();
	{
		name = format_type_with_typemod(ti->typoid, typmod);
	}
	PLL_CATCH(L);

	lua_pushstring(L, name);
	return 1;
}

static int
ti_tostring_cfunc(lua_State * L)
{
	pll_typeinfo *ti = pll_typeinfo_check(L, 1);
	char	   *name = NULL;

	PLL_TRY();
	{
		name = format_type_with_typemod(ti->typoid, ti->typmod);
	}
	PLL_CATCH(L);

	lua_pushfstring(L, "pl_lua.typeinfo(%s)", name);
	return 1;
}

/* The typeinfo of an array or range element, or of a composite's column */
static int
ti_element_cfunc(lua_State * L)
{
	pll_typeinfo *ti = pll_typeinfo_check(L, 1);

	if (ti->kind == PLL_KIND_ARRAY || ti->kind == PLL_KIND_RANGE)
	{
		(void) dt_element_ti(L, ti);
		return 1;
	}
	if (ti->kind == PLL_KIND_COMPOSITE)
	{
		TupleDesc	td = pll_typeinfo_tupdesc(L, ti);
		const char *name = luaL_checkstring(L, 2);
		int			attno = dt_attno_by_name(td, name);

		if (attno == 0)
			return luaL_error(L, "type has no column named \"%s\"", name);
		(void) pll_typeinfo_push(L, TupleDescAttr(td, attno - 1)->atttypid,
								 TupleDescAttr(td, attno - 1)->atttypmod);
		return 1;
	}
	return luaL_error(L, "type has no elements");
}

/* The typeinfo of the element of an array or range type; pushes it */
static pll_typeinfo *
dt_element_ti(lua_State * L, pll_typeinfo *ti)
{
	int32		typmod = -1;

	if (ti->kind == PLL_KIND_ARRAY && ti->typmod >= 0)
		typmod = ti->typmod;
	return pll_typeinfo_push(L, ti->elemtypoid, typmod);
}

TupleDesc
pll_typeinfo_tupdesc(lua_State * L, pll_typeinfo *ti)
{
	ti_current(L, ti);
	if (ti->tupdesc == NULL)
	{
		TupleDesc	copy = NULL;

		if (ti->kind != PLL_KIND_COMPOSITE)
			luaL_error(L, "type is not a composite type");
		if (ti->basetypoid == RECORDOID && ti->typmod < 0)
			luaL_error(L, "the structure of an anonymous record type is not known");

		PLL_TRY();
		{
			TupleDesc	tmp;
			MemoryContext oldcxt;

			tmp = lookup_rowtype_tupdesc(ti->basetypoid,
										 ti->basetypoid == RECORDOID ?
										 ti->typmod : -1);
			oldcxt = MemoryContextSwitchTo(pll_interp_of(L)->mcxt);
			copy = CreateTupleDescCopy(tmp);
			MemoryContextSwitchTo(oldcxt);
			ReleaseTupleDesc(tmp);
		}
		PLL_CATCH(L);
		ti->tupdesc = copy;
	}
	return ti->tupdesc;
}

/*
 * A copy of the tuple descriptor of a composite type, in CurrentMemoryContext.
 *
 * The descriptor cached in the typeinfo is freed when the typeinfo is
 * refreshed, which can happen whenever user code runs (it may change the type
 * and then convert a value of it).  Code that calls back into Lua while
 * using a descriptor must therefore work from a copy.
 */
static TupleDesc
dt_tupdesc_snapshot(lua_State * L, pll_typeinfo *ti)
{
	TupleDesc	td = pll_typeinfo_tupdesc(L, ti);
	TupleDesc	copy = NULL;

	PLL_TRY();
	{
		copy = CreateTupleDescCopy(td);
	}
	PLL_CATCH(L);
	return copy;
}

static bool
dt_same_type(pll_typeinfo *a, pll_typeinfo *b)
{
	if (a->basetypoid == b->basetypoid)
		return true;
	/* any composite value can be used where RECORD is expected */
	if (b->basetypoid == RECORDOID && a->kind == PLL_KIND_COMPOSITE)
		return true;
	return false;
}

/* ------------------------------------------------------------------------
 * Datum objects
 * ------------------------------------------------------------------------
 */

static const char *
dt_metatable_for(pll_typeinfo *ti)
{
	switch (ti->kind)
	{
		case PLL_KIND_COMPOSITE:
			return PLL_MT_COMPOSITE;
		case PLL_KIND_ARRAY:
			return PLL_MT_ARRAY;
		case PLL_KIND_RANGE:
			return PLL_MT_RANGE;
		case PLL_KIND_NUMERIC:
			return PLL_MT_NUMERIC;
		case PLL_KIND_JSONB:
			return PLL_MT_JSONB;
		case PLL_KIND_TIME:
			return PLL_MT_TIME;
		default:
			return PLL_MT_DATUM;
	}
}

/*
 * Make a flat copy of a value as a blob userdata and push it (or nil, for a
 * pass-by-value type).  Returns the Datum to keep in the datum object.
 */
static Datum
dt_store(lua_State * L, pll_typeinfo *ti, Datum value)
{
	Datum		flat = value;
	Size		size = 0;
	void	   *blob;

	if (ti->typbyval)
	{
		lua_pushnil(L);
		return value;
	}

	PLL_TRY();
	{
		if (ti->typlen == -1)
		{
			flat = PointerGetDatum(PG_DETOAST_DATUM(value));
			size = VARSIZE(DatumGetPointer(flat));
		}
		else if (ti->typlen == -2)
			size = strlen(DatumGetCString(value)) + 1;
		else
			size = ti->typlen;
	}
	PLL_CATCH(L);

	blob = lua_newuserdatauv(L, size > 0 ? size : 1, 0);
	memcpy(blob, DatumGetPointer(flat), size);
	return PointerGetDatum(blob);
}

void
pll_datum_push(lua_State * L, pll_typeinfo *ti, Datum value)
{
	int			base = lua_gettop(L) + 1;
	pll_typeinfo *tip;
	pll_datum  *d;
	Datum		stored;

	luaL_checkstack(L, 8, "out of stack space");
	tip = pll_typeinfo_push(L, ti->typoid, ti->typmod); /* base */
	stored = dt_store(L, tip, value);	/* base + 1: blob or nil */

	d = (pll_datum *) lua_newuserdatauv(L, sizeof(pll_datum), 4);
	d->ti = tip;
	d->value = stored;
	lua_pushvalue(L, base);
	lua_setiuservalue(L, -2, DT_UV_TYPEINFO);
	lua_pushvalue(L, base + 1);
	lua_setiuservalue(L, -2, DT_UV_BLOB);
	luaL_setmetatable(L, dt_metatable_for(tip));

	lua_copy(L, -1, base);
	lua_settop(L, base);
}

pll_datum *
pll_datum_test(lua_State * L, int idx)
{
	int			i;

	if (lua_type(L, idx) != LUA_TUSERDATA)
		return NULL;
	for (i = 0; dt_datum_metatables[i]; i++)
	{
		pll_datum  *d = (pll_datum *) luaL_testudata(L, idx,
													 dt_datum_metatables[i]);

		if (d != NULL)
			return d;
	}
	return NULL;
}

static pll_datum *
dt_check_datum(lua_State * L, int idx)
{
	pll_datum  *d = pll_datum_test(L, idx);

	if (d == NULL)
		luaL_error(L, "expected a datum object");
	return d;
}

/*
 * The datum object at idx, which must be of the given kind.  Functions that
 * code can get hold of as values (the iterator returned by pairs(), for one)
 * can be called with any arguments, so they must not assume the kind of
 * datum they are given.
 */
static pll_datum *
dt_check_kind(lua_State * L, int idx, int kind, const char *what)
{
	pll_datum  *d = dt_check_datum(L, idx);

	if ((int) d->ti->kind != kind)
		luaL_error(L, "expected %s", what);
	return d;
}

/* Replace the value of the datum object at idx with a copy of newvalue */
static void
dt_datum_replace(lua_State * L, int idx, Datum newvalue)
{
	pll_datum  *d = dt_check_datum(L, idx);

	idx = lua_absindex(L, idx);
	luaL_checkstack(L, 4, "out of stack space");
	d->value = dt_store(L, d->ti, newvalue);
	lua_setiuservalue(L, idx, DT_UV_BLOB);
	dt_offsets_forget(L, idx);
}

/* The text form of a value, in CurrentMemoryContext */
static char *
dt_output(lua_State * L, pll_typeinfo *ti, Datum value)
{
	char	   *result = NULL;

	PLL_TRY();
	{
		ti_io(ti);
		result = OidOutputFunctionCall(ti->typoutput, value);
	}
	PLL_CATCH(L);
	return result;
}

int
pll_datum_tostring(lua_State * L)
{
	pll_datum  *d = dt_check_datum(L, 1);
	char	   *s = dt_output(L, d->ti, d->value);

	lua_pushstring(L, s);
	return 1;
}

/* Push the bytes of a text-like varlena as a string */
static void
dt_push_text_bytes(lua_State * L, Datum value)
{
	struct varlena *v = NULL;

	PLL_TRY();
	{
		v = pg_detoast_datum_packed((struct varlena *) DatumGetPointer(value));
	}
	PLL_CATCH(L);
	lua_pushlstring(L, VARDATA_ANY(v), VARSIZE_ANY_EXHDR(v));
}

/* ------------------------------------------------------------------------
 * Transforms
 * ------------------------------------------------------------------------
 */

/*
 * Find the transform function for the type in the current function, if the
 * function asked for one (TRANSFORM FOR TYPE) and there is one.
 */
static Oid
dt_transform_func(lua_State * L, pll_typeinfo *ti, bool fromsql)
{
	Oid			result = InvalidOid;
	List	   *trftypes = pll_call_trftypes();
	bool		trusted = pll_interp_of(L)->trusted;

	if (trftypes == NIL)
		return InvalidOid;

	PLL_TRY();
	{
		Oid			langoid = get_language_oid(trusted ? "pl_lua" : "pl_luau",
											   true);

		if (OidIsValid(langoid))
			result = fromsql ?
				get_transform_fromsql(ti->typoid, langoid, trftypes) :
				get_transform_tosql(ti->typoid, langoid, trftypes);
	}
	PLL_CATCH(L);
	return result;
}

/*
 * Call a transform function with the argument and the Lua state; see
 * pl_lua.h for the protocol.
 */
static Datum
dt_call_transform(lua_State * L, Oid fn, Datum arg, bool *isnull)
{
	Datum		result = (Datum) 0;

	LOCAL_FCINFO(fcinfo, 1);
	FmgrInfo	flinfo;
	pl_lua_node node;

	node.type = T_Invalid;
	node.magic = PL_LUA_NODE_MAGIC;
	node.L = L;

	luaL_checkstack(L, 8, "out of stack space");

	PLL_TRY();
	{
		fmgr_info(fn, &flinfo);
		InitFunctionCallInfoData(*fcinfo, &flinfo, 1, InvalidOid,
								 (Node *) &node, NULL);
		fcinfo->args[0].value = arg;
		fcinfo->args[0].isnull = false;
		result = FunctionCallInvoke(fcinfo);
		*isnull = fcinfo->isnull;
	}
	PLL_CATCH(L);
	return result;
}

/* ------------------------------------------------------------------------
 * SQL value -> Lua value
 * ------------------------------------------------------------------------
 */

void
pll_value_push(lua_State * L, Datum value, bool isnull, pll_typeinfo *ti)
{
	luaL_checkstack(L, 8, "out of stack space");

	if (isnull)
	{
		lua_pushnil(L);
		return;
	}

	ti_current(L, ti);

	if (pll_call_trftypes() != NIL)
	{
		Oid			fn = dt_transform_func(L, ti, true);

		if (OidIsValid(fn))
		{
			bool		dummy;

			(void) dt_call_transform(L, fn, value, &dummy);
			return;
		}
	}

	switch (ti->native)
	{
		case PLL_NAT_TEXT:
		case PLL_NAT_BYTEA:
			dt_push_text_bytes(L, value);
			break;
		case PLL_NAT_IO:
			lua_pushstring(L, dt_output(L, ti, value));
			break;
		case PLL_NAT_BOOL:
			lua_pushboolean(L, DatumGetBool(value));
			break;
		case PLL_NAT_INT:
			switch (ti->basetypoid)
			{
				case INT2OID:
					lua_pushinteger(L, DatumGetInt16(value));
					break;
				case INT4OID:
					lua_pushinteger(L, DatumGetInt32(value));
					break;
				case INT8OID:
					lua_pushinteger(L, DatumGetInt64(value));
					break;
				default:
					lua_pushinteger(L, (lua_Integer) DatumGetObjectId(value));
					break;
			}
			break;
		case PLL_NAT_FLOAT:
			if (ti->basetypoid == FLOAT4OID)
				lua_pushnumber(L, (lua_Number) DatumGetFloat4(value));
			else
				lua_pushnumber(L, (lua_Number) DatumGetFloat8(value));
			break;
		case PLL_NAT_CURSOR:
			{
				char	   *name = NULL;

				PLL_TRY();
				{
					name = TextDatumGetCString(value);
				}
				PLL_CATCH(L);
				pll_cursor_push(L, name);
			}
			break;
		default:
			pll_datum_push(L, ti, value);
			break;
	}
}

/*
 * Build a row datum object from a tuple.  The tuple descriptor need not be
 * blessed (a copy is blessed if necessary).
 */
void
pll_row_push(lua_State * L, HeapTuple tuple, TupleDesc tupdesc)
{
	TupleDesc	td = tupdesc;
	Datum		datum = (Datum) 0;
	pll_typeinfo *ti;

	PLL_TRY();
	{
		if (td->tdtypeid == RECORDOID && td->tdtypmod < 0)
		{
			td = CreateTupleDescCopy(td);
			BlessTupleDesc(td);
		}
		datum = heap_copy_tuple_as_datum(tuple, td);
	}
	PLL_CATCH(L);

	ti = pll_typeinfo_push(L, td->tdtypeid, td->tdtypmod);
	pll_datum_push(L, ti, datum);
	lua_remove(L, -2);			/* the typeinfo */
}

/* ------------------------------------------------------------------------
 * Lua value -> SQL value
 * ------------------------------------------------------------------------
 */

static void
dt_error_noconv(lua_State * L, int idx, pll_typeinfo *ti)
{
	char	   *name = NULL;

	PLL_TRY();
	{
		name = format_type_be(ti->typoid);
	}
	PLL_CATCH(L);
	luaL_error(L, "cannot convert a Lua %s value to type %s",
			   luaL_typename(L, idx), name);
}

/* A flat copy of a value of the type, in CurrentMemoryContext */
static Datum
dt_copy_datum(lua_State * L, pll_typeinfo *ti, Datum value)
{
	Datum		result = value;

	if (ti->typbyval)
		return value;

	PLL_TRY();
	{
		result = datumCopy(value, ti->typbyval, ti->typlen);
	}
	PLL_CATCH(L);
	return result;
}

/*
 * Convert a value to another type with the assignment cast, checking
 * privileges on the cast function as the SQL command would.
 */
static Datum
dt_cast_datum(lua_State * L, pll_typeinfo *src, Datum value, pll_typeinfo *dst)
{
	Datum		result = (Datum) 0;

	PLL_TRY();
	{
		result = dt_cast_value(src->basetypoid, dst->basetypoid, value);
		if (result == value && !dst->typbyval)
			result = datumCopy(value, dst->typbyval, dst->typlen);
	}
	PLL_CATCH(L);
	return result;
}

/* Check EXECUTE privilege on a function used for a conversion */
static void
dt_check_execute(Oid funcid)
{
	AclResult	aclresult;

	aclresult = object_aclcheck(ProcedureRelationId, funcid, GetUserId(),
								ACL_EXECUTE);
	if (aclresult != ACLCHECK_OK)
		aclcheck_error(aclresult, OBJECT_FUNCTION, get_func_name(funcid));
}

/*
 * Convert the elements of an array with fn(element, arg), making a new array
 * of the same dimensions with elements of the type newelemtype.  Raises PG
 * errors.
 */
static Datum
dt_array_map(Datum value, Oid newelemtype, Datum (*fn) (Datum elem, void *arg),
			 void *arg)
{
	ArrayType  *arr = DatumGetArrayTypeP(value);
	Oid			elemtype = ARR_ELEMTYPE(arr);
	int16		elmlen;
	bool		elmbyval;
	char		elmalign;
	int16		nelmlen;
	bool		nelmbyval;
	char		nelmalign;
	Datum	   *elems;
	bool	   *nulls;
	int			nelems;
	int			i;

	get_typlenbyvalalign(elemtype, &elmlen, &elmbyval, &elmalign);
	get_typlenbyvalalign(newelemtype, &nelmlen, &nelmbyval, &nelmalign);
	deconstruct_array(arr, elemtype, elmlen, elmbyval, elmalign, &elems,
					  &nulls, &nelems);
	for (i = 0; i < nelems; i++)
	{
		if (!nulls[i])
			elems[i] = fn(elems[i], arg);
	}
	if (ARR_NDIM(arr) == 0)
		return PointerGetDatum(construct_empty_array(newelemtype));
	return PointerGetDatum(construct_md_array(elems, nulls, ARR_NDIM(arr),
											  ARR_DIMS(arr), ARR_LBOUND(arr),
											  newelemtype, nelmlen, nelmbyval,
											  nelmalign));
}

typedef struct dt_elemcast
{
	Oid			srctype;
	Oid			dsttype;
	Oid			funcid;			/* for typmod coercion */
	int32		typmod;
} dt_elemcast;

static Datum
dt_elemcast_cast(Datum elem, void *arg)
{
	dt_elemcast *ec = (dt_elemcast *) arg;

	return dt_cast_value(ec->srctype, ec->dsttype, elem);
}

static Datum
dt_elemcast_typmod(Datum elem, void *arg)
{
	dt_elemcast *ec = (dt_elemcast *) arg;

	return OidFunctionCall3(ec->funcid, elem, Int32GetDatum(ec->typmod),
							BoolGetDatum(false));
}

/*
 * Convert a value from one type to another with the assignment cast,
 * checking privileges on the cast function as the SQL command would.  The
 * result might be the same datum as the argument.  Raises PG errors.
 */
static Datum
dt_cast_value(Oid srctype, Oid dsttype, Datum value)
{
	Oid			funcid = InvalidOid;
	CoercionPathType path;

	if (srctype == dsttype)
		return value;

	path = find_coercion_pathway(dsttype, srctype, COERCION_ASSIGNMENT,
								 &funcid);
	switch (path)
	{
		case COERCION_PATH_RELABELTYPE:
			return value;
		case COERCION_PATH_FUNC:
			{
				LOCAL_FCINFO(fcinfo, 3);
				FmgrInfo	flinfo;
				int			nargs = get_func_nargs(funcid);
				Datum		result;

				dt_check_execute(funcid);
				fmgr_info(funcid, &flinfo);
				InitFunctionCallInfoData(*fcinfo, &flinfo, nargs, InvalidOid,
										 NULL, NULL);
				fcinfo->args[0].value = value;
				fcinfo->args[0].isnull = false;
				if (nargs > 1)
				{
					fcinfo->args[1].value = Int32GetDatum(-1);
					fcinfo->args[1].isnull = false;
				}
				if (nargs > 2)
				{
					fcinfo->args[2].value = BoolGetDatum(false);
					fcinfo->args[2].isnull = false;
				}
				result = FunctionCallInvoke(fcinfo);
				if (fcinfo->isnull)
					ereport(ERROR,
							(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
							 errmsg("cast function returned null")));
				return result;
			}
		case COERCION_PATH_COERCEVIAIO:
			{
				Oid			outfunc;
				Oid			infunc;
				Oid			ioparam;
				bool		isvarlena;
				char	   *str;

				getTypeOutputInfo(srctype, &outfunc, &isvarlena);
				getTypeInputInfo(dsttype, &infunc, &ioparam);
				str = OidOutputFunctionCall(outfunc, value);
				return OidInputFunctionCall(infunc, str, ioparam, -1);
			}
		case COERCION_PATH_ARRAYCOERCE:
			{
				dt_elemcast ec;

				ec.srctype = get_element_type(srctype);
				ec.dsttype = get_element_type(dsttype);
				return dt_array_map(value, ec.dsttype, dt_elemcast_cast, &ec);
			}
		default:
			ereport(ERROR,
					(errcode(ERRCODE_CANNOT_COERCE),
					 errmsg("cannot cast type %s to %s",
							format_type_be(srctype),
							format_type_be(dsttype))));
	}
	return (Datum) 0;			/* not reached */
}

/*
 * Apply a type modifier to a value of the (base) type, using its length
 * coercion function if it has one.  Raises PG errors.
 */
static Datum
dt_apply_typmod(Oid basetype, int32 typmod, Datum value)
{
	Oid			funcid = InvalidOid;
	CoercionPathType path;

	if (typmod < 0)
		return value;

	path = find_typmod_coercion_function(basetype, &funcid);
	if (path == COERCION_PATH_FUNC)
	{
		dt_check_execute(funcid);
		return OidFunctionCall3(funcid, value, Int32GetDatum(typmod),
								BoolGetDatum(false));
	}
	if (path == COERCION_PATH_ARRAYCOERCE)
	{
		dt_elemcast ec;
		ArrayType  *arr = DatumGetArrayTypeP(value);

		dt_check_execute(funcid);
		ec.funcid = funcid;
		ec.typmod = typmod;
		return dt_array_map(value, ARR_ELEMTYPE(arr), dt_elemcast_typmod, &ec);
	}
	return value;
}

/* Is there an assignment cast from one type to the other? */
static bool
dt_cast_exists(lua_State * L, pll_typeinfo *src, pll_typeinfo *dst)
{
	bool		result = false;

	PLL_TRY();
	{
		Oid			funcid;
		CoercionPathType path;

		path = find_coercion_pathway(dst->basetypoid, src->basetypoid,
									 COERCION_ASSIGNMENT, &funcid);
		result = (path != COERCION_PATH_NONE &&
				  path != COERCION_PATH_ARRAYCOERCE);
	}
	PLL_CATCH(L);
	return result;
}

/*
 * Final steps of conversion: apply the type modifier (length coercion), and
 * check domain constraints.  The value may be null.
 */
static Datum
dt_finish(lua_State * L, Datum value, pll_typeinfo *ti)
{
	int32		typmod = ti->typmod >= 0 ? ti->typmod : ti->basetypmod;

	if (typmod < 0 && ti->typoid == ti->basetypoid)
		return value;

	PLL_TRY();
	{
		value = dt_apply_typmod(ti->basetypoid, typmod, value);
		if (ti->typoid != ti->basetypoid)
			domain_check(value, false, ti->typoid, NULL, CurrentMemoryContext);
	}
	PLL_CATCH(L);
	return value;
}

/* Lua integer value of the number at idx, which must be integral */
static lua_Integer
dt_tointeger(lua_State * L, int idx, const char *what)
{
	lua_Integer i = 0;

	if (lua_isinteger(L, idx))
		return lua_tointeger(L, idx);
	else
	{
		lua_Number	n = lua_tonumber(L, idx);

		/* check the range first: an out-of-range conversion is undefined */
		bool		inrange = (n >= (lua_Number) LUA_MININTEGER &&
							   n < -(lua_Number) LUA_MININTEGER);

		if (inrange)
			i = (lua_Integer) n;
		if (!inrange || (lua_Number) i != n)
			luaL_error(L, "number has no exact integer representation, cannot be converted to %s",
					   what);
	}
	return i;
}

/* Convert a string, with an input function (never for bytea) */
static Datum
dt_input(lua_State * L, const char *s, pll_typeinfo *ti)
{
	Datum		result = (Datum) 0;

	PLL_TRY();
	{
		ti_io(ti);

		/*
		 * The type modifier is applied afterwards by dt_finish, using the
		 * length coercion function, which needs a privilege check; so don't
		 * let the input function apply it.
		 */
		result = OidInputFunctionCall(ti->typinput, unconstify(char *, s),
									  ti->typioparam, -1);
	}
	PLL_CATCH(L);
	return result;
}

static Datum
dt_string_to_datum(lua_State * L, int idx, pll_typeinfo *ti)
{
	size_t		len;
	const char *s = lua_tolstring(L, idx, &len);
	Datum		result = (Datum) 0;

	if (ti->native == PLL_NAT_BYTEA)
	{
		PLL_TRY();
		{
			bytea	   *b = (bytea *) palloc(VARHDRSZ + len);

			SET_VARSIZE(b, VARHDRSZ + len);
			memcpy(VARDATA(b), s, len);
			result = PointerGetDatum(b);
		}
		PLL_CATCH(L);
		return result;
	}

	if (ti->native == PLL_NAT_BOOL)
	{
		if (strcmp(s, "true") == 0 || strcmp(s, "t") == 0 ||
			strcmp(s, "1") == 0)
			return BoolGetDatum(true);
		if (strcmp(s, "false") == 0 || strcmp(s, "f") == 0 ||
			strcmp(s, "0") == 0)
			return BoolGetDatum(false);
		luaL_error(L, "invalid string value for type boolean: \"%s\"", s);
	}

	if (strlen(s) != len)
		luaL_error(L, "a string with an embedded zero byte cannot be converted to this type");

	return dt_input(L, s, ti);
}

static Datum
dt_number_to_datum(lua_State * L, int idx, pll_typeinfo *ti)
{
	Datum		result = (Datum) 0;

	switch (ti->native)
	{
		case PLL_NAT_INT:
			{
				lua_Integer i = dt_tointeger(L, idx,
											 ti->basetypoid == INT2OID ? "smallint" :
											 ti->basetypoid == INT4OID ? "integer" :
											 ti->basetypoid == INT8OID ? "bigint" : "oid");

				switch (ti->basetypoid)
				{
					case INT2OID:
						if (i < PG_INT16_MIN || i > PG_INT16_MAX)
							luaL_error(L, "value out of range for type smallint");
						return Int16GetDatum((int16) i);
					case INT4OID:
						if (i < PG_INT32_MIN || i > PG_INT32_MAX)
							luaL_error(L, "value out of range for type integer");
						return Int32GetDatum((int32) i);
					case INT8OID:
						return Int64GetDatum(i);
					default:
						if (i < 0 || i > (lua_Integer) PG_UINT32_MAX)
							luaL_error(L, "value out of range for type oid");
						return ObjectIdGetDatum((Oid) i);
				}
			}
		case PLL_NAT_FLOAT:
			{
				lua_Number	n = lua_tonumber(L, idx);

				if (ti->basetypoid == FLOAT4OID)
				{
					float4		f = (float4) n;

					if (isinf(f) && !isinf(n))
						luaL_error(L, "value out of range for type real");
					return Float4GetDatum(f);
				}
				return Float8GetDatum((float8) n);
			}
		case PLL_NAT_BOOL:
			{
				lua_Number	n = lua_tonumber(L, idx);

				if (n == 0)
					return BoolGetDatum(false);
				if (n == 1)
					return BoolGetDatum(true);
				luaL_error(L, "only 0 and 1 can be converted to type boolean");
			}
			break;
		case PLL_NAT_TEXT:
		case PLL_NAT_IO:
			{
				const char *s;

				lua_pushvalue(L, idx);
				s = lua_tostring(L, -1);	/* converts the copy */
				result = dt_input(L, s, ti);
				lua_pop(L, 1);
				return result;
			}
		default:
			break;
	}

	if (ti->kind == PLL_KIND_NUMERIC)
	{
		bool		isint = lua_isinteger(L, idx);
		lua_Integer i = isint ? lua_tointeger(L, idx) : 0;
		lua_Number	n = isint ? 0 : lua_tonumber(L, idx);

		PLL_TRY();
		{
			if (isint)
				result = DirectFunctionCall1(int8_numeric, Int64GetDatum(i));
			else
				result = DirectFunctionCall1(float8_numeric, Float8GetDatum(n));
		}
		PLL_CATCH(L);
		return result;
	}

	dt_error_noconv(L, idx, ti);
	return (Datum) 0;
}

/* Is the value an object that can be indexed by column name? */
static bool
dt_indexable(lua_State * L, int idx)
{
	return lua_type(L, idx) == LUA_TTABLE || pll_datum_test(L, idx) != NULL;
}

/*
 * Convert a Lua value to a Datum of the type, without type modifier
 * coercion or domain checking.  The value is not nil.
 */
static Datum
dt_to_datum(lua_State * L, int idx, pll_typeinfo *ti)
{
	pll_datum  *d = pll_datum_test(L, idx);
	int			t = lua_type(L, idx);

	if (d != NULL)
	{
		if (dt_same_type(d->ti, ti))
			return dt_copy_datum(L, ti, d->value);
		if (ti->kind == PLL_KIND_JSONB)
			return pll_jsonb_todatum(L, idx, 0);
		if (ti->kind == PLL_KIND_ARRAY && d->ti->basetypoid == ti->elemtypoid)
			return dt_array_construct(L, ti, idx, 1, true);
		if (ti->kind == PLL_KIND_COMPOSITE &&
			d->ti->kind == PLL_KIND_COMPOSITE &&
			!dt_cast_exists(L, d->ti, ti))
			return dt_composite_from_indexable(L, idx, ti);
		return dt_cast_datum(L, d->ti, d->value, ti);
	}

	/* a cursor object converts to refcursor */
	if (ti->native == PLL_NAT_CURSOR && t == LUA_TUSERDATA)
	{
		const char *name = pll_cursor_name(L, idx);

		if (name != NULL)
		{
			Datum		result = (Datum) 0;

			PLL_TRY();
			{
				result = CStringGetTextDatum(name);
			}
			PLL_CATCH(L);
			return result;
		}
	}

	/* a transform takes anything but a string */
	if (t != LUA_TSTRING && pll_call_trftypes() != NIL)
	{
		Oid			fn = dt_transform_func(L, ti, false);

		if (OidIsValid(fn))
		{
			bool		isnull = false;
			Datum		result;

			result = dt_call_transform(L, fn, Int32GetDatum(idx), &isnull);
			if (isnull)
				luaL_error(L, "transform function returned null");
			return result;
		}
	}

	if (ti->kind == PLL_KIND_JSONB)
		return pll_jsonb_todatum(L, idx, 0);

	switch (t)
	{
		case LUA_TBOOLEAN:
			if (ti->native == PLL_NAT_BOOL)
				return BoolGetDatum(lua_toboolean(L, idx));
			if (ti->kind == PLL_KIND_ARRAY)
				return dt_array_construct(L, ti, idx, 1, true);
			break;
		case LUA_TSTRING:
			return dt_string_to_datum(L, idx, ti);
		case LUA_TNUMBER:
			if (ti->kind == PLL_KIND_ARRAY)
				return dt_array_construct(L, ti, idx, 1, true);
			return dt_number_to_datum(L, idx, ti);
		case LUA_TTABLE:
			if (ti->kind == PLL_KIND_COMPOSITE)
				return dt_composite_from_indexable(L, idx, ti);
			if (ti->kind == PLL_KIND_ARRAY)
				return dt_array_construct(L, ti, idx, 1, false);
			if (ti->kind == PLL_KIND_TIME)
				return pll_time_todatum(L, idx, ti);
			break;
		default:
			break;
	}

	dt_error_noconv(L, idx, ti);
	return (Datum) 0;
}

Datum
pll_value_todatum(lua_State * L, int idx, pll_typeinfo *ti, bool *isnull)
{
	Datum		result;

	idx = lua_absindex(L, idx);
	luaL_checkstack(L, 8, "out of stack space");
	ti_current(L, ti);

	if (lua_isnil(L, idx))
	{
		*isnull = true;
		if (ti->typoid != ti->basetypoid)
		{
			PLL_TRY();
			{
				domain_check((Datum) 0, true, ti->typoid, NULL,
							 CurrentMemoryContext);
			}
			PLL_CATCH(L);
		}
		return (Datum) 0;
	}

	*isnull = false;
	result = dt_to_datum(L, idx, ti);
	return dt_finish(L, result, ti);
}

/* ------------------------------------------------------------------------
 * Composite values
 * ------------------------------------------------------------------------
 */

static HeapTupleHeader
dt_row_header(pll_datum *d)
{
	return (HeapTupleHeader) DatumGetPointer(d->value);
}

/* The attribute number of the named column, or 0 */
static int
dt_attno_by_name(TupleDesc td, const char *name)
{
	int			i;

	for (i = 0; i < td->natts; i++)
	{
		Form_pg_attribute att = TupleDescAttr(td, i);

		if (!att->attisdropped && strcmp(NameStr(att->attname), name) == 0)
			return i + 1;
	}
	return 0;
}

/* Push the value of a column of a row datum */
static void
dt_row_push_attr(lua_State * L, int rowidx, pll_datum *d, TupleDesc td,
				 int attno)
{
	Form_pg_attribute att = TupleDescAttr(td, attno - 1);
	HeapTupleData tup;
	Datum		v = (Datum) 0;
	bool		isnull = true;
	pll_typeinfo *cti;

	if (att->attisdropped)
	{
		lua_pushnil(L);
		return;
	}

	tup.t_len = HeapTupleHeaderGetDatumLength(dt_row_header(d));
	ItemPointerSetInvalid(&tup.t_self);
	tup.t_tableOid = InvalidOid;
	tup.t_data = dt_row_header(d);

	PLL_TRY();
	{
		v = heap_getattr(&tup, attno, td, &isnull);
	}
	PLL_CATCH(L);

	cti = pll_typeinfo_push(L, att->atttypid, att->atttypmod);
	pll_value_push(L, v, isnull, cti);
	lua_remove(L, -2);			/* the typeinfo */

	lua_pushinteger(L, attno);
	dt_link_parent(L, lua_gettop(L) - 1, lua_absindex(L, rowidx));
}

/*
 * Link the datum object at childidx to the parent at parentidx, if it is a
 * composite or array value.  The key identifying the child's place in the
 * parent is on top of the stack, and is popped.
 */
static void
dt_link_parent(lua_State * L, int childidx, int parentidx)
{
	pll_datum  *c = pll_datum_test(L, childidx);

	if (c == NULL ||
		(c->ti->kind != PLL_KIND_COMPOSITE && c->ti->kind != PLL_KIND_ARRAY))
	{
		lua_pop(L, 1);
		return;
	}
	lua_setiuservalue(L, childidx, DT_UV_KEY);
	lua_pushvalue(L, parentidx);
	lua_setiuservalue(L, childidx, DT_UV_PARENT);
}

/*
 * The datum at idx has been modified: if it is the child of another datum,
 * store the new value in the parent, and so on up.
 */
static void
dt_propagate(lua_State * L, int idx)
{
	pll_datum  *d;
	pll_datum  *pd;
	int			parent;
	int			key;

	idx = lua_absindex(L, idx);
	d = dt_check_datum(L, idx);
	luaL_checkstack(L, 6, "out of stack space");

	if (lua_getiuservalue(L, idx, DT_UV_PARENT) != LUA_TUSERDATA)
	{
		lua_pop(L, 1);
		return;
	}
	parent = lua_gettop(L);
	lua_getiuservalue(L, idx, DT_UV_KEY);
	key = lua_gettop(L);
	pd = dt_check_datum(L, parent);

	if (pd->ti->kind == PLL_KIND_COMPOSITE)
		dt_row_set_attr(L, parent, (int) lua_tointeger(L, key), d->value,
						false);
	else
	{
		int			indx[MAXDIM];
		int			n = (int) lua_rawlen(L, key);
		int			i;

		for (i = 0; i < n && i < MAXDIM; i++)
		{
			lua_rawgeti(L, key, i + 1);
			indx[i] = (int) lua_tointeger(L, -1);
			lua_pop(L, 1);
		}
		dt_array_set(L, parent, n, indx, d->value, false);
	}
	lua_settop(L, parent - 1);
}

/* Set a column of the row datum at rowidx, and propagate the change */
static void
dt_row_set_attr(lua_State * L, int rowidx, int attno, Datum val, bool isnull)
{
	pll_datum  *d = dt_check_datum(L, rowidx);
	TupleDesc	td = pll_typeinfo_tupdesc(L, d->ti);
	Datum		result = (Datum) 0;

	PLL_TRY();
	{
		HeapTupleData tup;
		Datum	   *values = palloc(sizeof(Datum) * td->natts);
		bool	   *nulls = palloc(sizeof(bool) * td->natts);

		tup.t_len = HeapTupleHeaderGetDatumLength(dt_row_header(d));
		ItemPointerSetInvalid(&tup.t_self);
		tup.t_tableOid = InvalidOid;
		tup.t_data = dt_row_header(d);
		heap_deform_tuple(&tup, td, values, nulls);
		values[attno - 1] = val;
		nulls[attno - 1] = isnull;
		result = HeapTupleGetDatum(heap_form_tuple(td, values, nulls));
	}
	PLL_CATCH(L);

	dt_datum_replace(L, rowidx, result);
	dt_propagate(L, rowidx);
}

/* Set an element of the array datum at arridx, and propagate the change */
static void
dt_array_set(lua_State * L, int arridx, int nsub, int *indx, Datum val,
			 bool isnull)
{
	pll_datum  *d = dt_check_datum(L, arridx);
	Datum		result = (Datum) 0;

	PLL_TRY();
	{
		result = array_set_element(d->value, nsub, indx, val, isnull, -1,
								   d->ti->elmlen, d->ti->elmbyval,
								   d->ti->elmalign);
	}
	PLL_CATCH(L);

	dt_datum_replace(L, arridx, result);
	dt_propagate(L, arridx);
}

/*
 * Convert the table or datum at idx to a value of the composite type, by
 * column name.
 */
static Datum
dt_composite_from_indexable(lua_State * L, int idx, pll_typeinfo *ti)
{
	TupleDesc	td = dt_tupdesc_snapshot(L, ti);
	int			natts = td->natts;
	Datum	   *values;
	bool	   *nulls;
	Datum		result = (Datum) 0;
	int			i;

	idx = lua_absindex(L, idx);
	luaL_checkstack(L, 8, "out of stack space");
	values = (Datum *) lua_newuserdatauv(L, natts * (sizeof(Datum) + sizeof(bool)) + 1, 0);
	nulls = (bool *) (values + natts);

	for (i = 0; i < natts; i++)
	{
		Form_pg_attribute att = TupleDescAttr(td, i);
		pll_typeinfo *cti;

		if (att->attisdropped)
		{
			values[i] = (Datum) 0;
			nulls[i] = true;
			continue;
		}

		lua_pushstring(L, NameStr(att->attname));
		lua_gettable(L, idx);
		cti = pll_typeinfo_push(L, att->atttypid, att->atttypmod);
		values[i] = pll_value_todatum(L, -2, cti, &nulls[i]);
		lua_pop(L, 2);
	}

	PLL_TRY();
	{
		HeapTuple	tup = heap_form_tuple(td, values, nulls);

		result = HeapTupleGetDatum(tup);
	}
	PLL_CATCH(L);
	lua_pop(L, 1);				/* the scratch space */
	return result;
}

Datum
pll_values_tocomposite(lua_State * L, int idx, int n, pll_typeinfo *rowti)
{
	TupleDesc	td = dt_tupdesc_snapshot(L, rowti);
	int			natts = td->natts;
	Datum	   *values;
	bool	   *nulls;
	Datum		result = (Datum) 0;
	int			i;
	int			k = 0;

	idx = lua_absindex(L, idx);
	luaL_checkstack(L, 8, "out of stack space");
	values = (Datum *) lua_newuserdatauv(L, natts * (sizeof(Datum) + sizeof(bool)) + 1, 0);
	nulls = (bool *) (values + natts);

	for (i = 0; i < natts; i++)
	{
		Form_pg_attribute att = TupleDescAttr(td, i);
		pll_typeinfo *cti;

		if (att->attisdropped)
		{
			values[i] = (Datum) 0;
			nulls[i] = true;
			continue;
		}
		if (k >= n)
			luaL_error(L, "too few values for a row of this type");

		cti = pll_typeinfo_push(L, att->atttypid, att->atttypmod);
		values[i] = pll_value_todatum(L, idx + k, cti, &nulls[i]);
		lua_pop(L, 1);
		k++;
	}
	if (k != n)
		luaL_error(L, "too many values for a row of this type");

	PLL_TRY();
	{
		HeapTuple	tup = heap_form_tuple(td, values, nulls);

		result = HeapTupleGetDatum(tup);
	}
	PLL_CATCH(L);
	lua_pop(L, 1);
	return result;
}

/* row[key] */
static int
row_index_cfunc(lua_State * L)
{
	pll_datum  *d = dt_check_datum(L, 1);
	TupleDesc	td = pll_typeinfo_tupdesc(L, d->ti);
	int			attno = 0;

	if (lua_type(L, 2) == LUA_TSTRING)
		attno = dt_attno_by_name(td, lua_tostring(L, 2));
	else if (lua_isinteger(L, 2))
	{
		lua_Integer i = lua_tointeger(L, 2);

		if (i >= 1 && i <= td->natts)
			attno = (int) i;
	}

	if (attno == 0)
	{
		lua_pushnil(L);
		return 1;
	}
	dt_row_push_attr(L, 1, d, td, attno);
	return 1;
}

/* #row: the number of columns, not counting dropped ones */
static int
row_len_cfunc(lua_State * L)
{
	pll_datum  *d = dt_check_datum(L, 1);
	TupleDesc	td = pll_typeinfo_tupdesc(L, d->ti);
	int			n = 0;
	int			i;

	for (i = 0; i < td->natts; i++)
	{
		if (!TupleDescAttr(td, i)->attisdropped)
			n++;
	}
	lua_pushinteger(L, n);
	return 1;
}

/* row[key] = value */
static int
row_newindex_cfunc(lua_State * L)
{
	pll_datum  *d = dt_check_datum(L, 1);
	TupleDesc	td = pll_typeinfo_tupdesc(L, d->ti);
	int			attno = 0;
	Form_pg_attribute att;
	pll_typeinfo *cti;
	Datum		newval;
	bool		newnull;

	if (lua_type(L, 2) == LUA_TSTRING)
		attno = dt_attno_by_name(td, lua_tostring(L, 2));
	else if (lua_isinteger(L, 2))
	{
		lua_Integer i = lua_tointeger(L, 2);

		if (i >= 1 && i <= td->natts && !TupleDescAttr(td, i - 1)->attisdropped)
			attno = (int) i;
	}
	if (attno == 0)
		return luaL_error(L, "row has no such column");

	att = TupleDescAttr(td, attno - 1);
	cti = pll_typeinfo_push(L, att->atttypid, att->atttypmod);
	newval = pll_value_todatum(L, 3, cti, &newnull);
	lua_pop(L, 1);

	dt_row_set_attr(L, 1, attno, newval, newnull);
	return 0;
}

/* The iterator for pairs(row): (row, previous name) -> name, value, attno */
static int
row_next_cfunc(lua_State * L)
{
	pll_datum  *d = dt_check_datum(L, 1);
	TupleDesc	td = pll_typeinfo_tupdesc(L, d->ti);
	int			attno = 0;
	int			i;

	if (!lua_isnil(L, 2))
		attno = dt_attno_by_name(td, luaL_checkstring(L, 2));

	for (i = attno; i < td->natts; i++)
	{
		if (!TupleDescAttr(td, i)->attisdropped)
		{
			lua_pushstring(L, NameStr(TupleDescAttr(td, i)->attname));
			dt_row_push_attr(L, 1, d, td, i + 1);
			lua_pushinteger(L, i + 1);
			return 3;
		}
	}
	lua_pushnil(L);
	return 1;
}

static int
row_pairs_cfunc(lua_State * L)
{
	(void) dt_check_datum(L, 1);
	lua_pushcfunction(L, row_next_cfunc);
	lua_pushvalue(L, 1);
	lua_pushnil(L);
	return 3;
}

/*
 * Parse the option argument of a conversion to table.  Pushes two values:
 * the map function (or nil) and the null replacement; their indexes are
 * stored in opts.
 */
static void
dt_parse_opts(lua_State * L, int idx, dt_opts *opts)
{
	idx = lua_absindex(L, idx);
	opts->discard = false;
	lua_pushnil(L);
	opts->mapidx = lua_gettop(L);
	lua_pushnil(L);
	opts->nullidx = lua_gettop(L);

	if (lua_isnone(L, idx))
		return;

	if (lua_type(L, idx) == LUA_TFUNCTION)
	{
		lua_pushvalue(L, idx);
		lua_copy(L, -1, opts->mapidx);
		lua_pop(L, 1);
	}
	else if (lua_type(L, idx) == LUA_TTABLE)
	{
		lua_getfield(L, idx, "map");
		lua_copy(L, -1, opts->mapidx);
		lua_pop(L, 1);
		lua_getfield(L, idx, "null");
		lua_copy(L, -1, opts->nullidx);
		lua_pop(L, 1);
		lua_getfield(L, idx, "discard");
		opts->discard = lua_toboolean(L, -1);
		lua_pop(L, 1);
	}
	else
	{
		lua_pushvalue(L, idx);
		lua_copy(L, -1, opts->nullidx);
		lua_pop(L, 1);
	}
}

/* row{ map = ..., null = ..., discard = ... } */
static int
row_call_cfunc(lua_State * L)
{
	pll_datum  *d = dt_check_datum(L, 1);
	TupleDesc	td = dt_tupdesc_snapshot(L, d->ti);
	dt_opts		opts;
	int			result;
	int			i;

	luaL_checkstack(L, 12, "out of stack space");
	dt_parse_opts(L, 2, &opts);
	lua_newtable(L);
	result = lua_gettop(L);

	for (i = 0; i < td->natts; i++)
	{
		Form_pg_attribute att = TupleDescAttr(td, i);
		const char *name = NameStr(att->attname);

		if (att->attisdropped)
			continue;

		dt_row_push_attr(L, 1, d, td, i + 1);
		if (lua_isnil(L, -1))
		{
			lua_pop(L, 1);
			lua_pushvalue(L, opts.nullidx);
		}

		if (!lua_isnil(L, opts.mapidx))
		{
			lua_pushvalue(L, opts.mapidx);
			lua_pushstring(L, name);
			lua_pushvalue(L, -3);
			lua_pushinteger(L, i + 1);
			lua_pushvalue(L, 1);
			lua_call(L, 4, 1);
			lua_remove(L, -2);	/* the original value */
		}

		if (opts.discard || lua_isnil(L, -1))
			lua_pop(L, 1);
		else
			lua_setfield(L, result, name);
	}

	if (opts.discard)
		return 0;
	lua_pushvalue(L, result);
	return 1;
}

/* ------------------------------------------------------------------------
 * Arrays
 * ------------------------------------------------------------------------
 */

/*
 * Fill in the elements for dimension 'level' onwards, from the table at
 * index tbl; elements go into elems/nulls starting at pos.  Returns the
 * next position.
 */
static int
dt_array_fill(lua_State * L, int tbl, int level, int ndims, int *dims,
			  Datum *elems, bool *nulls, int pos, pll_typeinfo *eti)
{
	int			i;

	for (i = 1; i <= dims[level]; i++)
	{
		lua_geti(L, tbl, i);
		if (level == ndims - 1)
		{
			elems[pos] = pll_value_todatum(L, -1, eti, &nulls[pos]);
			pos++;
		}
		else if (lua_isnil(L, -1))
		{
			int			n = 1;
			int			k;

			for (k = level + 1; k < ndims; k++)
				n *= dims[k];
			for (k = 0; k < n; k++)
			{
				elems[pos] = (Datum) 0;
				nulls[pos] = true;
				pos++;
			}
		}
		else if (dt_indexable(L, -1))
			pos = dt_array_fill(L, lua_gettop(L), level + 1, ndims, dims,
								elems, nulls, pos, eti);
		else
			luaL_error(L, "a value of type %s cannot supply the elements of an array dimension",
					   luaL_typename(L, -1));
		lua_pop(L, 1);
	}
	return pos;
}

/*
 * Build an array of the type from constructor arguments (first .. first +
 * nargs - 1; if as_list, the arguments are always the elements):
 *	  none				an empty array
 *	  table, n1, n2...	a multidimensional array, populated by indexing
 *	  table				one-dimensional, as large as the largest integer key
 *	  anything else		one-dimensional, of the arguments
 */
static Datum
dt_array_construct(lua_State * L, pll_typeinfo *ti, int first, int nargs,
				   bool as_list)
{
	int			dims[MAXDIM];
	int			lbs[MAXDIM];
	int			ndims = 1;
	int			i;
	int64		nelems = 1;
	Datum	   *elems;
	bool	   *nulls;
	pll_typeinfo *eti;
	Datum		result = (Datum) 0;
	int			scratch;
	bool		from_table;

	first = lua_absindex(L, first);
	luaL_checkstack(L, 12, "out of stack space");

	if (nargs == 0)
	{
		PLL_TRY();
		{
			result = PointerGetDatum(construct_empty_array(ti->elemtypoid));
		}
		PLL_CATCH(L);
		return result;
	}

	from_table = !as_list && lua_type(L, first) == LUA_TTABLE;

	if (from_table && nargs >= 2)
	{
		ndims = nargs - 1;
		if (ndims > MAXDIM)
			luaL_error(L, "too many array dimensions");
		for (i = 0; i < ndims; i++)
		{
			lua_Integer n = dt_tointeger(L, first + 1 + i, "an array dimension");

			if (n < 0 || n > MaxArraySize)
				luaL_error(L, "array dimension out of range");
			dims[i] = (int) n;
		}
	}
	else if (from_table)
	{
		lua_Integer maxkey = 0;

		lua_pushnil(L);
		while (lua_next(L, first))
		{
			if (lua_isinteger(L, -2) && lua_tointeger(L, -2) > maxkey)
				maxkey = lua_tointeger(L, -2);
			lua_pop(L, 1);
		}
		if (maxkey > MaxArraySize)
			luaL_error(L, "array too large");
		dims[0] = (int) maxkey;
	}
	else
		dims[0] = nargs;

	for (i = 0; i < ndims; i++)
	{
		nelems *= dims[i];
		lbs[i] = 1;
		if (nelems > MaxArraySize)
			luaL_error(L, "array too large");
	}

	if (nelems == 0)
	{
		PLL_TRY();
		{
			result = PointerGetDatum(construct_empty_array(ti->elemtypoid));
		}
		PLL_CATCH(L);
		return result;
	}

	elems = (Datum *) lua_newuserdatauv(L, nelems * (sizeof(Datum) + sizeof(bool)), 0);
	nulls = (bool *) (elems + nelems);
	scratch = lua_gettop(L);
	eti = dt_element_ti(L, ti);

	if (from_table)
		(void) dt_array_fill(L, first, 0, ndims, dims, elems, nulls, 0, eti);
	else
	{
		for (i = 0; i < nargs; i++)
			elems[i] = pll_value_todatum(L, first + i, eti, &nulls[i]);
	}

	PLL_TRY();
	{
		result = PointerGetDatum(construct_md_array(elems, nulls, ndims, dims,
													lbs, ti->elemtypoid,
													eti->typlen, eti->typbyval,
													eti->typalign));
	}
	PLL_CATCH(L);
	lua_settop(L, scratch - 1);
	return result;
}

/* The registry key of the table holding the cached element offsets */
static const char dt_offsets_key = 0;

/* Push the table of cached element offsets: weak keys, the datum objects */
static void
dt_push_offsets_table(lua_State * L)
{
	if (lua_rawgetp(L, LUA_REGISTRYINDEX, &dt_offsets_key) != LUA_TTABLE)
	{
		lua_pop(L, 1);
		lua_newtable(L);
		lua_createtable(L, 0, 1);
		lua_pushstring(L, "k");
		lua_setfield(L, -2, "__mode");
		lua_setmetatable(L, -2);
		lua_pushvalue(L, -1);
		lua_rawsetp(L, LUA_REGISTRYINDEX, &dt_offsets_key);
	}
}

/* Forget the element offsets of the datum object at idx, which has changed */
static void
dt_offsets_forget(lua_State * L, int idx)
{
	idx = lua_absindex(L, idx);
	luaL_checkstack(L, 4, "out of stack space");
	dt_push_offsets_table(L);
	lua_pushvalue(L, idx);
	lua_pushnil(L);
	lua_rawset(L, -3);
	lua_pop(L, 1);
}

/*
 * Push, and return the contents of, the offsets of the elements of the
 * one-dimensional array 'arr', which is the value of the datum object at
 * arridx, from the start of the data: PG_UINT32_MAX for a null element.  The
 * index is made on the first call and kept until the object changes.
 */
static const uint32 *
dt_array_offsets(lua_State * L, int arridx, ArrayType *arr, pll_typeinfo *ti)
{
	int			nelems = ARR_DIMS(arr)[0];
	uint32	   *offs;
	char	   *base;
	char	   *p;
	uint8	   *bitmap;
	int			i;

	arridx = lua_absindex(L, arridx);
	luaL_checkstack(L, 6, "out of stack space");
	dt_push_offsets_table(L);
	lua_pushvalue(L, arridx);
	if (lua_rawget(L, -2) == LUA_TUSERDATA)
	{
		lua_remove(L, -2);
		return (const uint32 *) lua_touserdata(L, -1);
	}
	lua_pop(L, 1);

	offs = (uint32 *) lua_newuserdatauv(L, sizeof(uint32) * Max(nelems, 1), 0);
	base = ARR_DATA_PTR(arr);
	p = base;
	bitmap = ARR_NULLBITMAP(arr);
	for (i = 0; i < nelems; i++)
	{
		if (bitmap != NULL && (bitmap[i >> 3] & (1 << (i & 7))) == 0)
		{
			offs[i] = PG_UINT32_MAX;
			continue;
		}
		offs[i] = (uint32) (p - base);
		p = att_addlength_pointer(p, ti->elmlen, p);
		p = (char *) att_align_nominal(p, ti->elmalign);
	}

	lua_pushvalue(L, arridx);
	lua_pushvalue(L, -2);
	lua_rawset(L, -4);			/* the table */
	lua_remove(L, -2);
	return offs;
}

/* Push element nsub-indexed from the array datum */
static void
dt_array_get(lua_State * L, int arridx, pll_datum *d, int nsub, int *indx,
			 pll_typeinfo *ti)
{
	Datum		v = (Datum) 0;
	bool		isnull = true;
	pll_typeinfo *eti;
	ArrayType  *arr = DatumGetArrayTypeP(d->value);
	int			i;

	if (ti->elmlen == -1 && nsub == 1 && ARR_NDIM(arr) == 1)
	{
		/*
		 * Finding an element of an array with variable-length elements means
		 * stepping over all those before it, so go through an index of the
		 * element positions, made once per datum object.
		 */
		const uint32 *offs = dt_array_offsets(L, arridx, arr, ti);
		int			pos = indx[0] - ARR_LBOUND(arr)[0];

		if (pos >= 0 && pos < ARR_DIMS(arr)[0] && offs[pos] != PG_UINT32_MAX)
		{
			v = fetch_att(ARR_DATA_PTR(arr) + offs[pos], ti->elmbyval,
						  ti->elmlen);
			isnull = false;
		}
		lua_pop(L, 1);			/* the index */
	}
	else
	{
		PLL_TRY();
		{
			v = array_get_element(d->value, nsub, indx, -1, ti->elmlen,
								  ti->elmbyval, ti->elmalign, &isnull);
		}
		PLL_CATCH(L);
	}

	eti = dt_element_ti(L, ti);
	pll_value_push(L, v, isnull, eti);
	lua_remove(L, -2);

	/* an element that is itself composite writes through to the array */
	lua_createtable(L, nsub, 0);
	for (i = 0; i < nsub; i++)
	{
		lua_pushinteger(L, indx[i]);
		lua_rawseti(L, -2, i + 1);
	}
	dt_link_parent(L, lua_gettop(L) - 1, lua_absindex(L, arridx));
}

/*
 * Common code for a[i][j]...: index (or assign) with the subscript at stack
 * position 2 (and value at 3), where the object at 1 is an array datum or a
 * slice.
 */
static int
dt_array_access(lua_State * L, bool assign)
{
	pll_datum  *d;
	dt_slice	cur;
	ArrayType  *arr;
	int			ndim;
	int			root;
	bool		isslice = luaL_testudata(L, 1, PLL_MT_ARRAY_SLICE) != NULL;

	luaL_checkstack(L, 8, "out of stack space");
	if (isslice)
	{
		cur = *(dt_slice *) lua_touserdata(L, 1);
		lua_getiuservalue(L, 1, 1);
	}
	else
	{
		cur.nfixed = 0;
		lua_pushvalue(L, 1);
	}
	root = lua_gettop(L);		/* the array datum itself */
	d = dt_check_kind(L, root, PLL_KIND_ARRAY, "an array");

	if (!lua_isinteger(L, 2))
	{
		if (assign)
			return luaL_error(L, "array subscripts must be integers");
		lua_pushnil(L);
		return 1;
	}

	arr = DatumGetArrayTypeP(d->value);
	ndim = ARR_NDIM(arr);
	if (ndim == 0)
		ndim = 1;				/* an empty array can be extended */

	if (cur.nfixed >= MAXDIM)
		return luaL_error(L, "too many array subscripts");
	cur.idx[cur.nfixed] = (int) lua_tointeger(L, 2);
	if (lua_tointeger(L, 2) != cur.idx[cur.nfixed])
	{
		if (assign)
			return luaL_error(L, "array subscript out of range");
		lua_pushnil(L);
		return 1;
	}
	cur.nfixed++;

	if (cur.nfixed < ndim)
	{
		dt_slice   *ns;

		if (assign)
			return luaL_error(L, "too few subscripts to assign an array element");
		ns = (dt_slice *) lua_newuserdatauv(L, sizeof(dt_slice), 1);
		*ns = cur;
		lua_pushvalue(L, root);
		lua_setiuservalue(L, -2, 1);
		luaL_setmetatable(L, PLL_MT_ARRAY_SLICE);
		return 1;
	}

	if (cur.nfixed > ndim)
		return luaL_error(L, "too many array subscripts");

	if (!assign)
	{
		/* out of bounds reads give nil, as array_get_element does */
		dt_array_get(L, root, d, cur.nfixed, cur.idx, d->ti);
		return 1;
	}
	else
	{
		pll_typeinfo *eti = dt_element_ti(L, d->ti);
		bool		isnull;
		Datum		val = pll_value_todatum(L, 3, eti, &isnull);

		lua_pop(L, 1);
		dt_array_set(L, root, cur.nfixed, cur.idx, val, isnull);
		return 0;
	}
}

static int
array_index_cfunc(lua_State * L)
{
	return dt_array_access(L, false);
}

static int
array_newindex_cfunc(lua_State * L)
{
	return dt_array_access(L, true);
}

static int
slice_index_cfunc(lua_State * L)
{
	return dt_array_access(L, false);
}

static int
slice_newindex_cfunc(lua_State * L)
{
	return dt_array_access(L, true);
}

/* #a: the size of the first dimension */
static int
array_len_cfunc(lua_State * L)
{
	pll_datum  *d = dt_check_kind(L, 1, PLL_KIND_ARRAY, "an array");
	ArrayType  *arr = DatumGetArrayTypeP(d->value);

	lua_pushinteger(L, ARR_NDIM(arr) == 0 ? 0 : ARR_DIMS(arr)[0]);
	return 1;
}

/* #slice: the size of the next dimension */
static int
slice_len_cfunc(lua_State * L)
{
	dt_slice   *slice = (dt_slice *) luaL_checkudata(L, 1, PLL_MT_ARRAY_SLICE);
	pll_datum  *d;
	ArrayType  *arr;

	lua_getiuservalue(L, 1, 1);
	d = dt_check_kind(L, -1, PLL_KIND_ARRAY, "an array");
	arr = DatumGetArrayTypeP(d->value);
	lua_pushinteger(L, slice->nfixed < ARR_NDIM(arr) ?
					ARR_DIMS(arr)[slice->nfixed] : 0);
	return 1;
}

/*
 * iterator for pairs(array) and pairs(slice): (object, previous subscript)
 * -> subscript, value, where the value is an element or a further slice
 */
static int
array_next_cfunc(lua_State * L)
{
	pll_datum  *d;
	int			level = 0;
	ArrayType  *arr;
	int			next;

	if (luaL_testudata(L, 1, PLL_MT_ARRAY_SLICE))
	{
		level = ((dt_slice *) lua_touserdata(L, 1))->nfixed;
		lua_getiuservalue(L, 1, 1);
		d = dt_check_kind(L, -1, PLL_KIND_ARRAY, "an array");
		lua_pop(L, 1);
	}
	else
		d = dt_check_kind(L, 1, PLL_KIND_ARRAY, "an array");

	arr = DatumGetArrayTypeP(d->value);
	if (level >= ARR_NDIM(arr))
	{
		lua_pushnil(L);
		return 1;
	}

	if (lua_isnil(L, 2))
		next = ARR_LBOUND(arr)[level];
	else
		next = (int) luaL_checkinteger(L, 2) + 1;

	if (next < ARR_LBOUND(arr)[level] ||
		next >= ARR_LBOUND(arr)[level] + ARR_DIMS(arr)[level])
	{
		lua_pushnil(L);
		return 1;
	}

	/* reuse the logic of the index metamethod, with the new subscript */
	lua_pushinteger(L, next);
	lua_replace(L, 2);
	(void) dt_array_access(L, false);
	lua_pushinteger(L, next);
	lua_insert(L, -2);
	return 2;
}

static int
array_pairs_cfunc(lua_State * L)
{
	lua_pushcfunction(L, array_next_cfunc);
	lua_pushvalue(L, 1);
	lua_pushnil(L);
	return 3;
}

/*
 * Recursive part of arrayval{...}: for each position of dimension 'level',
 * produce the element (or a nested table) and store it into the table on top
 * of the stack.  *pos is the index into the deconstructed elements; sub[]
 * the current subscripts.
 */
static void
dt_array_call_level(lua_State * L, pll_datum *d, pll_typeinfo *eti,
					Datum *elems, bool *nulls, int level, int *pos, int *sub,
					dt_opts *opts, int arrayidx)
{
	ArrayType  *arr = DatumGetArrayTypeP(d->value);
	int			ndim = ARR_NDIM(arr);
	int			tbl = lua_gettop(L);
	int			i;

	for (i = 0; i < ARR_DIMS(arr)[level]; i++)
	{
		sub[level] = ARR_LBOUND(arr)[level] + i;
		if (level < ndim - 1)
		{
			lua_newtable(L);
			dt_array_call_level(L, d, eti, elems, nulls, level + 1, pos, sub,
								opts, arrayidx);
			if (opts->discard)
				lua_pop(L, 1);
			else
				lua_rawseti(L, tbl, i + 1);
		}
		else
		{
			int			j;

			pll_value_push(L, elems[*pos], nulls[*pos], eti);
			if (lua_isnil(L, -1))
			{
				lua_pop(L, 1);
				lua_pushvalue(L, opts->nullidx);
			}
			if (!lua_isnil(L, opts->mapidx))
			{
				lua_pushvalue(L, opts->mapidx);
				lua_pushvalue(L, -2);
				lua_pushvalue(L, arrayidx);
				for (j = 0; j < ndim; j++)
					lua_pushinteger(L, sub[j]);
				lua_call(L, 2 + ndim, 1);
				lua_remove(L, -2);
			}
			(*pos)++;
			if (opts->discard || lua_isnil(L, -1))
				lua_pop(L, 1);
			else
				lua_rawseti(L, tbl, i + 1);
		}
	}
}

/* array{ map = ..., null = ..., discard = ... } */
static int
array_call_cfunc(lua_State * L)
{
	pll_datum  *d = dt_check_kind(L, 1, PLL_KIND_ARRAY, "an array");
	ArrayType  *arr = DatumGetArrayTypeP(d->value);
	int			ndim = ARR_NDIM(arr);
	dt_opts		opts;
	Datum	   *elems = NULL;
	bool	   *nulls = NULL;
	int			nelems = 0;
	int			pos = 0;
	int			sub[MAXDIM];
	pll_typeinfo *eti;
	int			result;

	luaL_checkstack(L, 12 + 2 * ndim, "out of stack space");
	dt_parse_opts(L, 2, &opts);
	lua_newtable(L);
	result = lua_gettop(L);
	eti = dt_element_ti(L, d->ti);
	lua_pop(L, 1);

	if (ndim > 0)
	{
		PLL_TRY();
		{
			deconstruct_array(arr, d->ti->elemtypoid, eti->typlen,
							  eti->typbyval, eti->typalign, &elems, &nulls,
							  &nelems);
		}
		PLL_CATCH(L);
		dt_array_call_level(L, d, eti, elems, nulls, 0, &pos, sub, &opts, 1);
	}

	if (opts.discard)
		return 0;
	lua_pushvalue(L, result);
	return 1;
}

Datum
pll_array_from_values(lua_State * L, int idx, int n, pll_typeinfo *ti)
{
	if (ti->kind != PLL_KIND_ARRAY)
		luaL_error(L, "type is not an array type");
	return dt_array_construct(L, ti, idx, n, true);
}

/* ------------------------------------------------------------------------
 * Ranges
 * ------------------------------------------------------------------------
 */

static Datum
dt_range_construct(lua_State * L, pll_typeinfo *ti, int first, int nargs)
{
	Datum		result = (Datum) 0;
	pll_typeinfo *eti;
	Datum		lowval = (Datum) 0;
	Datum		upval = (Datum) 0;
	bool		lownull = true;
	bool		upnull = true;
	const char *flags = "[)";
	bool		empty = (nargs == 0);

	first = lua_absindex(L, first);
	luaL_checkstack(L, 8, "out of stack space");

	if (nargs == 1)
	{
		if (lua_type(L, first) == LUA_TSTRING || pll_datum_test(L, first))
		{
			bool		isnull;

			return pll_value_todatum(L, first, ti, &isnull);
		}
		luaL_error(L, "invalid argument for a range constructor");
	}
	if (nargs > 3)
		luaL_error(L, "too many arguments for a range constructor");

	if (!empty)
	{
		eti = dt_element_ti(L, ti);
		lowval = pll_value_todatum(L, first, eti, &lownull);
		upval = pll_value_todatum(L, first + 1, eti, &upnull);
		lua_pop(L, 1);
		if (nargs == 3)
		{
			flags = luaL_checkstring(L, first + 2);
			if (strlen(flags) != 2 || (flags[0] != '[' && flags[0] != '(') ||
				(flags[1] != ']' && flags[1] != ')'))
				luaL_error(L, "invalid range bound flags \"%s\"", flags);
		}
	}

	PLL_TRY();
	{
		TypeCacheEntry *typcache = lookup_type_cache(ti->basetypoid,
													 TYPECACHE_RANGE_INFO);
		RangeBound	lower;
		RangeBound	upper;

		lower.val = lowval;
		lower.infinite = lownull;
		lower.inclusive = (flags[0] == '[');
		lower.lower = true;
		upper.val = upval;
		upper.infinite = upnull;
		upper.inclusive = (flags[1] == ']');
		upper.lower = false;
		result = RangeTypePGetDatum(make_range(typcache, &lower, &upper,
											   empty, NULL));
	}
	PLL_CATCH(L);
	return result;
}

/* range values are read-only */
static int
range_newindex_cfunc(lua_State * L)
{
	return luaL_error(L, "range values cannot be modified");
}

/* range.field */
static int
range_index_cfunc(lua_State * L)
{
	pll_datum  *d = dt_check_kind(L, 1, PLL_KIND_RANGE, "a range");
	const char *key;
	RangeBound	lower;
	RangeBound	upper;
	bool		empty = false;
	pll_typeinfo *eti;

	if (lua_type(L, 2) != LUA_TSTRING)
	{
		lua_pushnil(L);
		return 1;
	}
	key = lua_tostring(L, 2);

	PLL_TRY();
	{
		TypeCacheEntry *typcache = lookup_type_cache(d->ti->basetypoid,
													 TYPECACHE_RANGE_INFO);

		range_deserialize(typcache, DatumGetRangeTypeP(d->value), &lower,
						  &upper, &empty);
	}
	PLL_CATCH(L);

	if (strcmp(key, "isempty") == 0)
		lua_pushboolean(L, empty);
	else if (strcmp(key, "lower_inc") == 0)
		lua_pushboolean(L, !empty && lower.inclusive);
	else if (strcmp(key, "upper_inc") == 0)
		lua_pushboolean(L, !empty && upper.inclusive);
	else if (strcmp(key, "lower_inf") == 0)
		lua_pushboolean(L, !empty && lower.infinite);
	else if (strcmp(key, "upper_inf") == 0)
		lua_pushboolean(L, !empty && upper.infinite);
	else if (strcmp(key, "lower") == 0 || strcmp(key, "upper") == 0)
	{
		RangeBound *b = (key[0] == 'l') ? &lower : &upper;

		eti = dt_element_ti(L, d->ti);
		pll_value_push(L, b->val, empty || b->infinite, eti);
		lua_remove(L, -2);
	}
	else
		lua_pushnil(L);
	return 1;
}

/* ------------------------------------------------------------------------
 * Typeinfo methods
 * ------------------------------------------------------------------------
 */

/* typeinfo(...): the constructor */
static int
ti_call_cfunc(lua_State * L)
{
	pll_typeinfo *ti = pll_typeinfo_check(L, 1);
	int			nargs = lua_gettop(L) - 1;
	Datum		result = (Datum) 0;
	bool		isnull = false;

	luaL_checkstack(L, 8, "out of stack space");

	switch (ti->kind)
	{
		case PLL_KIND_ARRAY:
			if (nargs == 1 && pll_datum_test(L, 2) != NULL)
				result = pll_value_todatum(L, 2, ti, &isnull);
			else
				result = dt_array_construct(L, ti, 2, nargs, false);
			break;
		case PLL_KIND_RANGE:
			result = dt_range_construct(L, ti, 2, nargs);
			break;
		case PLL_KIND_COMPOSITE:
			if (nargs == 0 || (nargs == 1 && lua_isnil(L, 2)))
			{
				/* a row of nulls */
				lua_newtable(L);
				result = pll_value_todatum(L, -1, ti, &isnull);
			}
			else if (nargs == 1 && dt_indexable(L, 2))
				result = pll_value_todatum(L, 2, ti, &isnull);
			else
			{
				TupleDesc	td = pll_typeinfo_tupdesc(L, ti);
				int			ncols = 0;
				int			i;

				for (i = 0; i < td->natts; i++)
				{
					if (!TupleDescAttr(td, i)->attisdropped)
						ncols++;
				}
				if (nargs != ncols)
					return luaL_error(L, "incorrect number of arguments for type constructor (expected %d got %d)",
									  ncols, nargs);
				result = pll_values_tocomposite(L, 2, nargs, ti);
			}
			break;
		case PLL_KIND_JSONB:
			if (nargs == 2 && lua_istable(L, 3) && !lua_isnil(L, 2))
			{
				/* the second argument holds conversion options */
				result = pll_jsonb_todatum(L, 2, 3);
				break;
			}
			if (nargs != 1)
				return luaL_error(L, "a constructor of this type takes a value and optionally a table of options");

			/*
			 * A lone string is JSON text, to be parsed; this differs from
			 * converting a string value, which gives a JSON string.
			 */
			if (lua_type(L, 2) == LUA_TSTRING)
				result = dt_input(L, lua_tostring(L, 2), ti);
			else
				result = pll_value_todatum(L, 2, ti, &isnull);
			break;
		default:
			if (nargs != 1)
				return luaL_error(L, "a constructor of this type takes exactly one argument");
			result = pll_value_todatum(L, 2, ti, &isnull);
			break;
	}

	if (isnull)
	{
		lua_pushnil(L);
		return 1;
	}
	pll_datum_push(L, ti, result);
	return 1;
}

/* typeinfo:fromstring(str) */
static int
ti_fromstring_cfunc(lua_State * L)
{
	pll_typeinfo *ti = pll_typeinfo_check(L, 1);
	size_t		len;
	const char *s = luaL_checklstring(L, 2, &len);
	Datum		result;

	if (strlen(s) != len)
		return luaL_error(L, "a string with an embedded zero byte cannot be converted to this type");
	result = dt_input(L, s, ti);
	result = dt_finish(L, result, ti);
	pll_datum_push(L, ti, result);
	return 1;
}

/* typeinfo:frombinary(str) */
static int
ti_frombinary_cfunc(lua_State * L)
{
	pll_typeinfo *ti = pll_typeinfo_check(L, 1);
	size_t		len;
	const char *s = luaL_checklstring(L, 2, &len);
	Datum		result = (Datum) 0;

	PLL_TRY();
	{
		StringInfoData buf;
		Oid			recv;
		Oid			ioparam;

		getTypeBinaryInputInfo(ti->typoid, &recv, &ioparam);
		initStringInfo(&buf);
		appendBinaryStringInfo(&buf, s, len);
		result = OidReceiveFunctionCall(recv, &buf, ioparam, ti->typmod);
		if (buf.cursor != buf.len)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_BINARY_REPRESENTATION),
					 errmsg("incorrect binary data format")));
	}
	PLL_CATCH(L);

	result = dt_finish(L, result, ti);
	pll_datum_push(L, ti, result);
	return 1;
}

/* ------------------------------------------------------------------------
 * The pgtype module
 * ------------------------------------------------------------------------
 */

/* Push the typeinfo of the named SQL type, or nil if there is none */
static void
dt_push_parsed_type(lua_State * L, const char *name)
{
	Oid			typid = InvalidOid;
	int32		typmod = -1;

	PLL_TRY();
	{
		ErrorSaveContext escontext = {T_ErrorSaveContext};

		if (!parseTypeString(name, &typid, &typmod, (Node *) &escontext))
			typid = InvalidOid;
	}
	PLL_CATCH(L);

	if (!OidIsValid(typid))
		lua_pushnil(L);
	else
		(void) pll_typeinfo_push(L, typid, typmod);
}

/* pgtype(value [, argno]) */
static int
pgtype_call_cfunc(lua_State * L)
{
	pll_datum  *d = pll_datum_test(L, 2);

	if (d != NULL)
	{
		(void) pll_typeinfo_push(L, d->ti->typoid, d->ti->typmod);
		return 1;
	}
	if (lua_type(L, 3) == LUA_TSTRING)
	{
		dt_push_parsed_type(L, lua_tostring(L, 3));
		return 1;
	}
	if (lua_isinteger(L, 3))
	{
		pll_call_push_argtype(L, (int) lua_tointeger(L, 3));
		return 1;
	}
	lua_pushnil(L);
	return 1;
}

/* pgtype.name */
static int
pgtype_index_cfunc(lua_State * L)
{
	if (lua_type(L, 2) != LUA_TSTRING)
	{
		lua_pushnil(L);
		return 1;
	}
	dt_push_parsed_type(L, lua_tostring(L, 2));
	return 1;
}

/* pgtype.array.name */
static int
pgtype_array_index_cfunc(lua_State * L)
{
	pll_typeinfo *eti;
	Oid			arrtype = InvalidOid;

	if (lua_type(L, 2) != LUA_TSTRING)
	{
		lua_pushnil(L);
		return 1;
	}
	dt_push_parsed_type(L, lua_tostring(L, 2));
	if (lua_isnil(L, -1))
		return 1;

	eti = pll_typeinfo_check(L, -1);
	PLL_TRY();
	{
		arrtype = get_array_type(eti->typoid);
	}
	PLL_CATCH(L);

	if (!OidIsValid(arrtype))
	{
		lua_pushnil(L);
		return 1;
	}
	(void) pll_typeinfo_push(L, arrtype, eti->typmod);
	return 1;
}

/*
 * __eq, __lt and __le for datum objects of the same type, using the
 * equality operator or btree comparison function of the type.  Upvalue 1 is
 * 0, 1 or 2 respectively.
 */
static int
dt_compare_cfunc(lua_State * L)
{
	int			op = (int) lua_tointeger(L, lua_upvalueindex(1));
	pll_datum  *d1 = pll_datum_test(L, 1);
	pll_datum  *d2 = pll_datum_test(L, 2);
	bool		result = false;

	if (d1 == NULL || d2 == NULL || !dt_same_type(d1->ti, d2->ti))
	{
		if (op == 0)
		{
			lua_pushboolean(L, false);
			return 1;
		}
		return luaL_error(L, "attempt to compare values of different types");
	}

	PLL_TRY();
	{
		TypeCacheEntry *typcache = lookup_type_cache(d1->ti->basetypoid,
													 TYPECACHE_EQ_OPR_FINFO |
													 TYPECACHE_CMP_PROC_FINFO);

		if (op == 0)
		{
			if (!OidIsValid(typcache->eq_opr_finfo.fn_oid))
				ereport(ERROR,
						(errcode(ERRCODE_UNDEFINED_FUNCTION),
						 errmsg("could not identify an equality operator for type %s",
								format_type_be(d1->ti->basetypoid))));
			result = DatumGetBool(FunctionCall2Coll(&typcache->eq_opr_finfo,
													DEFAULT_COLLATION_OID,
													d1->value, d2->value));
		}
		else
		{
			int32		cmp;

			if (!OidIsValid(typcache->cmp_proc_finfo.fn_oid))
				ereport(ERROR,
						(errcode(ERRCODE_UNDEFINED_FUNCTION),
						 errmsg("could not identify a comparison function for type %s",
								format_type_be(d1->ti->basetypoid))));
			cmp = DatumGetInt32(FunctionCall2Coll(&typcache->cmp_proc_finfo,
												  DEFAULT_COLLATION_OID,
												  d1->value, d2->value));
			result = (op == 1) ? (cmp < 0) : (cmp <= 0);
		}
	}
	PLL_CATCH(L);

	lua_pushboolean(L, result);
	return 1;
}

/* Create a metatable in the registry, filling it with the functions */
static void
dt_add_methods(lua_State * L, const char *mtname, const luaL_Reg * funcs,
			   bool comparable)
{
	luaL_newmetatable(L, mtname);
	luaL_setfuncs(L, funcs, 0);
	if (comparable)
		pll_datum_add_compare(L);
	lua_pop(L, 1);
}

/* Add __eq, __lt and __le to the metatable on top of the stack */
void
pll_datum_add_compare(lua_State * L)
{
	static const char *const opnames[] = {"__eq", "__lt", "__le"};
	int			i;

	for (i = 0; i < 3; i++)
	{
		lua_pushinteger(L, i);
		lua_pushcclosure(L, dt_compare_cfunc, 1);
		lua_setfield(L, -2, opnames[i]);
	}
}

/* typeinfo:kind(): composite, array, range, or scalar for anything else */
static int
ti_kind_cfunc(lua_State * L)
{
	pll_typeinfo *ti = pll_typeinfo_check(L, 1);
	const char *kind;

	switch (ti->kind)
	{
		case PLL_KIND_COMPOSITE:
			kind = "composite";
			break;
		case PLL_KIND_ARRAY:
			kind = "array";
			break;
		case PLL_KIND_RANGE:
			kind = "range";
			break;
		default:
			kind = "scalar";
			break;
	}
	lua_pushstring(L, kind);
	return 1;
}

static const luaL_Reg ti_methods[] = {
	{"fromstring", ti_fromstring_cfunc},
	{"frombinary", ti_frombinary_cfunc},
	{"name", ti_name_cfunc},
	{"element", ti_element_cfunc},
	{"kind", ti_kind_cfunc},
	{NULL, NULL}
};

static const luaL_Reg ti_meta[] = {
	{"__call", ti_call_cfunc},
	{"__tostring", ti_tostring_cfunc},
	{NULL, NULL}
};

static const luaL_Reg scalar_meta[] = {
	{"__tostring", pll_datum_tostring},
	{NULL, NULL}
};

static const luaL_Reg composite_meta[] = {
	{"__tostring", pll_datum_tostring},
	{"__index", row_index_cfunc},
	{"__newindex", row_newindex_cfunc},
	{"__len", row_len_cfunc},
	{"__pairs", row_pairs_cfunc},
	{"__call", row_call_cfunc},
	{NULL, NULL}
};

static const luaL_Reg array_meta[] = {
	{"__tostring", pll_datum_tostring},
	{"__index", array_index_cfunc},
	{"__newindex", array_newindex_cfunc},
	{"__len", array_len_cfunc},
	{"__pairs", array_pairs_cfunc},
	{"__call", array_call_cfunc},
	{NULL, NULL}
};

static const luaL_Reg slice_meta[] = {
	{"__index", slice_index_cfunc},
	{"__newindex", slice_newindex_cfunc},
	{"__len", slice_len_cfunc},
	{"__pairs", array_pairs_cfunc},
	{NULL, NULL}
};

static const luaL_Reg range_meta[] = {
	{"__tostring", pll_datum_tostring},
	{"__index", range_index_cfunc},
	{"__newindex", range_newindex_cfunc},
	{NULL, NULL}
};

int
pll_open_pgtype(lua_State * L)
{
	if (!dt_callbacks_registered)
	{
		CacheRegisterSyscacheCallback(TYPEOID, dt_syscache_callback, (Datum) 0);
		CacheRegisterSyscacheCallback(ATTNUM, dt_syscache_callback, (Datum) 0);
		CacheRegisterRelcacheCallback(dt_relcache_callback, (Datum) 0);
		dt_callbacks_registered = true;
	}

	/* the typeinfo cache */
	lua_newtable(L);
	lua_rawsetp(L, LUA_REGISTRYINDEX, PLL_REGKEY_TYPEINFOS);

	/* metatables */
	luaL_newmetatable(L, PLL_MT_TYPEINFO);
	luaL_setfuncs(L, ti_meta, 0);
	luaL_newlib(L, ti_methods);
	lua_setfield(L, -2, "__index");
	lua_pop(L, 1);

	dt_add_methods(L, PLL_MT_DATUM, scalar_meta, true);
	dt_add_methods(L, PLL_MT_COMPOSITE, composite_meta, true);
	dt_add_methods(L, PLL_MT_ARRAY, array_meta, true);
	dt_add_methods(L, PLL_MT_ARRAY_SLICE, slice_meta, false);
	dt_add_methods(L, PLL_MT_RANGE, range_meta, true);

	/* the module: callable, and indexed by type name */
	lua_newtable(L);
	lua_newtable(L);			/* pgtype.array */
	lua_newtable(L);
	lua_pushcfunction(L, pgtype_array_index_cfunc);
	lua_setfield(L, -2, "__index");
	lua_setmetatable(L, -2);
	lua_setfield(L, -2, "array");

	lua_newtable(L);
	lua_pushcfunction(L, pgtype_call_cfunc);
	lua_setfield(L, -2, "__call");
	lua_pushcfunction(L, pgtype_index_cfunc);
	lua_setfield(L, -2, "__index");
	lua_setmetatable(L, -2);
	return 1;
}
