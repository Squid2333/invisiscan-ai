-- 0002_seed.sql 写入旧部署中的唯一地点和相机。
-- 此脚本仅在云端数据库执行。A 订阅 pub_cloud 后，
-- 会在初始同步时收到这些记录。
-- 旧版使用 MYSQL_LOCATION_ID=1 和 MYSQL_CAMERA_ID=1。沿用 ID 1 便于对照日志。
-- 执行前请修改名称和 stream_url。

BEGIN;

INSERT INTO locations (id, name, description)
OVERRIDING SYSTEM VALUE
VALUES (1, 'laboratory', 'Lab entrance (migrated from MYSQL_LOCATION_ID=1)');

INSERT INTO cameras (id, location_id, name, stream_url, publish_path,
                     width, height, fps, rotation, params)
OVERRIDING SYSTEM VALUE
VALUES (1, 1, 'lab-door', 'rtsp://127.0.0.1:8554/live', 'live',
        1440, 2560, 8, 270,
        -- 沿用旧版 RGBsort.py 的默认值。ROI 使用输出分辨率下的像素坐标。
        '{"roi": [800, 1000, 1200, 1100], "roi_percentile": 50,
          "default_depth_threshold": 2.5, "gate_offset": 0.4,
          "depth_filter_mode": "ge",
          "nms_iou_close": 0.3, "nms_iou_far": 0.6, "nms_depth_delta": 0.7}'::jsonb);

SELECT setval(pg_get_serial_sequence('locations', 'id'), 1);
SELECT setval(pg_get_serial_sequence('cameras', 'id'), 1);

-- 沿用旧规则。occurred_at.time() <= 08:40:00 时算准时。
INSERT INTO attendance_rules (location_id, late_after, effective_from)
VALUES (1, '08:40:00', '2000-01-01');

COMMIT;
