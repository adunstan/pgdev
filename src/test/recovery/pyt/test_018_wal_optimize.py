# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test WAL replay when some operation has skipped WAL."""

# These tests exercise code that once violated the mandate described in
# src/backend/access/transam/README section "Skipping WAL for New
# RelFileLocator".  The tests work by committing some transactions, initiating
# an immediate shutdown, and confirming that the expected data survives
# recovery.  For many years, individual commands made the decision to skip WAL,
# hence the frequent appearance of COPY in these tests.

import os
import re

import pytest


def check_orphan_relfilenodes(node, test_name):
    """Assert the data dir contains exactly the relfilenodes pg_class expects."""
    db_oid = node.safe_sql("SELECT oid FROM pg_database WHERE datname = 'postgres'")
    prefix = f"base/{db_oid}/"
    filepaths_referenced = node.safe_sql(
        """
       SELECT pg_relation_filepath(oid) FROM pg_class
       WHERE reltablespace = 0 AND relpersistence <> 't' AND
       pg_relation_filepath(oid) IS NOT NULL;"""
    )

    dir_path = os.path.join(node.data_dir, prefix)
    on_disk = sorted(
        prefix + name for name in os.listdir(dir_path) if re.fullmatch(r"[0-9]+", name)
    )
    referenced = sorted(filepaths_referenced.split("\n"))
    assert on_disk == referenced, test_name


@pytest.mark.parametrize("wal_level", ["minimal", "replica"])
def test_018_wal_optimize(create_pg, wal_level):
    # We run this same test suite for both wal_level=minimal and replica.
    node = create_pg(f"node_{wal_level}", start=False)
    # The default (non-streaming) init sets wal_level = minimal and
    # max_wal_senders = 0; mirror that, overriding wal_level for the
    # replica case.
    node.append_conf(
        f"""
wal_level = {wal_level}
max_wal_senders = 0
max_prepared_transactions = 1
wal_log_hints = on
wal_skip_threshold = 0
#wal_debug = on
"""
    )
    node.start()

    # Setup
    tablespace_dir = node.basedir + "/tablespace_other"
    os.mkdir(tablespace_dir)

    # Test redo of CREATE TABLESPACE.
    #
    # The leading statements are run individually rather than as one
    # multi-statement string: psql sends each top-level statement
    # separately, so CREATE TABLESPACE runs outside any
    # transaction block, whereas an in-process multi-statement PQexec would
    # wrap them in one implicit transaction (CREATE TABLESPACE forbids that).
    # The trailing BEGIN..COMMIT is a single deliberately-grouped transaction,
    # so it is kept as one statement.
    node.safe_sql("CREATE TABLE moved (id int);")
    node.safe_sql("INSERT INTO moved VALUES (1);")
    node.safe_sql(f"CREATE TABLESPACE other LOCATION '{tablespace_dir}';")
    node.safe_sql(
        """
        BEGIN;
        ALTER TABLE moved SET TABLESPACE other;
        CREATE TABLE originated (id int);
        INSERT INTO originated VALUES (1);
        CREATE UNIQUE INDEX ON originated(id) TABLESPACE other;
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM moved;")
    assert result == "1", f"wal_level = {wal_level}, CREATE+SET TABLESPACE"
    result = node.safe_sql(
        """
        INSERT INTO originated VALUES (1) ON CONFLICT (id)
          DO UPDATE set id = originated.id + 1
          RETURNING id;"""
    )
    assert result == "2", f"wal_level = {wal_level}, CREATE TABLESPACE, CREATE INDEX"

    # Test direct truncation optimization.  No tuples.
    node.safe_sql(
        """
        BEGIN;
        CREATE TABLE trunc (id serial PRIMARY KEY);
        TRUNCATE trunc;
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM trunc;")
    assert result == "0", f"wal_level = {wal_level}, TRUNCATE with empty table"

    # Test truncation with inserted tuples within the same transaction.
    # Tuples inserted after the truncation should be seen.
    node.safe_sql(
        """
        BEGIN;
        CREATE TABLE trunc_ins (id serial PRIMARY KEY);
        INSERT INTO trunc_ins VALUES (DEFAULT);
        TRUNCATE trunc_ins;
        INSERT INTO trunc_ins VALUES (DEFAULT);
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*), min(id) FROM trunc_ins;")
    assert result == "1|2", f"wal_level = {wal_level}, TRUNCATE INSERT"

    # Same for prepared transaction.
    # Tuples inserted after the truncation should be seen.
    # PREPARE TRANSACTION ends the explicit transaction; COMMIT PREPARED must
    # then run outside any transaction block, so it is a separate statement.
    node.safe_sql(
        """
        BEGIN;
        CREATE TABLE twophase (id serial PRIMARY KEY);
        INSERT INTO twophase VALUES (DEFAULT);
        TRUNCATE twophase;
        INSERT INTO twophase VALUES (DEFAULT);
        PREPARE TRANSACTION 't';"""
    )
    node.safe_sql("COMMIT PREPARED 't';")
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*), min(id) FROM trunc_ins;")
    assert result == "1|2", f"wal_level = {wal_level}, TRUNCATE INSERT PREPARE"

    # Writing WAL at end of xact, instead of syncing.
    node.safe_sql(
        """
        SET wal_skip_threshold = '1GB';
        BEGIN;
        CREATE TABLE noskip (id serial PRIMARY KEY);
        INSERT INTO noskip (SELECT FROM generate_series(1, 20000) a) ;
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM noskip;")
    assert result == "20000", f"wal_level = {wal_level}, end-of-xact WAL"

    # Data file for COPY query in subsequent tests
    basedir = node.basedir
    copy_file = f"{basedir}/copy_data.txt"
    with open(copy_file, "a", encoding="utf-8") as fh:
        fh.write("20000,30000\n20001,30001\n20002,30002")

    # Test truncation with inserted tuples using both INSERT and COPY.  Tuples
    # inserted after the truncation should be seen.
    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE ins_trunc (id serial PRIMARY KEY, id2 int);
        INSERT INTO ins_trunc VALUES (DEFAULT, generate_series(1,10000));
        TRUNCATE ins_trunc;
        INSERT INTO ins_trunc (id, id2) VALUES (DEFAULT, 10000);
        COPY ins_trunc FROM '{copy_file}' DELIMITER ',';
        INSERT INTO ins_trunc (id, id2) VALUES (DEFAULT, 10000);
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM ins_trunc;")
    assert result == "5", f"wal_level = {wal_level}, TRUNCATE COPY INSERT"

    # Test truncation with inserted tuples using COPY.  Tuples copied after
    # the truncation should be seen.
    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE trunc_copy (id serial PRIMARY KEY, id2 int);
        INSERT INTO trunc_copy VALUES (DEFAULT, generate_series(1,3000));
        TRUNCATE trunc_copy;
        COPY trunc_copy FROM '{copy_file}' DELIMITER ',';
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM trunc_copy;")
    assert result == "3", f"wal_level = {wal_level}, TRUNCATE COPY"

    # Like previous test, but rollback SET TABLESPACE in a subtransaction.
    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE spc_abort (id serial PRIMARY KEY, id2 int);
        INSERT INTO spc_abort VALUES (DEFAULT, generate_series(1,3000));
        TRUNCATE spc_abort;
        SAVEPOINT s;
          ALTER TABLE spc_abort SET TABLESPACE other; ROLLBACK TO s;
        COPY spc_abort FROM '{copy_file}' DELIMITER ',';
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM spc_abort;")
    assert (
        result == "3"
    ), f"wal_level = {wal_level}, SET TABLESPACE abort subtransaction"

    # in different subtransaction patterns
    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE spc_commit (id serial PRIMARY KEY, id2 int);
        INSERT INTO spc_commit VALUES (DEFAULT, generate_series(1,3000));
        TRUNCATE spc_commit;
        SAVEPOINT s; ALTER TABLE spc_commit SET TABLESPACE other; RELEASE s;
        COPY spc_commit FROM '{copy_file}' DELIMITER ',';
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM spc_commit;")
    assert (
        result == "3"
    ), f"wal_level = {wal_level}, SET TABLESPACE commit subtransaction"

    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE spc_nest (id serial PRIMARY KEY, id2 int);
        INSERT INTO spc_nest VALUES (DEFAULT, generate_series(1,3000));
        TRUNCATE spc_nest;
        SAVEPOINT s;
            ALTER TABLE spc_nest SET TABLESPACE other;
            SAVEPOINT s2;
                ALTER TABLE spc_nest SET TABLESPACE pg_default;
            ROLLBACK TO s2;
            SAVEPOINT s2;
                ALTER TABLE spc_nest SET TABLESPACE pg_default;
            RELEASE s2;
        ROLLBACK TO s;
        COPY spc_nest FROM '{copy_file}' DELIMITER ',';
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM spc_nest;")
    assert (
        result == "3"
    ), f"wal_level = {wal_level}, SET TABLESPACE nested subtransaction"

    # Leading statements run individually (see CREATE TABLESPACE note above);
    # the BEGIN..COMMIT is one grouped transaction.
    node.safe_sql("CREATE TABLE spc_hint (id int);")
    node.safe_sql("INSERT INTO spc_hint VALUES (1);")
    node.safe_sql(
        """
        BEGIN;
        ALTER TABLE spc_hint SET TABLESPACE other;
        CHECKPOINT;
        SELECT * FROM spc_hint;  -- set hint bit
        INSERT INTO spc_hint VALUES (2);
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM spc_hint;")
    assert result == "2", f"wal_level = {wal_level}, SET TABLESPACE, hint bit"

    node.safe_sql(
        """
        BEGIN;
        CREATE TABLE idx_hint (c int PRIMARY KEY);
        SAVEPOINT q; INSERT INTO idx_hint VALUES (1); ROLLBACK TO q;
        CHECKPOINT;
        INSERT INTO idx_hint VALUES (1);  -- set index hint bit
        INSERT INTO idx_hint VALUES (2);
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    # Run the conflicting INSERT in-process; it must fail with a unique
    # violation.
    res = node.sql("INSERT INTO idx_hint VALUES (2);")
    assert (
        res.error_message is not None
    ), f"wal_level = {wal_level}, unique index LP_DEAD"
    assert re.search(
        r"violates unique", res.error_message
    ), f"wal_level = {wal_level}, unique index LP_DEAD message"

    # UPDATE touches two buffers for one row.
    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE upd (id serial PRIMARY KEY, id2 int);
        INSERT INTO upd (id, id2) VALUES (DEFAULT, generate_series(1,10000));
        COPY upd FROM '{copy_file}' DELIMITER ',';
        UPDATE upd SET id2 = id2 + 1;
        DELETE FROM upd;
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM upd;")
    assert (
        result == "0"
    ), f"wal_level = {wal_level}, UPDATE touches two buffers for one row"

    # Test consistency of COPY with INSERT for table created in the same
    # transaction.
    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE ins_copy (id serial PRIMARY KEY, id2 int);
        INSERT INTO ins_copy VALUES (DEFAULT, 1);
        COPY ins_copy FROM '{copy_file}' DELIMITER ',';
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM ins_copy;")
    assert result == "4", f"wal_level = {wal_level}, INSERT COPY"

    # Test consistency of COPY that inserts more to the same table using
    # triggers.  If the INSERTS from the trigger go to the same block data
    # is copied to, and the INSERTs are WAL-logged, WAL replay will fail when
    # it tries to replay the WAL record but the "before" image doesn't match,
    # because not all changes were WAL-logged.
    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE ins_trig (id serial PRIMARY KEY, id2 text);
        CREATE FUNCTION ins_trig_before_row_trig() RETURNS trigger
          LANGUAGE plpgsql as $$
          BEGIN
            IF new.id2 NOT LIKE 'triggered%' THEN
              INSERT INTO ins_trig
                VALUES (DEFAULT, 'triggered row before' || NEW.id2);
            END IF;
            RETURN NEW;
          END; $$;
        CREATE FUNCTION ins_trig_after_row_trig() RETURNS trigger
          LANGUAGE plpgsql as $$
          BEGIN
            IF new.id2 NOT LIKE 'triggered%' THEN
              INSERT INTO ins_trig
                VALUES (DEFAULT, 'triggered row after' || NEW.id2);
            END IF;
            RETURN NEW;
          END; $$;
        CREATE TRIGGER ins_trig_before_row_insert
          BEFORE INSERT ON ins_trig
          FOR EACH ROW EXECUTE PROCEDURE ins_trig_before_row_trig();
        CREATE TRIGGER ins_trig_after_row_insert
          AFTER INSERT ON ins_trig
          FOR EACH ROW EXECUTE PROCEDURE ins_trig_after_row_trig();
        COPY ins_trig FROM '{copy_file}' DELIMITER ',';
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM ins_trig;")
    assert result == "9", f"wal_level = {wal_level}, COPY with INSERT triggers"

    # Test consistency of INSERT, COPY and TRUNCATE in same transaction block
    # with TRUNCATE triggers.
    node.safe_sql(
        f"""
        BEGIN;
        CREATE TABLE trunc_trig (id serial PRIMARY KEY, id2 text);
        CREATE FUNCTION trunc_trig_before_stat_trig() RETURNS trigger
          LANGUAGE plpgsql as $$
          BEGIN
            INSERT INTO trunc_trig VALUES (DEFAULT, 'triggered stat before');
            RETURN NULL;
          END; $$;
        CREATE FUNCTION trunc_trig_after_stat_trig() RETURNS trigger
          LANGUAGE plpgsql as $$
          BEGIN
            INSERT INTO trunc_trig VALUES (DEFAULT, 'triggered stat before');
            RETURN NULL;
          END; $$;
        CREATE TRIGGER trunc_trig_before_stat_truncate
          BEFORE TRUNCATE ON trunc_trig
          FOR EACH STATEMENT EXECUTE PROCEDURE trunc_trig_before_stat_trig();
        CREATE TRIGGER trunc_trig_after_stat_truncate
          AFTER TRUNCATE ON trunc_trig
          FOR EACH STATEMENT EXECUTE PROCEDURE trunc_trig_after_stat_trig();
        INSERT INTO trunc_trig VALUES (DEFAULT, 1);
        TRUNCATE trunc_trig;
        COPY trunc_trig FROM '{copy_file}' DELIMITER ',';
        COMMIT;"""
    )
    node.stop("immediate")
    node.start()
    result = node.safe_sql("SELECT count(*) FROM trunc_trig;")
    assert (
        result == "4"
    ), f"wal_level = {wal_level}, TRUNCATE COPY with TRUNCATE triggers"

    # Test redo of temp table creation.
    node.safe_sql(
        """
        CREATE TEMP TABLE temp (id serial PRIMARY KEY, id2 text);"""
    )
    node.stop("immediate")
    node.start()
    check_orphan_relfilenodes(
        node, f"wal_level = {wal_level}, no orphan relfilenode remains"
    )
