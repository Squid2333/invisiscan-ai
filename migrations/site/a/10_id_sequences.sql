-- 仅在 A 执行一次。先执行 0001_init.sql，
-- 再运行本文件，然后才能向 subjects 或 face_templates 插入数据。
--
-- 两站点都会向 subjects 和 face_templates 插入数据，记录会双向复制。
-- 通过拆分自增序列避免主键冲突。
-- A 使用奇数 ID，云端使用偶数 ID，见 site/cloud/10_id_sequences.sql。
-- 外部接口使用 UUID 标识记录。

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
