-- name: GetOpenEventForCamera :one
SELECT id FROM events
WHERE camera_id = $1 AND status = 'open'
ORDER BY id DESC
LIMIT 1
FOR UPDATE;

-- name: CreateEvent :one
INSERT INTO events (uuid, camera_id, started_at)
VALUES ($1, $2, $3)
RETURNING id;

-- name: UpdateOpenEventForTrack :exec
UPDATE events
SET started_at = LEAST(started_at, $2), track_count = track_count + 1
WHERE id = $1 AND status = 'open';

-- name: SetNewEventTrackCount :exec
UPDATE events SET track_count = 1 WHERE id = $1;

-- name: CloseEvent :execrows
UPDATE events e
SET status = 'closed', closed_reason = $2,
    ended_at = GREATEST(e.started_at, COALESCE((SELECT max(ended_at) FROM tracks WHERE event_id = e.id), e.started_at))
WHERE e.id = $1 AND e.status = 'open';

-- name: GetEvent :one
SELECT id, uuid, camera_id, started_at, ended_at, status, closed_reason, track_count, created_at
FROM events WHERE id = $1;

-- name: EventExists :one
SELECT EXISTS (SELECT 1 FROM events WHERE uuid = $1);
