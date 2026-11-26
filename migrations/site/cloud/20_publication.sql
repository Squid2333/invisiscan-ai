-- site/cloud/20_publication.sql: cloud database only. Creates pub_cloud,
-- which host A subscribes to. Requires wal_level = logical and
-- max_slot_wal_keep_size set in postgresql.conf (IMPLEMENTATION_PLAN.md 4.3).
--
-- Tables written by invisiscan-main. DELETE and TRUNCATE are not published:
-- subjects and face_templates use soft deletes, and host A's events and
-- recognition logs reference cameras, locations and subjects, so a delete
-- there must not reach A.
--
-- Usage: psql -d invisiscan -f 20_publication.sql

BEGIN;

CREATE PUBLICATION pub_cloud
    FOR TABLE locations, cameras, attendance_rules, subjects, face_templates
    WITH (publish = 'insert, update');

-- Low-privilege account host A's subscription connects as. Set its password
-- separately (\password invisiscan_repl) and allow only host A's tunnel IP
-- in pg_hba.conf. SELECT is needed for the initial table sync.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'invisiscan_repl') THEN
        CREATE ROLE invisiscan_repl WITH LOGIN REPLICATION;
    END IF;
END
$$;
GRANT SELECT ON locations, cameras, attendance_rules, subjects, face_templates
    TO invisiscan_repl;

COMMIT;
