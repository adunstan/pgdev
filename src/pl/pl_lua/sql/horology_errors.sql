--
-- date and time values: invalid input
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse
SET timezone = 'GMT';
SET datestyle = 'ISO, YMD';

-- bad field values
DO LANGUAGE pl_lua $$ print(pgtype.interval{ hour="foo" }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ hour=1.2 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ hour=0/0 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ hour=1/0 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ sec=1/0 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ epoch=0/0 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.date{ epoch=1/0 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{}) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ epoch=1/0 }) $$;

-- bad time zones
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=2019, month=1, day=1, timezone="1234" }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=2019, month=1, day=1, timezone="Nowhere/Land" }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=2019, month=1, day=1, timezone=1234.5 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=2019, month=1, day=1, timezone=function() end }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=2019, month=1, day=1, timezone=false }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=2019, month=1, day=1, timezone=100000 }) $$;
\echo :LAST_ERROR_SQLSTATE

-- combinations of fields that are not allowed
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ epoch=0, epoch_msec=0 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ epoch=0, year=2019 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ epoch=0, hour=20 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=2019 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=2019, month=4 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.time{ min=10 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.time{ hour=20, sec=10 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.time{ year=2019, hour=20 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ epoch=0, timezone="UTC" }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timetz{ hour=20, timezone="America/Los_Angeles" }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.time{ hour=20, timezone=3600 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.date{ year=2019, month=4, day=22, timezone="UTC" }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ year=2019, month=4, day=22, timezone="UTC" }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ hour=1, timezone=0 }) $$;

-- out of range values
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ epoch=2^62 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ epoch=1e300 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ epoch_msec=math.mininteger }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ epoch_usec=math.maxinteger, usec=1 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ epoch=-300000000000 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamptz{ year=-5000, month=1, day=1 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.date{ year=-5000, month=1, day=1 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.date{ year=5874897, month=12, day=32 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.date{ year=2000, month=1, day=2147483647 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ year=2^32 + 2000, month=1, day=1 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ year=2000, month=1, day=1, sec=2^40 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp{ year=2000, month=1, day=1, msec=2^62 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ year=2^31 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ month=2147483647, year=1 }) $$;

-- values that are on the edge of the range still work
DO LANGUAGE pl_lua $$ print(pgtype.date{ year=5874897, month=12, day=31 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.date{ year=-4713, month=11, day=24 }) $$;
DO LANGUAGE pl_lua $$ print(pgtype.interval{ day=-2^31, hour=2^31 - 1 }) $$;

-- as_table and field access on the wrong types
DO LANGUAGE pl_lua $$ print(pgtype.timestamp('2019-04-22 10:20:30'):as_table('Europe/London')) $$;
DO LANGUAGE pl_lua $$ print(pgtype.time('03:45:01').dow) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp('2019-04-22 10:20:30').nonesuch) $$;
DO LANGUAGE pl_lua $$ print(pgtype.timestamp('infinity'):as_table()) $$;
