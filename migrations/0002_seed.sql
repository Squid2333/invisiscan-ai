-- 0002_seed.sql: the single location and camera of the old deployment.
-- The old code used MYSQL_LOCATION_ID=1 and MYSQL_CAMERA_ID=1; keep ids 1
-- so logs are easy to compare. Edit names and stream_url before applying.

BEGIN;

INSERT INTO locations (id, name, description)
OVERRIDING SYSTEM VALUE
VALUES (1, 'laboratory', 'Lab entrance (migrated from MYSQL_LOCATION_ID=1)');

INSERT INTO cameras (id, location_id, name, stream_url, publish_path,
                     width, height, fps, rotation, params)
OVERRIDING SYSTEM VALUE
VALUES (1, 1, 'lab-door', 'rtsp://127.0.0.1:8554/live', 'live',
        1440, 2560, 8, 270,
        -- Old RGBsort.py defaults. ROI is in output-resolution pixels.
        '{"roi": [800, 1000, 1200, 1100], "roi_percentile": 50,
          "default_depth_threshold": 2.5, "gate_offset": 0.4,
          "depth_filter_mode": "ge",
          "nms_iou_close": 0.3, "nms_iou_far": 0.6, "nms_depth_delta": 0.7}'::jsonb);

SELECT setval(pg_get_serial_sequence('locations', 'id'), 1);
SELECT setval(pg_get_serial_sequence('cameras', 'id'), 1);

-- Old rule: on time when occurred_at.time() <= 08:40:00.
INSERT INTO attendance_rules (location_id, late_after, effective_from)
VALUES (1, '08:40:00', '2000-01-01');

COMMIT;
