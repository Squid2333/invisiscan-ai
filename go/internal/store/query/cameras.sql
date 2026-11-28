-- name: ListEnabledCameras :many
SELECT id, location_id, name, stream_url, publish_path, width, height, fps,
       rotation, params, enabled, created_at, updated_at
FROM cameras
WHERE enabled
ORDER BY id;

-- name: LockCamera :one
SELECT id FROM cameras WHERE id = $1 FOR UPDATE;

-- name: TryOwnCamera :one
SELECT pg_try_advisory_lock(sqlc.arg(camera_id)::bigint)::boolean;
