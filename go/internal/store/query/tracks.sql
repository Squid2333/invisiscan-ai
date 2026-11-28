-- name: CreateTrack :one
INSERT INTO tracks (uuid, event_id, camera_id, tracker_id, started_at, clock_source, frame_count)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING id;

-- name: AddConfirmedTrackFrames :execrows
UPDATE tracks
SET frame_count = frame_count + sqlc.arg(frame_count)::integer, updated_at = now()
WHERE id = sqlc.arg(id) AND status = 'confirmed';

-- name: EndTrack :execrows
UPDATE tracks
SET status = 'ended', end_reason = $3, ended_at = $4, updated_at = now()
WHERE id = $1 AND status = $2 AND ended_at IS NULL;

-- name: GetTrack :one
SELECT id, uuid, event_id, camera_id, tracker_id, started_at, ended_at, frame_count,
       status, end_reason, clock_source, category, category_confidence, category_probs,
       attempts, last_error, next_attempt_at, locked_by, locked_until, created_at, updated_at
FROM tracks WHERE id = $1;
