--
-- Composite (row) values
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

CREATE TYPE rd_row AS (a integer, b text[], c text);

CREATE FUNCTION rd_show(rec rd_row) RETURNS text LANGUAGE pl_lua AS $$
  return tostring(rec.a) .. " " .. tostring(rec.b[1]) .. " " .. tostring(rec.c) ..
         " " .. tostring(rec[1]) .. " " .. tostring(rec.nosuch) .. " " .. tostring(rec[9])
$$;
SELECT rd_show(ROW(1, '{x,y}', 'z'));
SELECT rd_show(ROW(1, '{x,y}', NULL));

-- assignment
CREATE FUNCTION rd_modify(rec rd_row) RETURNS rd_row LANGUAGE pl_lua AS $$
  rec.a = rec.a + 1
  rec.c = nil
  rec.b = { "new", "value" }
  return rec
$$;
SELECT rd_modify(ROW(1, '{x,y}', 'z'));
SELECT * FROM rd_modify(ROW(1, '{x,y}', 'z'));
CREATE FUNCTION rd_modify_attno(rec rd_row) RETURNS rd_row LANGUAGE pl_lua AS $$
  rec[1] = 100
  rec[3] = "three"
  return rec
$$;
SELECT rd_modify_attno(ROW(1, '{x,y}', 'z'));
CREATE FUNCTION rd_modify_bad(rec rd_row) RETURNS rd_row LANGUAGE pl_lua AS $$
  rec.nosuch = 1
  return rec
$$;
SELECT rd_modify_bad(ROW(1, '{x,y}', 'z'));
CREATE FUNCTION rd_modify_badtype(rec rd_row) RETURNS rd_row LANGUAGE pl_lua AS $$
  rec.a = "notanumber"
  return rec
$$;
SELECT rd_modify_badtype(ROW(1, '{x,y}', 'z'));

-- iteration
CREATE FUNCTION rd_pairs(rec rd_row) RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  for name, value, attno in pairs(rec) do
    r[#r + 1] = name .. "/" .. attno .. "/" .. type(value)
  end
  return table.concat(r, " ")
$$;
SELECT rd_pairs(ROW(1, '{x,y}', 'z'));

-- dropped columns
CREATE TABLE rd_tab (a integer, dropme integer, c text);
INSERT INTO rd_tab VALUES (1, 2, 'three');
ALTER TABLE rd_tab DROP COLUMN dropme;
CREATE FUNCTION rd_dropped(rec rd_tab) RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  for name, value, attno in pairs(rec) do
    r[#r + 1] = name .. "/" .. attno .. "=" .. tostring(value)
  end
  r[#r + 1] = tostring(rec[2])
  r[#r + 1] = tostring(rec[3])
  return table.concat(r, " ")
$$;
SELECT rd_dropped(rd_tab) FROM rd_tab;

-- conversion to tables
CREATE FUNCTION rd_table(rec rd_row) RETURNS text LANGUAGE pl_lua AS $$
  local t = rec()
  local r = { tostring(t.a), tostring(t.c) }
  local t2 = rec{ null = "NULL" }
  r[#r + 1] = t2.c
  local t3 = rec(function(name, value, attno, row)
    if name == "a" then return value * 100 end
    if name == "c" then return nil end
    return value
  end)
  r[#r + 1] = tostring(t3.a)
  r[#r + 1] = tostring(t3.c)
  local t4 = rec("X")
  r[#r + 1] = t4.c
  local names = {}
  rec{ map = function(name) names[#names + 1] = name end, discard = true }
  r[#r + 1] = table.concat(names, ",")
  return table.concat(r, " ")
$$;
SELECT rd_table(ROW(1, '{x,y}', NULL));

-- construction
CREATE FUNCTION rd_make() RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(pgtype.rd_row({ a = 1, b = { "p", "q" }, c = "z" }))
  r[#r + 1] = tostring(pgtype.rd_row(2, { "p" }, "w"))
  r[#r + 1] = tostring(pgtype.rd_row({ a = 3 }))
  r[#r + 1] = tostring(pgtype.rd_row(pgtype.rd_row(4, nil, nil)))
  return table.concat(r, " ")
$$;
SELECT rd_make();
CREATE FUNCTION rd_make_bad() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.rd_row(1, 2))
$$;
SELECT rd_make_bad();

-- results: tables, rows, and one value per column
CREATE FUNCTION rd_ret_table() RETURNS rd_row LANGUAGE pl_lua AS $$
  return { a = 5, b = { "m" }, c = "n" }
$$;
CREATE FUNCTION rd_ret_values() RETURNS rd_row LANGUAGE pl_lua AS $$
  return 6, { "m" }, "n"
$$;
CREATE FUNCTION rd_ret_many() RETURNS rd_row LANGUAGE pl_lua AS $$
  return 1, 2, 3, 4
$$;
SELECT * FROM rd_ret_table();
SELECT * FROM rd_ret_values();
SELECT * FROM rd_ret_many();

-- output parameters and record results
CREATE FUNCTION rd_out(OUT x integer, OUT y text) LANGUAGE pl_lua AS $$
  return 1, "one"
$$;
SELECT * FROM rd_out();
CREATE FUNCTION rd_out_table(OUT x integer, OUT y text) LANGUAGE pl_lua AS $$
  return { x = 2, y = "two" }
$$;
SELECT * FROM rd_out_table();
CREATE FUNCTION rd_record() RETURNS record LANGUAGE pl_lua AS $$
  return 3, "three"
$$;
SELECT * FROM rd_record() AS (p integer, q text);

-- a null row, and rows with null columns
CREATE FUNCTION rd_nullrow() RETURNS rd_row LANGUAGE pl_lua AS $$
  return nil
$$;
SELECT rd_nullrow() IS NULL;
CREATE FUNCTION rd_isnull(rec rd_row) RETURNS boolean LANGUAGE pl_lua AS $$
  return rec == nil
$$;
SELECT rd_isnull(NULL), rd_isnull(ROW(NULL, NULL, NULL));

-- nested rows
CREATE TYPE rd_outer AS (id integer, inner_row rd_row);
CREATE FUNCTION rd_nested(o rd_outer) RETURNS text LANGUAGE pl_lua AS $$
  return o.id .. ":" .. o.inner_row.a .. ":" .. o.inner_row.c
$$;
SELECT rd_nested(ROW(1, ROW(2, '{a}', 'c')));
CREATE FUNCTION rd_nested_ret() RETURNS rd_outer LANGUAGE pl_lua AS $$
  return { id = 1, inner_row = { a = 2, c = "x" } }
$$;
SELECT rd_nested_ret();

-- nested composite and array values write through to the value that contains
-- them
CREATE TYPE rd_in AS (fred integer, jim numeric);
CREATE TYPE rd_mid AS (thingy text[], wotsit rd_in);
CREATE TYPE rd_top AS (foo text, bar rd_in, baz rd_mid);
CREATE FUNCTION rd_nested_write() RETURNS text LANGUAGE pl_lua AS $$
  local function new()
    return pgtype.rd_top("zzz", { fred = 1, jim = 2 },
                        { thingy = { "a", "b", "c" }, wotsit = { fred = 3, jim = 4 } })
  end
  local r = new()
  local out = {}
  r.baz.wotsit.jim = 10
  r.baz.thingy[2] = "k"
  out[#out + 1] = tostring(r)
  r = new()
  r.bar.fred = -1
  r.foo = "yyy"
  out[#out + 1] = tostring(r)
  r = new()
  local inner = r.baz.wotsit        -- hold on to an intermediate value
  inner.fred = 30
  inner.jim = 40
  out[#out + 1] = tostring(r)
  out[#out + 1] = tostring(r.baz.wotsit.fred)
  r = new()
  r.bar = { fred = 100, jim = 200 } -- replacing a whole column
  r.baz.thingy[1] = "@"
  out[#out + 1] = tostring(r)
  return table.concat(out, "\n")
$$;
SELECT rd_nested_write();

-- elements of an array of composites write through too
CREATE TYPE rd_pt AS (x integer, y integer);
CREATE FUNCTION rd_array_of_rows(p rd_pt[]) RETURNS rd_pt[] LANGUAGE pl_lua AS $$
  p[2].y = 99
  p[1].x = -1
  return p
$$;
SELECT rd_array_of_rows(ARRAY[ROW(1,2), ROW(3,4)]::rd_pt[]);

-- constructors
CREATE FUNCTION rd_ctor() RETURNS text LANGUAGE pl_lua AS $$
  local r = {}
  r[#r + 1] = tostring(pgtype.rd_pt())
  r[#r + 1] = tostring(pgtype.rd_pt(nil))
  r[#r + 1] = tostring(pgtype.rd_pt({ 1, 2 }))
  r[#r + 1] = tostring(pgtype.rd_pt("1", "2"))
  r[#r + 1] = tostring(pcall(pgtype.rd_pt, 1))
  return table.concat(r, " ")
$$;
SELECT rd_ctor();
CREATE FUNCTION rd_ctor_bad() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(pgtype.rd_pt(1))
$$;
SELECT rd_ctor_bad();

-- equality of rows
CREATE FUNCTION rd_eq() RETURNS text LANGUAGE pl_lua AS $$
  local a, b = pgtype.rd_pt(1, 2), pgtype.rd_pt(1, 2)
  return tostring(a == b) .. " " .. tostring(a == pgtype.rd_pt(2, 1))
$$;
SELECT rd_eq();

-- the length of a row is its number of columns, not counting dropped ones
CREATE TABLE rd_len_t (a integer, b text, c integer);
ALTER TABLE rd_len_t DROP COLUMN b;
CREATE FUNCTION rd_len(r rd_len_t) RETURNS integer LANGUAGE pl_lua AS $$ return #r $$;
SELECT rd_len(ROW(1, 2)::rd_len_t);
DROP FUNCTION rd_len(rd_len_t);
DROP TABLE rd_len_t;

-- too many values for a row
DO LANGUAGE pl_lua $$
  print(pcall(pgtype.rd_row, 1, 'x', 'y', 'z'))
  print(pcall(pgtype.rd_row, 1, 2, 3, 4, 5, 6, 7, 8, 9))
$$;

-- a metamethod that changes the type while it is being converted
CREATE TYPE rd_reent AS (a integer, b text, c integer);
CREATE FUNCTION rd_reent_f() RETURNS rd_reent LANGUAGE pl_lua AS $$
  return setmetatable({}, {__index = function(_, k)
    if k == 'a' then
      spi.execute("ALTER TYPE rd_reent ADD ATTRIBUTE d integer")
      spi.execute("ALTER TYPE rd_reent DROP ATTRIBUTE d")
      local r = spi.execute("SELECT ROW(1, 'x', 2)::rd_reent AS r")
      local _ = r[1].r.a
      return 1
    end
    return k == 'b' and 'x' or 3
  end})
$$;
SELECT rd_reent_f();
SELECT rd_reent_f();
DROP FUNCTION rd_reent_f();
DROP TYPE rd_reent;
