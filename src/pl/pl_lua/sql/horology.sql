--
-- date and time values: construction from tables and field access
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse
SET timezone = 'GMT';
SET datestyle = 'ISO, YMD';

-- calendar fields
DO LANGUAGE pl_lua $$
  print(pgtype.timestamptz{ year=2019, month=4, day=22, hour=12, min=23, sec=34.1 })
  print(pgtype.timestamptz{ year=2019, month=4, day=22 })
  print(pgtype.timestamptz{ year=1919, month=4, day=22, hour=12, sec=1.5 })
  print(pgtype.timestamptz{ year=-4713, month=12, day=1, hour=12 })
  print(pgtype.timestamptz{ year=2019, month=4, day=22, hour=12, min=23, sec=34, usec=123456 })
  print(pgtype.timestamptz{ year=2019, month=4, day=22, hour=12, min=23, sec=34.1, msec=1, usec=1 })
  print(pgtype.timestamp{ year=2019, month=4, day=22, hour=12, min=23, sec=34.1 })
  print(pgtype.timestamp{ year=2000, month=1, day=1, sec=-1.5 })
  print(pgtype.date{ year=2019, month=4, day=22 })
  print(pgtype.date{ year=-4713, month=11, day=24 })
  print(pgtype.time{ hour=12, min=23, sec=34.1 })
  print(pgtype.time{ hour=12, min=120, sec=1 })
  print(pgtype.interval{ year=1, month=2, day=3, hour=4, min=5, sec=6, usec=7 })
  print(pgtype.interval{ hour=100 })
  print(pgtype.interval{ month=-30, day=-1 })
$$;

-- fields outside their normal ranges are normalized, except for intervals
DO LANGUAGE pl_lua $$
  print(pgtype.timestamp{ year=2019, month=16, day=22 })
  print(pgtype.timestamp{ year=2019, month=-4, day=22 })
  print(pgtype.timestamp{ year=2019, month=4, day=22, hour=12, min=720, sec=3661 })
  print(pgtype.timestamp{ year=2019, month=4, day=22, hour=24, min=0, sec=1 })
  print(pgtype.timestamp{ year=2019, month=4, day=22, hour=-1, min=0, sec=1 })
  print(pgtype.timestamp{ year=2019, month=4, day=0 })
  print(pgtype.date{ year=2019, month=4, day=22, hour=24 })
  print(pgtype.date{ year=2019, month=4, day=22, hour=24, min=1 })
  print(pgtype.time{ hour=25, min=23, sec=34.1 })
  print(pgtype.interval{ hour=30, min=70 })
$$;

-- epoch fields
DO LANGUAGE pl_lua $$
  print(pgtype.timestamptz{ epoch=0 })
  print(pgtype.timestamptz{ epoch=1555891200.000001 })
  print(pgtype.timestamptz{ epoch_msec=1555891200001 })
  print(pgtype.timestamptz{ epoch_usec=-1555891200000001 })
  print(pgtype.timestamptz{ epoch=1555891200, msec=-1, usec=1 })
  print(pgtype.timestamp{ epoch=1555891200 })
  print(pgtype.timestamp{ epoch=1555891200, timezone="America/Los_Angeles" })
  print(pgtype.timestamp{ epoch=1555891200, timezone="+0545" })
  print(pgtype.date{ epoch=1555891200, timezone="Pacific/Auckland" })
  print(pgtype.time{ epoch=3601 })
  print(pgtype.time{ epoch=-1 })
  print(pgtype.timetz{ epoch=3601, timezone=-3600 })
  print(pgtype.interval{ epoch=90061.5 })
  print(pgtype.timestamptz{ epoch=1/0 }, pgtype.timestamp{ epoch=-1/0 })
$$;

-- time zones
DO LANGUAGE pl_lua $$
  local f = { year=2019, month=4, day=22, hour=12, min=23, sec=34.1 }
  for _, z in ipairs{ 10800, -10800, "+0300", "-03:30", "+14", "America/Los_Angeles",
                      "Pacific/Auckland", "Asia/Kathmandu", true } do
    f.timezone = z
    print(tostring(z), pgtype.timestamptz(f))
  end
$$;

SET timezone = 'America/New_York';
DO LANGUAGE pl_lua $$
  print(pgtype.timestamptz{ year=2019, month=4, day=22, hour=12 })
  print(pgtype.timestamptz{ year=2019, month=1, day=22, hour=12 })
$$;
SET timezone = 'GMT';

-- ambiguous local times at the end of daylight saving time
DO LANGUAGE pl_lua $$
  local f = { year=2018, month=10, day=28, hour=1, min=30, timezone="Europe/London" }
  print(pgtype.timestamptz(f))
  f.isdst = true
  print(pgtype.timestamptz(f))
  f.isdst = false
  print(pgtype.timestamptz(f))
$$;

-- values in table form
DO LANGUAGE pl_lua $$
  local function show(v, zone)
    local t = v:as_table(zone)
    local keys = {}
    for k in pairs(t) do keys[#keys + 1] = k end
    table.sort(keys)
    local out = {}
    for _, k in ipairs(keys) do out[#out + 1] = k .. "=" .. tostring(t[k]) end
    print(tostring(v), table.concat(out, " "))
  end
  show(pgtype.timestamptz('2019-04-22 10:20:30.5+00'))
  show(pgtype.timestamptz('2019-04-22 10:20:30+00'), 'America/Los_Angeles')
  show(pgtype.timestamptz('2019-04-22 10:20:30+00'), 'Asia/Kathmandu')
  show(pgtype.timestamp('2019-04-22 10:20:30'))
  show(pgtype.date('2019-04-22'))
  show(pgtype.time('10:20:30.25'))
  show(pgtype.timetz('10:20:30+04'))
  show(pgtype.interval('P1Y2M3DT4H5M6.5S'))

  -- tables made by as_table are accepted as input
  local ts = pgtype.timestamptz('2018-10-28 01:30:00+00')
  print(pgtype.timestamptz(ts:as_table('Europe/London')))
  print(tostring(pgtype.timestamptz(ts:as_table('Europe/London'))) == tostring(ts))
  local iv = pgtype.interval('P1Y2M3DT4H5M6.5S')
  print(pgtype.interval(iv:as_table()))
$$;

-- field access
SET timezone = 'Europe/London';
DO LANGUAGE pl_lua $$
  local t = pgtype.timestamptz('1968-05-10 03:45:01.234567+01')
  for _, k in ipairs{ 'century', 'day', 'decade', 'dow', 'doy', 'epoch',
                      'epoch_msec', 'epoch_usec', 'hour', 'isodow', 'isoweek',
                      'isoyear', 'julian', 'microseconds', 'millennium', 'milliseconds', 'minute',
                      'month', 'quarter', 'second', 'timezone',
                      'timezone_hour', 'timezone_minute', 'week', 'year' } do
    print(k, t[k])
  end
$$;

SET timezone = 'UTC';
DO LANGUAGE pl_lua $$
  for _, v in ipairs{ pgtype.timestamp('1968-05-10 03:45:01.234567'),
                      pgtype.date('1968-05-10'),
                      pgtype.time('03:45:01.234567'),
                      pgtype.timetz('03:45:01.234567+01'),
                      pgtype.interval('P1Y2M3DT4H5M6.789001S') } do
    print(tostring(v), v.epoch, v.epoch_msec, v.epoch_usec, v.hour, v.second)
  end
$$;

-- the module
DO LANGUAGE pl_lua $$
  local tm = require 'pl_lua.time'
  print(tm.new('date', { year=2020, month=2, day=29 }))
  print(tm.as_table(pgtype.date('2020-02-29')).day)
$$;

-- values pass through function arguments and results
CREATE FUNCTION next_day(d date) RETURNS date LANGUAGE pl_lua AS $$
  local t = d:as_table()
  t.day = t.day + 1
  return t
$$;
SELECT next_day('2020-02-28'), next_day('2020-02-29'), next_day('2020-12-31');

-- comparison of date/time values of the same type
DO LANGUAGE pl_lua $$
  local d1, d2 = pgtype.date('2026-01-01'), pgtype.date('2026-06-01')
  print(d1 < d2, d1 <= d2, d2 < d1, d1 == pgtype.date('2026-01-01'), d1 == d2)
  local t1 = pgtype.timestamptz('2026-01-01 00:00:00+00')
  local t2 = pgtype.timestamptz('2026-01-01 01:00:00+00')
  print(t1 < t2, t1 <= t1, t1 == t2, t1 == pgtype.timestamptz('2026-01-01 05:30:00+05:30'))
  local i1, i2 = pgtype.interval('1 day'), pgtype.interval('25 hours')
  print(i1 < i2, i1 <= i2, i1 == pgtype.interval('24 hours'))
  -- values of different types are never equal, and cannot be ordered
  print(d1 == t1)
  print(pcall(function() return d1 < t1 end))
$$;
