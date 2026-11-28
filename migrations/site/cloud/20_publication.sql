-- 仅在云端执行，创建供 A 订阅的 pub_cloud。
-- 需要在 postgresql.conf 里设置 wal_level = logical 和 max_slot_wal_keep_size。
-- 设置说明见 IMPLEMENTATION_PLAN.md 第 4.3 节。
--
-- 这些表由 invisiscan-main 写入。发布范围排除 DELETE 和 TRUNCATE。
-- subjects 和 face_templates 使用软删除。
-- A 的事件和识别日志引用 cameras、locations 和 subjects，
-- 因此云端的物理删除不能复制到 A。
--
-- 执行命令 psql -d invisiscan -f 20_publication.sql

BEGIN;

CREATE PUBLICATION pub_cloud
    FOR TABLE locations, cameras, attendance_rules, subjects, face_templates
    WITH (publish = 'insert, update');

-- A 的订阅使用这个低权限账号连接。用 \password invisiscan_repl 单独设置密码，
-- 在 pg_hba.conf 里仅放行 A 的隧道地址。
-- 初始同步需要 SELECT 权限。
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
