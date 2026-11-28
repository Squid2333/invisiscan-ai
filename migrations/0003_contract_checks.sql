-- 0003_contract_checks.sql 用数据库约束落实文档里的字段规则。
-- A 和云端均执行此迁移。
-- 0001_init.sql 必须保持原样，因为部署中的数据库可能已经执行过它。

BEGIN;

-- 只有人体图可以被选为预览图。
-- 分割图和对齐后的人脸图都不作为预览上传。
ALTER TABLE track_images
    ADD CONSTRAINT track_images_selected_body
    CHECK (NOT selected OR kind = 'body');

-- FIQA 质量分只适用于已对齐的人脸图。
ALTER TABLE track_images
    ADD CONSTRAINT track_images_quality_face
    CHECK (quality IS NULL OR kind = 'face');

ALTER TABLE track_images
    ADD CONSTRAINT track_images_seq_nonnegative
    CHECK (seq >= 0);

ALTER TABLE events
    ADD CONSTRAINT events_track_count_nonnegative
    CHECK (track_count >= 0);

ALTER TABLE tracks
    ADD CONSTRAINT tracks_frame_count_nonnegative
    CHECK (frame_count >= 0),
    ADD CONSTRAINT tracks_attempts_nonnegative
    CHECK (attempts >= 0);

ALTER TABLE face_embeddings
    ADD CONSTRAINT face_embeddings_attempts_nonnegative
    CHECK (attempts >= 0);

ALTER TABLE recordings
    ADD CONSTRAINT recordings_size_nonnegative
    CHECK (size_bytes IS NULL OR size_bytes >= 0);

ALTER TABLE pipeline_heartbeats
    ADD CONSTRAINT pipeline_heartbeats_pending_tasks_nonnegative
    CHECK (pending_tasks >= 0),
    ADD CONSTRAINT pipeline_heartbeats_pending_uploads_nonnegative
    CHECK (pending_uploads >= 0),
    ADD CONSTRAINT pipeline_heartbeats_disk_ratio_range
    CHECK (disk_used_ratio IS NULL OR disk_used_ratio BETWEEN 0 AND 1);

COMMIT;
