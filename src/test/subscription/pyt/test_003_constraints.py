# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that constraints work on the subscriber."""


def test_003_constraints(create_pg):
    # Initialize publisher node
    node_publisher = create_pg("publisher", allows_streaming="logical")

    # Create subscriber node
    node_subscriber = create_pg("subscriber")

    # Setup structure on publisher
    node_publisher.safe_sql(
        "CREATE TABLE tab_fk (bid int PRIMARY KEY);")
    node_publisher.safe_sql(
        "CREATE TABLE tab_fk_ref (id int PRIMARY KEY, junk text, "
        "bid int REFERENCES tab_fk (bid));")

    # Setup structure on subscriber; column order intentionally different
    node_subscriber.safe_sql(
        "CREATE TABLE tab_fk (bid int PRIMARY KEY);")
    node_subscriber.safe_sql(
        "CREATE TABLE tab_fk_ref (id int PRIMARY KEY, "
        "bid int REFERENCES tab_fk (bid), junk text);")

    # Setup logical replication
    publisher_connstr = (
        f"host={node_publisher.host} port={node_publisher.port} dbname=postgres"
    )
    node_publisher.safe_sql(
        "CREATE PUBLICATION tap_pub FOR ALL TABLES;")

    node_subscriber.safe_sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher_connstr}' "
        "PUBLICATION tap_pub WITH (copy_data = false)")

    node_publisher.wait_for_catchup("tap_sub")

    node_publisher.safe_sql(
        "INSERT INTO tab_fk (bid) VALUES (1);")
    # "junk" value is meant to be large enough to force out-of-line storage
    node_publisher.safe_sql(
        "INSERT INTO tab_fk_ref (id, bid, junk) "
        "VALUES (1, 1, repeat(pi()::text,20000));")

    node_publisher.wait_for_catchup("tap_sub")

    # Check data on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*), min(bid), max(bid) FROM tab_fk;")
    assert result == "1|1|1", "check replicated tab_fk inserts on subscriber"

    result = node_subscriber.safe_sql(
        "SELECT count(*), min(bid), max(bid) FROM tab_fk_ref;")
    assert result == "1|1|1", "check replicated tab_fk_ref inserts on subscriber"

    # Drop the fk on publisher
    node_publisher.safe_sql("DROP TABLE tab_fk CASCADE;")

    # Insert data
    node_publisher.safe_sql(
        "INSERT INTO tab_fk_ref (id, bid) VALUES (2, 2);")

    node_publisher.wait_for_catchup("tap_sub")

    # FK is not enforced on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*), min(bid), max(bid) FROM tab_fk_ref;")
    assert result == "2|1|2", "check FK ignored on subscriber"

    # Add replica trigger
    node_subscriber.safe_sql("""
CREATE FUNCTION filter_basic_dml_fn() RETURNS TRIGGER AS $$
BEGIN
    IF (TG_OP = 'INSERT') THEN
        IF (NEW.id < 10) THEN
            RETURN NEW;
        ELSE
            RETURN NULL;
        END IF;
    ELSIF (TG_OP = 'UPDATE') THEN
        RETURN NULL;
    ELSE
        RAISE WARNING 'Unknown action';
        RETURN NULL;
    END IF;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER filter_basic_dml_trg
    BEFORE INSERT OR UPDATE OF bid ON tab_fk_ref
    FOR EACH ROW EXECUTE PROCEDURE filter_basic_dml_fn();
ALTER TABLE tab_fk_ref ENABLE REPLICA TRIGGER filter_basic_dml_trg;
""")

    # Insert data
    node_publisher.safe_sql(
        "INSERT INTO tab_fk_ref (id, bid) VALUES (10, 10);")

    node_publisher.wait_for_catchup("tap_sub")

    # The trigger should cause the insert to be skipped on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*), min(bid), max(bid) FROM tab_fk_ref;")
    assert result == "2|1|2", \
        "check replica insert trigger applied on subscriber"

    # Update data
    node_publisher.safe_sql(
        "UPDATE tab_fk_ref SET bid = 2 WHERE bid = 1;")

    node_publisher.wait_for_catchup("tap_sub")

    # The trigger should cause the update to be skipped on subscriber
    result = node_subscriber.safe_sql(
        "SELECT count(*), min(bid), max(bid) FROM tab_fk_ref;")
    assert result == "2|1|2", \
        "check replica update column trigger applied on subscriber"

    # Update on a column not specified in the trigger, but it will trigger
    # anyway because logical replication ships all columns in an update.
    node_publisher.safe_sql(
        "UPDATE tab_fk_ref SET id = 6 WHERE id = 1;")

    node_publisher.wait_for_catchup("tap_sub")

    result = node_subscriber.safe_sql(
        "SELECT count(*), min(id), max(id) FROM tab_fk_ref;")
    assert result == "2|1|2", \
        "check column trigger applied even on update for other column"

    node_subscriber.stop("fast")
    node_publisher.stop("fast")
