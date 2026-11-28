-- name: RecoverConfirmedTracks :execrows
UPDATE tracks t
SET status = 'ended', end_reason = 'recovered',
    ended_at = GREATEST(t.started_at, (SELECT max(p.frame_time) FROM track_points p WHERE p.track_id = t.id)),
    locked_by = NULL, locked_until = NULL, next_attempt_at = now(), updated_at = now()
WHERE t.status = 'confirmed' AND t.camera_id = ANY(sqlc.arg(camera_ids)::bigint[]);

-- name: RecoverOpenEvents :execrows
UPDATE events e
SET status = 'closed', closed_reason = 'recovered',
    ended_at = GREATEST(e.started_at, (SELECT max(t.ended_at) FROM tracks t WHERE t.event_id = e.id))
WHERE e.status = 'open' AND e.camera_id = ANY(sqlc.arg(camera_ids)::bigint[]);

-- name: ListRecoveredWork :many
SELECT id, status, category FROM tracks
WHERE camera_id = ANY(sqlc.arg(camera_ids)::bigint[])
  AND (status = 'ended'
       OR (status = 'classified' AND category = 'indoor')
       OR (status = 'pending_reid' AND sqlc.arg(reid_enabled)::boolean))
ORDER BY ended_at, id;
