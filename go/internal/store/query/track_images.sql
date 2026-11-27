-- name: ListTrackImages :many
SELECT track_id, kind, seq, object_key, quality, selected, preview_key, uploaded_at, created_at
FROM track_images WHERE track_id = $1 ORDER BY kind, seq;
