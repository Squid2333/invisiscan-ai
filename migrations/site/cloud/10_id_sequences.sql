-- site/cloud/10_id_sequences.sql: cloud database only. Run once, right after
-- 0001_init.sql and before any row is inserted into subjects or face_templates.
--
-- The cloud uses even ids; host A uses odd ids (site/a/10_id_sequences.sql).
-- See that file for the reason.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM subjects) OR EXISTS (SELECT 1 FROM face_templates) THEN
        RAISE EXCEPTION 'subjects or face_templates already has rows; split the id sequences before any insert';
    END IF;
END
$$;

ALTER TABLE subjects       ALTER COLUMN id SET START WITH 2 SET INCREMENT BY 2 RESTART;
ALTER TABLE face_templates ALTER COLUMN id SET START WITH 2 SET INCREMENT BY 2 RESTART;

COMMIT;
