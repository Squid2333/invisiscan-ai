-- name: ListTrackPoints :many
SELECT track_id, seq, frame_time, bbox_x, bbox_y, bbox_w, bbox_h, depth, det_score
FROM track_points WHERE track_id = $1 ORDER BY seq;
