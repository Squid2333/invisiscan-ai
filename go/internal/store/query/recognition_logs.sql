-- name: InsertRecognitionLog :one
INSERT INTO recognition_logs
    (uuid, track_id, event_id, camera_id, location_id, subject_id, matched_template_id,
     modality, occurred_at, action, score, rounds, decision)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
ON CONFLICT (track_id, modality) DO NOTHING
RETURNING id;

-- name: GetRecognitionLog :one
SELECT id, uuid, track_id, event_id, camera_id, location_id, subject_id, matched_template_id,
       modality, occurred_at, occurred_date, action, score, rounds, decision, created_at
FROM recognition_logs WHERE track_id = $1 AND modality = $2;
