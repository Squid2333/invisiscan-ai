-- 仅在 A 执行，创建供云端订阅的 pub_a。
-- 需要在 postgresql.conf 里设置 wal_level = logical 和 max_slot_wal_keep_size。
-- 设置说明见 IMPLEMENTATION_PLAN.md 第 4.3 节。
--
-- A 写入的表使用同一个发布。同一事务里的记录会按提交顺序到达云端。
-- 例如自动登记陌生人时，同一事务会写入 subjects、face_templates、
-- face_embeddings、recognition_logs 和 domain_events。
-- 这些记录一起复制到云端。
--
-- 发布范围排除 DELETE 和 TRUNCATE。A 在本地清理短期数据，
-- 云端按自己的保留策略管理长期数据。
-- subjects 和 face_templates 使用软删除，
-- 因此也无需把物理删除复制到云端。
--
-- 执行命令 psql -d invisiscan -f 20_publication.sql

BEGIN;

CREATE PUBLICATION pub_a
    FOR TABLE events, tracks, track_points, track_images, recognition_logs, recordings,
              domain_events, pipeline_heartbeats, face_embeddings,
              subjects, face_templates
    WITH (publish = 'insert, update');

-- 云端订阅使用这个低权限账号连接。用 \password invisiscan_repl 单独设置密码，
-- 在 pg_hba.conf 里仅放行云端隧道地址。
-- 初始同步需要 SELECT 权限。
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
