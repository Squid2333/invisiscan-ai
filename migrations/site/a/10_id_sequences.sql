-- site/a/10_id_sequences.sql: host A only. Run once, right after 0001_init.sql
-- and before any row is inserted into subjects or face_templates.
--
-- Both sites insert into subjects and face_templates, and the rows are
-- replicated both ways. The identity sequences are split so ids never
-- collide: host A uses odd ids, the cloud uses even ids
-- (site/cloud/10_id_sequences.sql). External APIs use uuid, not id.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM subjects) OR EXISTS (SELECT 1 FROM face_templates) THEN
        RAISE EXCEPTION 'subjects or face_templates already has rows; split the id sequences before any insert';
    END IF;
END
$$;

ALTER TABLE subjects       ALTER COLUMN id SET START WITH 1 SET INCREMENT BY 2 RESTART;
ALTER TABLE face_templates ALTER COLUMN id SET START WITH 1 SET INCREMENT BY 2 RESTART;

COMMIT;
