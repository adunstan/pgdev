--
-- Trigger functions
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

CREATE TABLE orders (id serial PRIMARY KEY, price numeric, qty integer,
                     total numeric, note text);

-- modifying the new row
CREATE FUNCTION set_total() RETURNS trigger LANGUAGE pl_lua AS $$
  new.total = new.price * new.qty
$$;
CREATE TRIGGER orders_total BEFORE INSERT OR UPDATE ON orders
  FOR EACH ROW EXECUTE FUNCTION set_total();
INSERT INTO orders (price, qty) VALUES (2.50, 4), (1.25, 8);
UPDATE orders SET qty = 10 WHERE id = 1;
SELECT id, price, qty, total FROM orders ORDER BY id;

-- the fields of the trigger object
CREATE FUNCTION describe() RETURNS trigger LANGUAGE pl_lua AS $$
  local args = { ... }
  print(trigger.name, trigger.when, trigger.operation, trigger.op, trigger.level)
  print(trigger.relation.name, trigger.relation.namespace,
        trigger.relation.oid == spi.execute("SELECT 'orders'::regclass::oid AS o")[1].o)
  print(trigger.relation.attributes.id, trigger.relation.attributes.note)
  print(old ~= nil, new ~= nil, trigger.row == new, #args, args[1], args[2])
  if trigger.operation == 'delete' then
    print(rawequal(trigger.row, old))
    print(old.id)
  end
  if trigger.operation == 'update' then
    print(old.qty, new.qty)
  end
$$;
CREATE TRIGGER orders_describe AFTER INSERT OR UPDATE OR DELETE ON orders
  FOR EACH ROW EXECUTE FUNCTION describe('one', 'two');
INSERT INTO orders (price, qty) VALUES (1, 1);
UPDATE orders SET qty = 2 WHERE id = 3;
DELETE FROM orders WHERE id = 3;
DROP TRIGGER orders_describe ON orders;

-- statement-level triggers
CREATE FUNCTION stmt() RETURNS trigger LANGUAGE pl_lua AS $$
  print(trigger.when, trigger.operation, trigger.level, old, new, trigger.row)
$$;
CREATE TRIGGER orders_stmt BEFORE INSERT OR TRUNCATE ON orders
  FOR EACH STATEMENT EXECUTE FUNCTION stmt();
INSERT INTO orders (price, qty) VALUES (1, 1);
TRUNCATE orders;
DROP TRIGGER orders_stmt ON orders;

-- skipping the operation, and replacing the row
CREATE FUNCTION filter_rows() RETURNS trigger LANGUAGE pl_lua AS $$
  if new.qty < 0 then
    return nil
  elseif new.qty == 0 then
    return { id = new.id, price = new.price, qty = 1, note = 'was zero' }
  elseif new.qty == 99 then
    trigger.row = nil
    return
  elseif new.qty == 98 then
    local r = pgtype.orders(new)
    r.note = 'copied'
    return r
  end
$$;
CREATE TRIGGER orders_filter BEFORE INSERT ON orders
  FOR EACH ROW EXECUTE FUNCTION filter_rows();
INSERT INTO orders (price, qty) VALUES (1, -1), (2, 0), (3, 5), (4, 99), (5, 98);
SELECT price, qty, note FROM orders ORDER BY id;
DROP TRIGGER orders_filter ON orders;

-- assigning to trigger.row
CREATE FUNCTION assign_row() RETURNS trigger LANGUAGE pl_lua AS $$
  trigger.row = { id = new.id, price = 9, qty = 9 }
$$;
CREATE TRIGGER orders_assign BEFORE INSERT ON orders
  FOR EACH ROW EXECUTE FUNCTION assign_row();
INSERT INTO orders (price, qty) VALUES (1, 1);
SELECT price, qty FROM orders ORDER BY id DESC LIMIT 1;
DROP TRIGGER orders_assign ON orders;

CREATE FUNCTION readonly_fields() RETURNS trigger LANGUAGE pl_lua AS $$
  trigger.name = 'x'
$$;
CREATE TRIGGER orders_ro BEFORE INSERT ON orders
  FOR EACH ROW EXECUTE FUNCTION readonly_fields();
INSERT INTO orders (price, qty) VALUES (1, 1);
DROP TRIGGER orders_ro ON orders;

-- the function of a trigger can't be called directly
CREATE FUNCTION not_direct() RETURNS trigger LANGUAGE pl_lua AS $$ return nil $$;
SELECT not_direct();

-- INSTEAD OF triggers
CREATE VIEW order_view AS SELECT id, price, qty FROM orders;
CREATE FUNCTION view_insert() RETURNS trigger LANGUAGE pl_lua AS $$
  spi.execute('INSERT INTO orders (price, qty, note) VALUES ($1, $2, $3)',
              new.price, new.qty, 'via view')
  return new
$$;
CREATE TRIGGER order_view_ins INSTEAD OF INSERT ON order_view
  FOR EACH ROW EXECUTE FUNCTION view_insert();
INSERT INTO order_view (price, qty) VALUES (7, 7);
SELECT price, qty, note FROM orders WHERE note = 'via view';

-- triggers on tables with dropped columns
CREATE TABLE dropped (a integer, junk text, b integer);
ALTER TABLE dropped DROP COLUMN junk;
CREATE FUNCTION dropped_trig() RETURNS trigger LANGUAGE pl_lua AS $$
  new.b = new.a * 2
  print(trigger.relation.attributes.a, trigger.relation.attributes.b)
$$;
CREATE TRIGGER dropped_t BEFORE INSERT ON dropped
  FOR EACH ROW EXECUTE FUNCTION dropped_trig();
INSERT INTO dropped (a) VALUES (21);
SELECT * FROM dropped;

-- transition tables are visible to queries in the trigger function
CREATE TABLE trans (a integer);
CREATE FUNCTION trans_report() RETURNS trigger LANGUAGE pl_lua AS $$
  local q = trigger.operation == 'delete' and 'SELECT sum(a) AS s FROM oldtab'
            or 'SELECT sum(a) AS s FROM newtab'
  print(trigger.operation, spi.execute(q)[1].s)
$$;
CREATE TRIGGER trans_ins AFTER INSERT ON trans
  REFERENCING NEW TABLE AS newtab FOR EACH STATEMENT EXECUTE FUNCTION trans_report();
CREATE TRIGGER trans_del AFTER DELETE ON trans
  REFERENCING OLD TABLE AS oldtab FOR EACH STATEMENT EXECUTE FUNCTION trans_report();
INSERT INTO trans SELECT generate_series(1, 4);
DELETE FROM trans WHERE a > 2;

-- the trigger object can be iterated over
CREATE TABLE tp_t (a integer);
CREATE FUNCTION tp_trg() RETURNS trigger LANGUAGE pl_lua AS $$
  local keys = {}
  for k in pairs(trigger) do keys[#keys + 1] = k end
  table.sort(keys)
  print(table.concat(keys, ' '))
$$;
CREATE TRIGGER tp_trg BEFORE INSERT ON tp_t FOR EACH ROW EXECUTE FUNCTION tp_trg();
INSERT INTO tp_t VALUES (1);
DROP TABLE tp_t;
DROP FUNCTION tp_trg();

-- values that cannot be a row of the table, returned from a trigger
CREATE TABLE trg_bad (a integer, b text);
CREATE FUNCTION trg_bad_f() RETURNS trigger LANGUAGE pl_lua AS $$
  local n = tonumber((...))
  if n == 1 then return 5 end
  if n == 2 then return pgtype.text('x') end
  if n == 3 then return { a = 'not an integer' } end
  if n == 4 then return pgtype.integer(5), 6 end
  if n == 5 then trigger.row = 'string'; return end
  if n == 6 then return coroutine.create(function() end) end
  if n == 7 then return trigger end
  if n == 8 then return { a = 1, no_such_column = 2 } end
  return trigger.row
$$;
DO $$
DECLARE
  i integer;
BEGIN
  FOR i IN 1..8 LOOP
    EXECUTE 'DROP TRIGGER IF EXISTS trg_bad_t ON trg_bad';
    EXECUTE format('CREATE TRIGGER trg_bad_t BEFORE INSERT ON trg_bad FOR EACH ROW EXECUTE FUNCTION trg_bad_f(%s)', i);
    BEGIN
      INSERT INTO trg_bad VALUES (i, 'x');
      RAISE NOTICE 'case %: accepted', i;
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'case %: %', i, SQLERRM;
    END;
  END LOOP;
END $$;
SELECT * FROM trg_bad ORDER BY a;
DROP TABLE trg_bad;
DROP FUNCTION trg_bad_f();

-- a trigger object kept after the trigger returned is still harmless
CREATE TABLE trg_keep (a integer);
CREATE FUNCTION trg_keep_f() RETURNS trigger LANGUAGE pl_lua AS $$
  _G.kept = trigger
  return trigger.row
$$;
CREATE TRIGGER trg_keep_t BEFORE INSERT ON trg_keep FOR EACH ROW EXECUTE FUNCTION trg_keep_f();
INSERT INTO trg_keep VALUES (42);
CREATE FUNCTION trg_keep_use() RETURNS text LANGUAGE pl_lua AS $$
  local k = _G.kept
  return k.operation .. ' ' .. k.relation.name .. ' ' .. tostring(k.row.a) .. ' ' ..
         tostring(pcall(function() k.operation = 'x' end))
$$;
SELECT trg_keep_use();
DROP TABLE trg_keep;
DROP FUNCTION trg_keep_f();
DROP FUNCTION trg_keep_use();
