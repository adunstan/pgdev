--
-- Event trigger functions
--

SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS pl_lua;
RESET client_min_messages;
\set VERBOSITY terse

CREATE FUNCTION log_ddl() RETURNS event_trigger LANGUAGE pl_lua AS $$
  print(trigger.event, trigger.tag)
$$;
CREATE EVENT TRIGGER log_start ON ddl_command_start EXECUTE FUNCTION log_ddl();
CREATE EVENT TRIGGER log_end ON ddl_command_end EXECUTE FUNCTION log_ddl();
CREATE TABLE evt_test (a integer);
ALTER TABLE evt_test ADD COLUMN b integer;
DROP TABLE evt_test;
DROP EVENT TRIGGER log_start;
DROP EVENT TRIGGER log_end;

-- an event trigger function can't be called directly
SELECT log_ddl();
