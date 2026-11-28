-- 仅在云端执行一次。先执行 0001_init.sql，
-- 再运行本文件，然后才能向 subjects 或 face_templates 插入数据。
--
-- 云端使用偶数 ID，A 使用奇数 ID。
-- 原因见 site/a/10_id_sequences.sql。

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
