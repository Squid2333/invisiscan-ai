-- 0003_contract_checks.sql: constraints that make documented field semantics
-- executable. This migration is applied on both host A and the cloud.
-- Keep 0001_init.sql immutable: deployed databases may already have it.

BEGIN;

-- A selected preview is always a body image. Segmentation and aligned-face
-- rows are never uploaded as previews.
ALTER TABLE track_images
    ADD CONSTRAINT track_images_selected_body
    CHECK (NOT selected OR kind = 'body');

-- FIQA quality is meaningful only for aligned face images.
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
