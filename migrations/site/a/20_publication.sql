-- site/a/20_publication.sql: host A only. Creates pub_a, which the cloud
-- subscribes to. Requires wal_level = logical and max_slot_wal_keep_size set
-- in postgresql.conf (IMPLEMENTATION_PLAN.md 4.3).
--
-- One publication holds every table host A writes. Rows written in the same
-- transaction (for example an auto-enrolled stranger: subjects +
-- face_templates + face_embeddings + recognition_logs + domain_events) then
-- reach the cloud in commit order.
--
-- DELETE and TRUNCATE are not published: host A keeps short-term data and
-- purges it locally, while the cloud keeps long-term data under its own
-- retention. subjects and face_templates use soft deletes, so no DELETE has
-- to cross either.
--
-- Usage: psql -d invisiscan -f 20_publication.sql

BEGIN;

CREATE PUBLICATION pub_a
    FOR TABLE events, tracks, track_points, track_images, recognition_logs, recordings,
              domain_events, pipeline_heartbeats, face_embeddings,
              subjects, face_templates
    WITH (publish = 'insert, update');

-- Low-privilege account the cloud subscription connects as. Set its password
-- separately (\password invisiscan_repl) and allow only the cloud tunnel IP
-- in pg_hba.conf. SELECT is needed for the initial table sync.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'invisiscan_repl') THEN
        CREATE ROLE invisiscan_repl WITH LOGIN REPLICATION;
    END IF;
END
$$;
GRANT SELECT ON events, tracks, track_points, track_images, recognition_logs, recordings,
                domain_events, pipeline_heartbeats, face_embeddings,
                subjects, face_templates
    TO invisiscan_repl;

COMMIT;
