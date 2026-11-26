-- 0001_init.sql: initial schema for invisiscan-ai.
-- Target: PostgreSQL 16 + pgvector >= 0.7. Field semantics: docs/DATA_MODEL.md.
-- All times are timestamptz; the business time zone is Asia/Shanghai.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------------------------

CREATE TABLE locations (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text        NOT NULL UNIQUE,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cameras (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    location_id  bigint      NOT NULL REFERENCES locations (id),
    name         text        NOT NULL UNIQUE,
    stream_url   text        NOT NULL,           -- RTSP URL the pipeline reads
    publish_path text        NOT NULL UNIQUE,    -- MediaMTX path name
    -- Output resolution after rotation; track_points use this coordinate system.
    width        integer     NOT NULL CHECK (width > 0),
    height       integer     NOT NULL CHECK (height > 0),
    fps          real        NOT NULL CHECK (fps > 0),
    rotation     smallint    NOT NULL DEFAULT 0 CHECK (rotation IN (0, 90, 180, 270)),
    -- Per-camera overrides of algorithm defaults (ROI, depth gate, NMS, ...).
    params       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    enabled      boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Events and tracks
-- ---------------------------------------------------------------------------

CREATE TABLE events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid          uuid        NOT NULL UNIQUE,
    camera_id     bigint      NOT NULL REFERENCES cameras (id),
    -- Earliest start among confirmed tracks; may move earlier while open.
    started_at    timestamptz NOT NULL,
    -- Latest appearance among confirmed tracks; set when closed.
    ended_at      timestamptz,
    status        text        NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'closed')),
    closed_reason text
                  CHECK (closed_reason IN ('absence', 'recovered', 'shutdown')),
    track_count   integer     NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT events_closed_consistent CHECK (
        (status = 'open'   AND ended_at IS NULL     AND closed_reason IS NULL) OR
        (status = 'closed' AND ended_at IS NOT NULL AND closed_reason IS NOT NULL)
    ),
    CONSTRAINT events_time_order CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX events_camera_started ON events (camera_id, started_at);
CREATE INDEX events_open ON events (id) WHERE status = 'open';

CREATE TABLE tracks (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid                uuid        NOT NULL UNIQUE,
    event_id            bigint      NOT NULL REFERENCES events (id),
    camera_id           bigint      NOT NULL REFERENCES cameras (id),
    tracker_id          integer     NOT NULL,   -- in-process tracker ID, debug only
    started_at          timestamptz NOT NULL,
    ended_at            timestamptz,
    frame_count         integer     NOT NULL DEFAULT 0,
    status              text        NOT NULL DEFAULT 'confirmed'
                        CHECK (status IN ('confirmed', 'ended', 'discarded', 'classified',
                                          'pending_reid', 'identified', 'done', 'failed')),
    end_reason          text CHECK (end_reason IN ('lost', 'recovered', 'shutdown')),
    clock_source        text        NOT NULL DEFAULT 'rtcp'
                        CHECK (clock_source IN ('rtcp', 'fallback_local')),
    category            text CHECK (category IN ('indoor', 'outdoor', 'pass')),
    category_confidence real,
    category_probs      jsonb,
    attempts            integer     NOT NULL DEFAULT 0,
    last_error          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tracks_time_order CHECK (ended_at IS NULL OR ended_at >= started_at),
    CONSTRAINT tracks_ended_has_time CHECK (status = 'confirmed' OR ended_at IS NOT NULL),
    CONSTRAINT tracks_classified_has_category CHECK (
        status NOT IN ('classified', 'pending_reid', 'identified', 'done')
        OR category IS NOT NULL
    )
);
CREATE INDEX tracks_event ON tracks (event_id);
CREATE INDEX tracks_camera_started ON tracks (camera_id, started_at);
-- Reconcile scans non-terminal tracks only.
CREATE INDEX tracks_pending ON tracks (status, ended_at)
    WHERE status IN ('confirmed', 'ended', 'classified', 'pending_reid');

-- Replaces the old per-track <person_dir>.txt. Columns match the old txt
-- line "x,y,w,h,depth" so the LSTM input is unchanged:
--   bbox_*: tracker output (Kalman state) box, top-left + size, converted
--           back to the camera output resolution (cameras.width/height).
--   depth:  per-track depth EMA (alpha 0.8); 0 when never observed.
CREATE TABLE track_points (
    track_id   bigint      NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    seq        integer     NOT NULL CHECK (seq >= 0),
    frame_time timestamptz NOT NULL,
    bbox_x     integer     NOT NULL,
    bbox_y     integer     NOT NULL,
    bbox_w     integer     NOT NULL,
    bbox_h     integer     NOT NULL,
    depth      real        NOT NULL DEFAULT 0,
    det_score  real,
    PRIMARY KEY (track_id, seq)
);

CREATE TABLE track_images (
    track_id   bigint      NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    kind       text        NOT NULL CHECK (kind IN ('body', 'seg', 'face')),
    seq        integer     NOT NULL,   -- same numbering as track_points.seq
    object_key text        NOT NULL UNIQUE,
    quality    real,                   -- DSL-FIQA score, face images only
    selected   boolean     NOT NULL DEFAULT false,  -- 6-image sample for review
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (track_id, kind, seq)
);

-- ---------------------------------------------------------------------------
-- Identity and biometric templates
-- ---------------------------------------------------------------------------

CREATE TABLE subjects (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid        uuid        NOT NULL UNIQUE,
    name        text,       -- NULL for auto-enrolled strangers; UI shows uuid prefix
    kind        text        NOT NULL CHECK (kind IN ('known', 'unknown')),
    external_id text        UNIQUE,   -- legacy users.id for migrated known people
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    active      boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT subjects_known_has_name CHECK (kind = 'unknown' OR name IS NOT NULL)
);

CREATE TABLE face_templates (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid              uuid        NOT NULL UNIQUE,
    subject_id        bigint      NOT NULL REFERENCES subjects (id),
    -- AdaFace ir_50 output, already unit length. NULL only for inactive
    -- legacy images where MTCNN found no face (kept for manual review).
    embedding         vector(512),
    model_name        text        NOT NULL,
    quality           real,
    object_key        text        NOT NULL UNIQUE,
    source            text        NOT NULL
                      CHECK (source IN ('legacy_import', 'auto_enroll', 'manual')),
    source_track_id   bigint      REFERENCES tracks (id),
    legacy_picture_id bigint      UNIQUE,
    active            boolean     NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT face_templates_active_has_embedding
        CHECK (NOT active OR embedding IS NOT NULL)
);
CREATE INDEX face_templates_subject ON face_templates (subject_id);
CREATE INDEX face_templates_model_active ON face_templates (model_name) WHERE active;
CREATE INDEX face_templates_embedding_hnsw ON face_templates
    USING hnsw (embedding vector_cosine_ops) WHERE active;

-- Reserved for ReID; dimension to be fixed once the model is chosen.
CREATE TABLE body_templates (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid            uuid        NOT NULL UNIQUE,
    subject_id      bigint      NOT NULL REFERENCES subjects (id),
    embedding       vector(768) NOT NULL,
    model_name      text        NOT NULL,
    quality         real,
    object_key      text        NOT NULL UNIQUE,
    source_track_id bigint      REFERENCES tracks (id),
    active          boolean     NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX body_templates_subject ON body_templates (subject_id);

-- ---------------------------------------------------------------------------
-- Recognition logs and attendance rules
-- ---------------------------------------------------------------------------

CREATE TABLE recognition_logs (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid                uuid        NOT NULL UNIQUE,
    track_id            bigint      NOT NULL REFERENCES tracks (id),
    event_id            bigint      NOT NULL REFERENCES events (id),
    camera_id           bigint      NOT NULL REFERENCES cameras (id),
    location_id         bigint      NOT NULL REFERENCES locations (id),
    subject_id          bigint      REFERENCES subjects (id),
    matched_template_id bigint,     -- face_templates.id or body_templates.id per modality
    modality            text        NOT NULL CHECK (modality IN ('face', 'body')),
    occurred_at         timestamptz NOT NULL,   -- track started_at
    occurred_date       date GENERATED ALWAYS AS
                        ((occurred_at AT TIME ZONE 'Asia/Shanghai')::date) STORED,
    action              text        NOT NULL CHECK (action IN ('enter', 'exit')),
    score               real        NOT NULL,   -- similarity, higher = more alike
    rounds              smallint    NOT NULL DEFAULT 1 CHECK (rounds IN (1, 2)),
    decision            text        NOT NULL
                        CHECK (decision IN ('matched', 'enrolled', 'unmatched', 'no_face')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT recognition_logs_one_per_track UNIQUE (track_id, modality),
    CONSTRAINT recognition_logs_subject_consistent CHECK (
        (decision IN ('matched', 'enrolled') AND subject_id IS NOT NULL) OR
        (decision IN ('unmatched', 'no_face') AND subject_id IS NULL)
    )
);
CREATE INDEX recognition_logs_date ON recognition_logs (location_id, occurred_date);
CREATE INDEX recognition_logs_subject ON recognition_logs (subject_id, occurred_at);

CREATE TABLE attendance_rules (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    location_id    bigint NOT NULL REFERENCES locations (id),
    -- Old rule: occurred_at.time() <= late_after is on time, later is late.
    late_after     time   NOT NULL,
    effective_from date   NOT NULL,
    effective_to   date,
    CONSTRAINT attendance_rules_range CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE VIEW v_attendance AS
SELECT l.*,
       (l.occurred_at AT TIME ZONE 'Asia/Shanghai')::time > r.late_after AS is_late
FROM recognition_logs l
JOIN attendance_rules r
  ON r.location_id = l.location_id
 AND l.occurred_date BETWEEN r.effective_from AND COALESCE(r.effective_to, 'infinity'::date);

-- ---------------------------------------------------------------------------
-- Recordings (MediaMTX segments)
-- ---------------------------------------------------------------------------

CREATE TABLE recordings (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    camera_id        bigint      NOT NULL REFERENCES cameras (id),
    object_key       text        NOT NULL UNIQUE,
    started_at       timestamptz NOT NULL,   -- parsed from the segment file name
    ended_at         timestamptz NOT NULL,   -- started_at + MTX_SEGMENT_DURATION
    duration_seconds double precision NOT NULL CHECK (duration_seconds > 0),
    size_bytes       bigint,
    deleted_at       timestamptz,            -- file removed by recordDeleteAfter
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT recordings_time_order CHECK (ended_at > started_at)
);
CREATE INDEX recordings_camera_time ON recordings (camera_id, started_at, ended_at);

COMMIT;
