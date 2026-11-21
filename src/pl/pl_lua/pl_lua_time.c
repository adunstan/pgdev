/*-------------------------------------------------------------------------
 *
 * pl_lua_time.c
 *	  date/time datum objects and the pl_lua.time module.
 *
 * Building a value from a table of fields happens in two phases.  First the
 * fields are read out of the Lua table into a C structure (this can run Lua
 * code, through metamethods, and so is done outside of PLL_TRY).  Then the
 * value is computed from the structure by PostgreSQL code, inside PLL_TRY.
 *
 * Years in tables are astronomical (year 0 is 1 BC), as in struct pg_tm.
 *
 * The pl_lua.time module contains two functions:
 *
 *	new(typename, fields)		construct a date/time value of one of the types
 *								date, time, timetz, timestamp, timestamptz or
 *								interval from a table of fields; the same as
 *								calling pgtype.<typename>(fields)
 *	as_table(value [, zone])	the same as value:as_table(zone)
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua_time.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <math.h>

#include "catalog/pg_type.h"
#include "common/int.h"
#include "miscadmin.h"
#include "pgtime.h"
#include "utils/builtins.h"
#include "utils/date.h"
#include "utils/datetime.h"
#include "utils/fmgrprotos.h"
#include "utils/lsyscache.h"
#include "utils/numeric.h"
#include "utils/timestamp.h"

#include "pl_lua_int.h"

/* The numeric fields of a table, in the order they are read */
typedef enum TimeFieldId
{
	F_YEAR,
	F_MONTH,
	F_DAY,
	F_HOUR,
	F_MIN,
	F_SEC,
	F_MSEC,
	F_USEC,
	F_EPOCH,
	F_EPOCH_MSEC,
	F_EPOCH_USEC,
	NUM_TIME_FIELDS
} TimeFieldId;

static const char *const time_field_names[NUM_TIME_FIELDS] = {
	"year", "month", "day", "hour", "min", "sec", "msec", "usec",
	"epoch", "epoch_msec", "epoch_usec"
};

typedef struct TimeField
{
	bool		present;
	bool		isint;
	int64		ival;
	double		dval;
} TimeField;

typedef enum ZoneKind
{
	ZONE_NONE,
	ZONE_SESSION,
	ZONE_OFFSET,
	ZONE_NAME
} ZoneKind;

typedef struct TimeFields
{
	TimeField	f[NUM_TIME_FIELDS];
	ZoneKind	zkind;
	int			zoffset;		/* seconds east of UTC, for ZONE_OFFSET */
	const char *zname;			/* for ZONE_NAME; kept alive by the caller */
	int			isdst;			/* -1 if not given */
} TimeFields;

/* Largest time zone offset we accept, in seconds (as in the datetime code) */
#define PLL_MAX_TZ_OFFSET	((MAX_TZDISP_HOUR * SECS_PER_HOUR) + 59 * 60 + 59)

/* microseconds between 1970-01-01 and 2000-01-01 */
#define UNIX_TO_PG_USECS \
	(((int64) (POSTGRES_EPOCH_JDATE - UNIX_EPOCH_JDATE)) * USECS_PER_DAY)

typedef Datum (*extract_fn) (FunctionCallInfo fcinfo);

static pll_datum *time_arg(lua_State * L, int idx);
static bool is_epoch_field(TimeFieldId id);
static void read_number_field(lua_State * L, int tidx, TimeFields *f,
							  TimeFieldId id);
static int	parse_offset_string(const char *s, int *offset);
static void read_zone_fields(lua_State * L, int tidx, TimeFields *f);
static void read_time_fields(lua_State * L, int tidx, TimeFields *f);
static int64 mul_check(int64 a, int64 b);
static int64 add_check(int64 a, int64 b);
static int64 field_to_usec(const TimeField *tf, int64 scale);
static int64 sec_to_usec(const TimeField *tf);
static int64 floor_div64(int64 a, int64 b);
static int64 floor_mod64(int64 a, int64 b);
static int64 time_of_day_usec(const TimeFields *f);
static int64 epoch_to_usec(const TimeFields *f);
static void calendar_days_and_time(const TimeFields *f, Oid typoid, int64 *days,
								   int64 *tod);
static pg_tz *zone_for(const TimeFields *f);
static int	zone_offset_with_hint(struct pg_tm *tm, pg_tz *tzp, int isdst);
static Timestamp local_from_instant(const TimeFields *f, Timestamp utc);
static void check_fields(const TimeFields *f, Oid typoid, bool *has_calendar,
						 bool *has_epoch);
static Datum build_interval(const TimeFields *f, bool has_epoch);
static Datum build_time_value(const TimeFields *f, Oid typoid);
static int	time_index(lua_State * L);
static int	time_as_table(lua_State * L);
static int	time_new(lua_State * L);
static int	time_module_as_table(lua_State * L);
static extract_fn extract_function_for(Oid typoid);
static void push_extract_result(lua_State * L, Datum num);
static void push_field(lua_State * L, pll_datum *pd, const char *name);
static void set_int_field(lua_State * L, const char *name, lua_Integer v);
static void set_str_field(lua_State * L, const char *name, const char *v);
static void set_bool_field(lua_State * L, const char *name, bool v);
static void table_from_tm(lua_State * L, struct pg_tm *tm, fsec_t fsec);

void
pll_tj_error(lua_State * L, int sqlerrcode, const char *fmt, ...)
{
	char		buf[256];
	va_list		ap;

	va_start(ap, fmt);
	vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);

	PLL_TRY();
	{
		ereport(ERROR,
				(errcode(sqlerrcode),
				 errmsg_internal("%s", buf)));
	}
	PLL_CATCH(L);
	pg_unreachable();
}

/* ------------------------------------------------------------------------
 * Table of fields to value
 * ------------------------------------------------------------------------
 */

static bool
is_epoch_field(TimeFieldId id)
{
	return id == F_EPOCH || id == F_EPOCH_MSEC || id == F_EPOCH_USEC;
}

/*
 * Read one numeric field of the table at tidx, checking it as far as can be
 * done without knowing the type.
 */
static void
read_number_field(lua_State * L, int tidx, TimeFields *f, TimeFieldId id)
{
	TimeField  *tf = &f->f[id];
	const char *name = time_field_names[id];

	lua_getfield(L, tidx, name);
	if (lua_isnil(L, -1))
	{
		lua_pop(L, 1);
		return;
	}
	if (lua_type(L, -1) != LUA_TNUMBER)
		pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
					 "field \"%s\" must be a number", name);

	tf->present = true;
	if (lua_isinteger(L, -1))
	{
		tf->isint = true;
		tf->ival = lua_tointeger(L, -1);
	}
	else
	{
		tf->dval = lua_tonumber(L, -1);
		if (isnan(tf->dval) || (!isfinite(tf->dval) && !is_epoch_field(id)))
			pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
						 "field \"%s\" must be a finite number", name);
	}
	lua_pop(L, 1);

	if (id <= F_MIN && id != F_SEC)
	{
		/* these are 32-bit integers */
		if (!tf->isint)
		{
			if (tf->dval != floor(tf->dval))
				pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
							 "field \"%s\" must be an integer", name);
			if (tf->dval < (double) PG_INT32_MIN ||
				tf->dval > (double) PG_INT32_MAX)
				pll_tj_error(L, ERRCODE_DATETIME_FIELD_OVERFLOW,
							 "field \"%s\" is out of range", name);
			tf->isint = true;
			tf->ival = (int64) tf->dval;
		}
		if (tf->ival < PG_INT32_MIN || tf->ival > PG_INT32_MAX)
			pll_tj_error(L, ERRCODE_DATETIME_FIELD_OVERFLOW,
						 "field \"%s\" is out of range", name);
	}
	else if (id == F_SEC)
	{
		/* the whole seconds must fit in 32 bits */
		double		whole = tf->isint ? (double) tf->ival : floor(tf->dval);

		if (whole < (double) PG_INT32_MIN || whole > (double) PG_INT32_MAX)
			pll_tj_error(L, ERRCODE_DATETIME_FIELD_OVERFLOW,
						 "field \"%s\" is out of range", name);
	}
}

/*
 * Parse a time zone offset string: a sign followed by hh, hhmm, hhmmss,
 * hh:mm or hh:mm:ss.  Returns the offset in seconds east of UTC in *offset,
 * or false if the string is not of that form.
 */
static int
parse_offset_string(const char *s, int *offset)
{
	int			sign;
	int			parts[3] = {0, 0, 0};
	int			nparts = 0;
	const char *p;
	int			ndigits;

	if (s[0] == '+')
		sign = 1;
	else if (s[0] == '-')
		sign = -1;
	else
		return false;
	p = s + 1;

	if (strchr(p, ':') != NULL)
	{
		while (nparts < 3)
		{
			ndigits = 0;
			parts[nparts] = 0;
			while (isdigit((unsigned char) *p) && ndigits < 2)
			{
				parts[nparts] = parts[nparts] * 10 + (*p - '0');
				p++;
				ndigits++;
			}
			if (ndigits == 0)
				return false;
			nparts++;
			if (*p == ':')
				p++;
			else
				break;
		}
		if (*p != '\0')
			return false;
	}
	else
	{
		ndigits = (int) strlen(p);
		if (ndigits != 2 && ndigits != 4 && ndigits != 6)
			return false;
		for (nparts = 0; nparts * 2 < ndigits; nparts++)
		{
			if (!isdigit((unsigned char) p[nparts * 2]) ||
				!isdigit((unsigned char) p[nparts * 2 + 1]))
				return false;
			parts[nparts] = (p[nparts * 2] - '0') * 10 + (p[nparts * 2 + 1] - '0');
		}
	}

	if (parts[1] > 59 || parts[2] > 59)
		return false;
	*offset = sign * (parts[0] * SECS_PER_HOUR + parts[1] * 60 + parts[2]);
	return true;
}

/* Read the timezone and isdst fields. */
static void
read_zone_fields(lua_State * L, int tidx, TimeFields *f)
{
	f->zkind = ZONE_NONE;
	f->isdst = -1;

	lua_getfield(L, tidx, "isdst");
	if (lua_type(L, -1) == LUA_TBOOLEAN)
		f->isdst = lua_toboolean(L, -1) ? 1 : 0;
	else if (!lua_isnil(L, -1))
		pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
					 "field \"isdst\" must be a boolean");
	lua_pop(L, 1);

	/* the value is left on the stack, to keep a zone name alive */
	lua_getfield(L, tidx, "timezone");
	switch (lua_type(L, -1))
	{
		case LUA_TNIL:
			break;
		case LUA_TBOOLEAN:
			if (!lua_toboolean(L, -1))
				pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
							 "field \"timezone\" must not be false");
			f->zkind = ZONE_SESSION;
			break;
		case LUA_TNUMBER:
			{
				lua_Number	d = lua_tonumber(L, -1);

				if (!isfinite(d) || d != floor(d))
					pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
								 "field \"timezone\" must be an integral number of seconds");
				if (fabs(d) > PLL_MAX_TZ_OFFSET)
					pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
								 "time zone offset is out of range");
				f->zkind = ZONE_OFFSET;
				f->zoffset = (int) d;
			}
			break;
		case LUA_TSTRING:
			{
				const char *s = lua_tostring(L, -1);
				int			off;

				if (parse_offset_string(s, &off))
				{
					if (abs(off) > PLL_MAX_TZ_OFFSET)
						pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
									 "time zone offset is out of range");
					f->zkind = ZONE_OFFSET;
					f->zoffset = off;
				}
				else
				{
					f->zkind = ZONE_NAME;
					f->zname = s;
				}
			}
			break;
		default:
			pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
						 "field \"timezone\" must be a string, number or true");
	}
}

static void
read_time_fields(lua_State * L, int tidx, TimeFields *f)
{
	int			i;

	memset(f, 0, sizeof(TimeFields));
	for (i = 0; i < NUM_TIME_FIELDS; i++)
		read_number_field(L, tidx, f, (TimeFieldId) i);
	read_zone_fields(L, tidx, f);
}

/* ------------------------------------------------------------------------
 * Computing the value (PostgreSQL code, may raise errors)
 * ------------------------------------------------------------------------
 */

static int64
mul_check(int64 a, int64 b)
{
	int64		r;

	if (pg_mul_s64_overflow(a, b, &r))
		ereport(ERROR,
				(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
				 errmsg("date/time field value out of range")));
	return r;
}

static int64
add_check(int64 a, int64 b)
{
	int64		r;

	if (pg_add_s64_overflow(a, b, &r))
		ereport(ERROR,
				(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
				 errmsg("date/time field value out of range")));
	return r;
}

/* The value of a field, multiplied by scale, in microseconds */
static int64
field_to_usec(const TimeField *tf, int64 scale)
{
	double		d;

	if (!tf->present)
		return 0;
	if (tf->isint)
		return mul_check(tf->ival, scale);

	d = rint(tf->dval * (double) scale);
	if (!(d >= -9223372036854775808.0 && d < 9223372036854775808.0))
		ereport(ERROR,
				(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
				 errmsg("date/time field value out of range")));
	return (int64) d;
}

/* Seconds, with a fraction, in microseconds */
static int64
sec_to_usec(const TimeField *tf)
{
	double		whole;
	double		frac;

	if (!tf->present)
		return 0;
	if (tf->isint)
		return mul_check(tf->ival, USECS_PER_SEC);

	whole = floor(tf->dval);
	frac = rint((tf->dval - whole) * (double) USECS_PER_SEC);
	return add_check(mul_check((int64) whole, USECS_PER_SEC), (int64) frac);
}

static int64
floor_div64(int64 a, int64 b)
{
	int64		q = a / b;

	if ((a % b != 0) && ((a < 0) != (b < 0)))
		q--;
	return q;
}

static int64
floor_mod64(int64 a, int64 b)
{
	return a - floor_div64(a, b) * b;
}

/* The time fields (hour .. usec) of a calendar table, as microseconds */
static int64
time_of_day_usec(const TimeFields *f)
{
	int64		t = 0;

	if (f->f[F_HOUR].present)
		t = mul_check(f->f[F_HOUR].ival, USECS_PER_HOUR);
	if (f->f[F_MIN].present)
		t = add_check(t, mul_check(f->f[F_MIN].ival, USECS_PER_MINUTE));
	t = add_check(t, sec_to_usec(&f->f[F_SEC]));
	t = add_check(t, field_to_usec(&f->f[F_MSEC], 1000));
	t = add_check(t, field_to_usec(&f->f[F_USEC], 1));
	return t;
}

/* The epoch (seconds, msec or usec since 1970) as microseconds */
static int64
epoch_to_usec(const TimeFields *f)
{
	int64		t;

	if (f->f[F_EPOCH].present)
		t = field_to_usec(&f->f[F_EPOCH], USECS_PER_SEC);
	else if (f->f[F_EPOCH_MSEC].present)
		t = field_to_usec(&f->f[F_EPOCH_MSEC], 1000);
	else
		t = field_to_usec(&f->f[F_EPOCH_USEC], 1);

	t = add_check(t, field_to_usec(&f->f[F_MSEC], 1000));
	return add_check(t, field_to_usec(&f->f[F_USEC], 1));
}

/*
 * Normalize the calendar fields of a table into a number of days since the
 * PostgreSQL epoch and a time of day in microseconds (which may be outside of
 * a single day).  Months outside of 1..12 and days outside of the month are
 * carried.
 */
static void
calendar_days_and_time(const TimeFields *f, Oid typoid, int64 *days,
					   int64 *tod)
{
	int64		m0 = f->f[F_YEAR].ival * 12 + (f->f[F_MONTH].ival - 1);
	int64		y = floor_div64(m0, 12);
	int			mo = (int) (m0 - y * 12) + 1;

	if (y < INT_MIN || y > INT_MAX || !IS_VALID_JULIAN((int) y, mo, 1))
		ereport(ERROR,
				(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
				 errmsg("%s out of range",
						typoid == DATEOID ? "date" : "timestamp")));

	*days = (int64) date2j((int) y, mo, 1) - POSTGRES_EPOCH_JDATE +
		(f->f[F_DAY].ival - 1);
	*tod = time_of_day_usec(f);
}

/* The time zone to use for f, or NULL if it is a fixed offset or none */
static pg_tz *
zone_for(const TimeFields *f)
{
	pg_tz	   *tzp;

	switch (f->zkind)
	{
		case ZONE_SESSION:
			return session_timezone;
		case ZONE_NAME:
			tzp = pg_tzset(f->zname);
			if (tzp == NULL)
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
						 errmsg("time zone \"%s\" not recognized", f->zname)));
			return tzp;
		default:
			return NULL;
	}
}

/*
 * Find the offset (in seconds west of UTC) for a local time, as
 * DetermineTimeZoneOffset does, but if the local time is ambiguous because
 * of a transition out of daylight saving time, use isdst (if not -1) to
 * choose between the two possibilities.
 */
static int
zone_offset_with_hint(struct pg_tm *tm, pg_tz *tzp, int isdst)
{
	int			tz = DetermineTimeZoneOffset(tm, tzp);
	int			chosen_isdst = tm->tm_isdst;
	pg_time_t	mytime;
	pg_time_t	prevtime;
	pg_time_t	boundary;
	long int	before_gmtoff;
	long int	after_gmtoff;
	int			before_isdst;
	int			after_isdst;
	int			res;

	if (isdst < 0 || chosen_isdst == isdst)
		return tz;

	mytime = ((pg_time_t) (date2j(tm->tm_year, tm->tm_mon, tm->tm_mday) -
						   UNIX_EPOCH_JDATE)) * SECS_PER_DAY +
		tm->tm_sec + (tm->tm_min + tm->tm_hour * MINS_PER_HOUR) * SECS_PER_MINUTE;
	prevtime = mytime - SECS_PER_DAY;
	res = pg_next_dst_boundary(&prevtime, &before_gmtoff, &before_isdst,
							   &boundary, &after_gmtoff, &after_isdst, tzp);
	if (res <= 0)
		return tz;

	/* both interpretations are valid only if the time is in the overlap */
	if (mytime - before_gmtoff < boundary && mytime - after_gmtoff >= boundary)
	{
		if (before_isdst == isdst)
			return -(int) before_gmtoff;
		if (after_isdst == isdst)
			return -(int) after_gmtoff;
	}
	return tz;
}

/*
 * The local time, in the zone of f (or UTC if none), at the instant utc.
 */
static Timestamp
local_from_instant(const TimeFields *f, Timestamp utc)
{
	pg_tz	   *tzp = zone_for(f);
	Timestamp	local;

	if (f->zkind == ZONE_OFFSET)
		local = add_check(utc, mul_check(f->zoffset, USECS_PER_SEC));
	else if (tzp != NULL)
	{
		struct pg_tm tm;
		fsec_t		fsec;
		int			tz;

		if (timestamp2tm(utc, &tz, &tm, &fsec, NULL, tzp) != 0 ||
			tm2timestamp(&tm, fsec, NULL, &local) != 0)
			ereport(ERROR,
					(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
					 errmsg("timestamp out of range")));
	}
	else
		local = utc;

	if (!IS_VALID_TIMESTAMP(local))
		ereport(ERROR,
				(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
				 errmsg("timestamp out of range")));
	return local;
}

/*
 * Check the combination of fields given for the type.
 */
static void
check_fields(const TimeFields *f, Oid typoid, bool *has_calendar, bool *has_epoch)
{
	int			i;
	int			nepoch = 0;
	bool		cal = false;
	bool		any = false;

	for (i = F_YEAR; i <= F_SEC; i++)
		cal |= f->f[i].present;
	for (i = F_EPOCH; i <= F_EPOCH_USEC; i++)
		nepoch += f->f[i].present;
	for (i = 0; i < NUM_TIME_FIELDS; i++)
		any |= f->f[i].present;

	if (!any)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("no date/time fields given")));
	if (nepoch > 1)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("only one of \"epoch\", \"epoch_msec\" and \"epoch_usec\" can be given")));
	if (nepoch > 0 && cal)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("calendar fields and epoch fields cannot both be given")));

	*has_calendar = cal;
	*has_epoch = (nepoch > 0);

	if (cal && typoid != INTERVALOID)
	{
		bool		datetype = (typoid == DATEOID || typoid == TIMESTAMPOID ||
								typoid == TIMESTAMPTZOID);

		if (datetype)
		{
			if (!f->f[F_YEAR].present || !f->f[F_MONTH].present ||
				!f->f[F_DAY].present)
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
						 errmsg("fields \"year\", \"month\" and \"day\" are all required")));
		}
		else
		{
			if (!f->f[F_HOUR].present)
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
						 errmsg("field \"hour\" is required")));
			if (f->f[F_SEC].present && !f->f[F_MIN].present)
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
						 errmsg("field \"sec\" requires field \"min\"")));
			if (f->f[F_YEAR].present || f->f[F_MONTH].present ||
				f->f[F_DAY].present)
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
						 errmsg("date fields cannot be given for this type")));
		}
	}

	/* where a time zone is allowed */
	if (f->zkind != ZONE_NONE)
	{
		bool		ok;

		if (typoid == TIMESTAMPTZOID)
			ok = !*has_epoch;
		else if (typoid == TIMETZOID)
			ok = (f->zkind == ZONE_OFFSET);
		else if (typoid == TIMESTAMPOID || typoid == DATEOID)
			ok = *has_epoch;
		else
			ok = false;

		if (!ok)
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("a time zone cannot be given for this combination of type and fields")));
	}
}

static Datum
build_interval(const TimeFields *f, bool has_epoch)
{
	Interval   *iv = palloc(sizeof(Interval));
	int64		months = 0;

	if (has_epoch)
	{
		iv->month = 0;
		iv->day = 0;
		iv->time = epoch_to_usec(f);
		return IntervalPGetDatum(iv);
	}

	if (f->f[F_YEAR].present)
		months = f->f[F_YEAR].ival * 12;
	if (f->f[F_MONTH].present)
		months += f->f[F_MONTH].ival;
	if (months < PG_INT32_MIN || months > PG_INT32_MAX)
		ereport(ERROR,
				(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
				 errmsg("interval out of range")));
	iv->month = (int32) months;
	iv->day = f->f[F_DAY].present ? (int32) f->f[F_DAY].ival : 0;
	iv->time = time_of_day_usec(f);
	return IntervalPGetDatum(iv);
}

/*
 * Compute the value of the given type from f.
 */
static Datum
build_time_value(const TimeFields *f, Oid typoid)
{
	bool		has_calendar;
	bool		has_epoch;
	Timestamp	local = 0;
	int64		days = 0;
	int64		tod = 0;
	int64		epoch_us = 0;

	check_fields(f, typoid, &has_calendar, &has_epoch);

	if (typoid == INTERVALOID)
		return build_interval(f, has_epoch);

	if (has_epoch)
	{
		if (f->f[F_EPOCH].present && !f->f[F_EPOCH].isint &&
			isinf(f->f[F_EPOCH].dval))
		{
			if (typoid != TIMESTAMPOID && typoid != TIMESTAMPTZOID)
				ereport(ERROR,
						(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
						 errmsg("an infinite epoch is only valid for timestamps")));
			if (f->f[F_EPOCH].dval > 0)
				return TimestampGetDatum(DT_NOEND);
			return TimestampGetDatum(DT_NOBEGIN);
		}
		if ((f->f[F_EPOCH_MSEC].present && !f->f[F_EPOCH_MSEC].isint &&
			 isinf(f->f[F_EPOCH_MSEC].dval)) ||
			(f->f[F_EPOCH_USEC].present && !f->f[F_EPOCH_USEC].isint &&
			 isinf(f->f[F_EPOCH_USEC].dval)))
			ereport(ERROR,
					(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
					 errmsg("an infinite epoch is only valid in \"epoch\"")));
		epoch_us = epoch_to_usec(f);
	}
	else
		calendar_days_and_time(f, typoid, &days, &tod);

	switch (typoid)
	{
		case TIMESTAMPTZOID:
			if (has_epoch)
				local = add_check(epoch_us, -UNIX_TO_PG_USECS);
			else
			{
				struct pg_tm tm;
				fsec_t		fsec;
				int			tz;

				local = add_check(mul_check(days, USECS_PER_DAY), tod);
				if (!IS_VALID_TIMESTAMP(local))
					ereport(ERROR,
							(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
							 errmsg("timestamp out of range")));

				if (f->zkind == ZONE_OFFSET)
					tz = -f->zoffset;
				else
				{
					pg_tz	   *tzp = (f->zkind == ZONE_NONE) ?
						session_timezone : zone_for(f);

					if (timestamp2tm(local, NULL, &tm, &fsec, NULL, NULL) != 0)
						ereport(ERROR,
								(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
								 errmsg("timestamp out of range")));
					tz = zone_offset_with_hint(&tm, tzp, f->isdst);
				}
				local = add_check(local, mul_check(tz, USECS_PER_SEC));
			}
			if (!IS_VALID_TIMESTAMP(local))
				ereport(ERROR,
						(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
						 errmsg("timestamp out of range")));
			return TimestampTzGetDatum(local);

		case TIMESTAMPOID:
			if (has_epoch)
			{
				local = add_check(epoch_us, -UNIX_TO_PG_USECS);
				if (!IS_VALID_TIMESTAMP(local))
					ereport(ERROR,
							(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
							 errmsg("timestamp out of range")));
				local = local_from_instant(f, local);
			}
			else
				local = add_check(mul_check(days, USECS_PER_DAY), tod);
			if (!IS_VALID_TIMESTAMP(local))
				ereport(ERROR,
						(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
						 errmsg("timestamp out of range")));
			return TimestampGetDatum(local);

		case DATEOID:
			if (has_epoch)
			{
				local = add_check(epoch_us, -UNIX_TO_PG_USECS);
				if (!IS_VALID_TIMESTAMP(local))
					ereport(ERROR,
							(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
							 errmsg("date out of range")));
				local = local_from_instant(f, local);
				days = floor_div64(local, USECS_PER_DAY);
			}
			else if (tod != USECS_PER_DAY)	/* 24:00:00 is still the same day */
				days += floor_div64(tod, USECS_PER_DAY);
			if (!IS_VALID_DATE(days))
				ereport(ERROR,
						(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
						 errmsg("date out of range")));
			return DateADTGetDatum((DateADT) days);

		case TIMEOID:
			if (has_epoch)
				tod = epoch_us;
			return TimeADTGetDatum(floor_mod64(tod, USECS_PER_DAY));

		case TIMETZOID:
			{
				TimeTzADT  *result = palloc(sizeof(TimeTzADT));

				if (has_epoch)
					tod = epoch_us;
				result->time = floor_mod64(tod, USECS_PER_DAY);
				if (f->zkind == ZONE_OFFSET)
					result->zone = -f->zoffset;
				else
				{
					struct pg_tm tm;
					fsec_t		fsec;
					int			tz;

					/* default to the session time zone, as of now */
					if (timestamp2tm(GetCurrentTransactionStartTimestamp(), &tz,
									 &tm, &fsec, NULL, session_timezone) != 0)
						tz = 0;
					result->zone = tz;
				}
				return TimeTzADTPGetDatum(result);
			}

		default:
			elog(ERROR, "unsupported date/time type %u", typoid);
	}
	return (Datum) 0;
}

Datum
pll_time_todatum(lua_State * L, int idx, pll_typeinfo *ti)
{
	TimeFields	fields;
	int			top = lua_gettop(L);
	Datum volatile result = (Datum) 0;
	Oid			typoid = ti->basetypoid;

	idx = lua_absindex(L, idx);
	if (lua_type(L, idx) != LUA_TTABLE)
		pll_tj_error(L, ERRCODE_DATATYPE_MISMATCH,
					 "a table of fields is required to construct a date/time value");

	luaL_checkstack(L, 8, "out of stack space");
	read_time_fields(L, idx, &fields);

	PLL_TRY();
	{
		result = build_time_value(&fields, typoid);
	}
	PLL_CATCH(L);

	lua_settop(L, top);
	return result;
}

/* ------------------------------------------------------------------------
 * Value to fields
 * ------------------------------------------------------------------------
 */

static pll_datum *
time_arg(lua_State * L, int idx)
{
	pll_datum  *pd = pll_datum_test(L, idx);

	if (pd == NULL || pd->ti->kind != PLL_KIND_TIME)
		luaL_error(L, "bad argument #%d (date/time value expected)", idx);
	return pd;
}

static extract_fn
extract_function_for(Oid typoid)
{
	switch (typoid)
	{
		case DATEOID:
			return extract_date;
		case TIMEOID:
			return extract_time;
		case TIMETZOID:
			return extract_timetz;
		case TIMESTAMPOID:
			return extract_timestamp;
		case TIMESTAMPTZOID:
			return extract_timestamptz;
		default:
			return extract_interval;
	}
}

/*
 * Push the numeric result of extract() as a Lua number: an integer if it has
 * no fractional part, otherwise a float.  The numeric is given as text so
 * that this runs outside of PLL_TRY.
 */
static void
push_extract_result(lua_State * L, Datum num)
{
	char	   *volatile str = NULL;
	char	   *dot;

	PLL_TRY();
	{
		str = DatumGetCString(DirectFunctionCall1(numeric_out, num));
	}
	PLL_CATCH(L);

	dot = strchr(str, '.');
	if (dot != NULL && strspn(dot + 1, "0") == strlen(dot + 1))
		*dot = '\0';			/* the fraction is all zeros */

	if (strspn(str + (str[0] == '-'), "0123456789") ==
		strlen(str + (str[0] == '-')) && strlen(str) <= 18)
		lua_pushinteger(L, (lua_Integer) strtoll(str, NULL, 10));
	else
		lua_pushnumber(L, (lua_Number) strtod(str, NULL));
}

/* Push the value of the named field of the datum */
static void
push_field(lua_State * L, pll_datum *pd, const char *name)
{
	Datum volatile num = (Datum) 0;
	Oid			typoid = pd->ti->basetypoid;
	int			scale = 0;

	PLL_TRY();
	{
		extract_fn	fn = extract_function_for(typoid);
		const char *unit = name;

		if (strcmp(name, "isoweek") == 0)
			unit = "week";
		else if (strcmp(name, "epoch_msec") == 0)
		{
			unit = "epoch";
			scale = 1000;
		}
		else if (strcmp(name, "epoch_usec") == 0)
		{
			unit = "epoch";
			scale = 1000000;
		}

		/* a date has all the fields of the timestamp at its midnight */
		if (typoid == DATEOID)
			num = DirectFunctionCall2(extract_timestamp, CStringGetTextDatum(unit),
									  DirectFunctionCall1(date_timestamp, pd->value));
		else
			num = DirectFunctionCall2(fn, CStringGetTextDatum(unit), pd->value);
		if (scale != 0)
			num = DirectFunctionCall2(numeric_mul, num,
									  DirectFunctionCall1(int4_numeric,
														  Int32GetDatum(scale)));
		else if (strcmp(name, "julian") == 0)
			num = DirectFunctionCall2(numeric_trunc, num, Int32GetDatum(0));
		else if (strcmp(name, "milliseconds") == 0)
			num = DirectFunctionCall2(numeric_round, num, Int32GetDatum(0));
	}
	PLL_CATCH(L);

	push_extract_result(L, num);
}

static int
time_index(lua_State * L)
{
	pll_datum  *pd = time_arg(L, 1);
	const char *key;

	if (lua_type(L, 2) != LUA_TSTRING)
	{
		lua_pushnil(L);
		return 1;
	}
	key = lua_tostring(L, 2);

	if (strcmp(key, "as_table") == 0)
	{
		lua_pushcfunction(L, time_as_table);
		return 1;
	}

	push_field(L, pd, key);
	return 1;
}

static void
set_int_field(lua_State * L, const char *name, lua_Integer v)
{
	lua_pushinteger(L, v);
	lua_setfield(L, -2, name);
}

static void
set_str_field(lua_State * L, const char *name, const char *v)
{
	lua_pushstring(L, v);
	lua_setfield(L, -2, name);
}

static void
set_bool_field(lua_State * L, const char *name, bool v)
{
	lua_pushboolean(L, v);
	lua_setfield(L, -2, name);
}

/* year, month, day, hour, min and sec of a broken-down time */
static void
table_from_tm(lua_State * L, struct pg_tm *tm, fsec_t fsec)
{
	set_int_field(L, "year", tm->tm_year);
	set_int_field(L, "month", tm->tm_mon);
	set_int_field(L, "day", tm->tm_mday);
	set_int_field(L, "hour", tm->tm_hour);
	set_int_field(L, "min", tm->tm_min);
	set_int_field(L, "sec", tm->tm_sec);
	set_int_field(L, "usec", fsec);
}

/*
 * value:as_table([zone]): the value broken down into a table of fields, which
 * can be passed back to the constructor.
 */
static int
time_as_table(lua_State * L)
{
	pll_datum  *pd = time_arg(L, 1);
	Oid			typoid = pd->ti->basetypoid;
	TimeFields	zf;
	struct pg_tm tm;
	fsec_t		fsec = 0;
	int			tz = 0;
	const char *tzn = NULL;
	bool		have_zone = !lua_isnoneornil(L, 2);

	if (have_zone && typoid != TIMESTAMPTZOID)
		pll_tj_error(L, ERRCODE_INVALID_PARAMETER_VALUE,
					 "a time zone can only be given for timestamp with time zone values");

	memset(&zf, 0, sizeof(zf));
	zf.isdst = -1;
	if (have_zone)
	{
		/* reuse the parser for the zone field */
		lua_newtable(L);
		lua_pushvalue(L, 2);
		lua_setfield(L, -2, "timezone");
		read_zone_fields(L, lua_gettop(L), &zf);
	}

	lua_createtable(L, 0, 12);

	PLL_TRY();
	{
		switch (typoid)
		{
			case TIMESTAMPTZOID:
				{
					pg_tz	   *tzp = zone_for(&zf);
					TimestampTz ts = DatumGetTimestampTz(pd->value);

					if (zf.zkind == ZONE_OFFSET)
					{
						Timestamp	local = ts + (Timestamp) zf.zoffset * USECS_PER_SEC;

						if (TIMESTAMP_NOT_FINITE(ts) ||
							timestamp2tm(local, NULL, &tm, &fsec, NULL, NULL) != 0)
							ereport(ERROR,
									(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
									 errmsg("timestamp out of range")));
						tm.tm_isdst = 0;
						tz = -zf.zoffset;
					}
					else
					{
						if (tzp == NULL)
							tzp = session_timezone;
						if (TIMESTAMP_NOT_FINITE(ts) ||
							timestamp2tm(ts, &tz, &tm, &fsec, &tzn, tzp) != 0)
							ereport(ERROR,
									(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
									 errmsg("timestamp out of range")));
					}
				}
				break;
			case TIMESTAMPOID:
				if (TIMESTAMP_NOT_FINITE(DatumGetTimestamp(pd->value)) ||
					timestamp2tm(DatumGetTimestamp(pd->value), NULL, &tm, &fsec,
								 NULL, NULL) != 0)
					ereport(ERROR,
							(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
							 errmsg("timestamp out of range")));
				break;
			case DATEOID:
				if (DATE_NOT_FINITE(DatumGetDateADT(pd->value)))
					ereport(ERROR,
							(errcode(ERRCODE_DATETIME_VALUE_OUT_OF_RANGE),
							 errmsg("date out of range")));
				j2date(DatumGetDateADT(pd->value) + POSTGRES_EPOCH_JDATE,
					   &tm.tm_year, &tm.tm_mon, &tm.tm_mday);
				break;
			case TIMEOID:
				time2tm(DatumGetTimeADT(pd->value), &tm, &fsec);
				break;
			case TIMETZOID:
				{
					TimeTzADT  *t = DatumGetTimeTzADTP(pd->value);

					timetz2tm(t, &tm, &fsec, &tz);
				}
				break;
			default:
				break;
		}
	}
	PLL_CATCH(L);

	switch (typoid)
	{
		case TIMESTAMPTZOID:
			table_from_tm(L, &tm, fsec);
			set_bool_field(L, "isdst", tm.tm_isdst > 0);
			set_int_field(L, "timezone", -tz);
			if (tzn != NULL)
				set_str_field(L, "timezone_abbrev", tzn);
			break;
		case TIMESTAMPOID:
			table_from_tm(L, &tm, fsec);
			break;
		case DATEOID:
			set_int_field(L, "year", tm.tm_year);
			set_int_field(L, "month", tm.tm_mon);
			set_int_field(L, "day", tm.tm_mday);
			break;
		case TIMEOID:
		case TIMETZOID:
			set_int_field(L, "hour", tm.tm_hour);
			set_int_field(L, "min", tm.tm_min);
			set_int_field(L, "sec", tm.tm_sec);
			set_int_field(L, "usec", fsec);
			if (typoid == TIMETZOID)
				set_int_field(L, "timezone", -tz);
			break;
		default:
			{
				Interval   *iv = DatumGetIntervalP(pd->value);
				int64		t = iv->time;

				set_int_field(L, "year", iv->month / MONTHS_PER_YEAR);
				set_int_field(L, "month", iv->month % MONTHS_PER_YEAR);
				set_int_field(L, "day", iv->day);
				set_int_field(L, "hour", t / USECS_PER_HOUR);
				t %= USECS_PER_HOUR;
				set_int_field(L, "min", t / USECS_PER_MINUTE);
				t %= USECS_PER_MINUTE;
				set_int_field(L, "sec", t / USECS_PER_SEC);
				set_int_field(L, "usec", t % USECS_PER_SEC);
			}
			break;
	}
	return 1;
}

/* ------------------------------------------------------------------------
 * The pl_lua.time module
 * ------------------------------------------------------------------------
 */

static int
time_new(lua_State * L)
{
	const char *name = luaL_checkstring(L, 1);
	Oid			typoid;
	pll_typeinfo *ti;
	Datum		d;

	luaL_checktype(L, 2, LUA_TTABLE);
	if (strcmp(name, "date") == 0)
		typoid = DATEOID;
	else if (strcmp(name, "time") == 0)
		typoid = TIMEOID;
	else if (strcmp(name, "timetz") == 0)
		typoid = TIMETZOID;
	else if (strcmp(name, "timestamp") == 0)
		typoid = TIMESTAMPOID;
	else if (strcmp(name, "timestamptz") == 0)
		typoid = TIMESTAMPTZOID;
	else if (strcmp(name, "interval") == 0)
		typoid = INTERVALOID;
	else
		return luaL_error(L, "unknown date/time type \"%s\"", name);

	ti = pll_typeinfo_push(L, typoid, -1);
	d = pll_time_todatum(L, 2, ti);
	pll_datum_push(L, ti, d);
	return 1;
}

static int
time_module_as_table(lua_State * L)
{
	(void) time_arg(L, 1);
	lua_settop(L, 2);
	return time_as_table(L);
}

static const luaL_Reg time_datum_methods[] = {
	{"__index", time_index},
	{"__tostring", pll_datum_tostring},
	{NULL, NULL}
};

static const luaL_Reg time_module_funcs[] = {
	{"new", time_new},
	{"as_table", time_module_as_table},
	{NULL, NULL}
};

int
pll_open_time(lua_State * L)
{
	luaL_newmetatable(L, PLL_MT_TIME);
	luaL_setfuncs(L, time_datum_methods, 0);
	pll_datum_add_compare(L);
	lua_pop(L, 1);

	luaL_newlib(L, time_module_funcs);
	return 1;
}
