import argparse
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta

import cv2
import numpy as np
import redis
import torch
import torch.nn.functional as F
from ultralytics import YOLO

from trackers.bytetrack.byte_tracker_depth import BYTETracker
from utils.track_utils import save_image
from video_depth_anything.video_depth_stream import VideoDepthAnything

torch.backends.cudnn.benchmark = True


# =============================
# Runtime / IO config
# =============================
# Resolve paths from this file instead of the shell's current working
# directory.  This keeps the project runnable both from the repository root
# and when launched by an external supervisor.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

RTSP_URL = os.getenv("RGBDSORT_RTSP_URL", "rtsp://127.0.0.1:8554/live")
FRAME_WIDTH = 2560
FRAME_HEIGHT = 1440
FPS = 10
FRAME_QUEUE_SIZE = 1000
QUEUE_WARN_THRESHOLD = 0.8
QUEUE_LOG_INTERVAL_SEC = 5
SAVE_CROP_WORKERS = 4
SAVE_CROP_QUEUE_SIZE = 512

OUTPUT_DIR = os.getenv("RGBDSORT_OUTPUT_DIR", os.path.join(PROJECT_ROOT, "runtime", "output"))
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

# Database access is optional.  Keep credentials out of source control and
# let deployments provide them through environment variables.
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "laboratory")
MYSQL_CAMERA_ID = int(os.getenv("MYSQL_CAMERA_ID", "1"))
MYSQL_LOCATION_ID = int(os.getenv("MYSQL_LOCATION_ID", "1"))
MYSQL_BIN = shutil.which("mysql")

MIN_TRACK_FRAMES = 3
MAX_CONSECUTIVE_NO_PERSON = 20

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
TIMESTAMP_SAMPLE = "2000-00-00 00:00:00"
TIMESTAMP_FONT = cv2.FONT_HERSHEY_SIMPLEX
TIMESTAMP_FONT_SCALE = 2.0
TIMESTAMP_THICKNESS = 2
TIMESTAMP_TEXT_X = 10
TIMESTAMP_TEXT_Y = 10
TIMESTAMP_SLOT_PAD = 2
TIMESTAMP_BLUE_MIN = 110
TIMESTAMP_BLUE_GAP = 45
TIMESTAMP_WHITE_MIN = 180
TIMESTAMP_WHITE_MAX_DELTA = 50
OCR_DEBUG_LOG_INTERVAL_SEC = 10
OCR_DEBUG_DUMP_EVERY_FAILS = 30
OCR_DEBUG_MAX_DUMPS = 20
OCR_DEBUG_DIR = os.getenv("RGBDSORT_OCR_DEBUG_DIR", "/tmp/rgbsort_ocr_debug")
QUEUE_STATUS_LOG_INTERVAL_SEC = 10

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)

frame_queue = queue.Queue(maxsize=FRAME_QUEUE_SIZE)
queue_drop_count = 0
last_queue_log_time = 0.0
event_file_lock = threading.Lock()
save_crop_queue = queue.Queue(maxsize=SAVE_CROP_QUEUE_SIZE)
save_crop_drop_count = 0
last_save_crop_log_time = 0.0
save_crop_workers_started = False
save_crop_workers_lock = threading.Lock()


# ============================================================
# 1. GPU mask-level depth
# ============================================================
@torch.no_grad()
def extract_mask_depths_gpu_iqr(
    result,
    depth_resized: torch.Tensor,
    sample_k: int = 4096,
    iqr_k: float = 1.5,
    use_sampling: bool = True,
):
    masks = result.masks
    if masks is None:
        return np.zeros((0,), dtype=np.float32)

    m = masks.data
    if m is None or m.numel() == 0:
        return np.zeros((0,), dtype=np.float32)

    m = m.bool().squeeze(1)
    num_masks = m.shape[0]
    if num_masks == 0:
        return np.zeros((0,), dtype=np.float32)

    device = depth_resized.device
    m = m.to(device)

    out = torch.full((num_masks,), float("nan"), device=device, dtype=torch.float32)

    for i in range(num_masks):
        mask_i = m[i]
        if not mask_i.any():
            continue

        vals = depth_resized[mask_i].float()
        vals = vals[torch.isfinite(vals)]
        if vals.numel() == 0:
            continue

        if use_sampling and vals.numel() > sample_k:
            idx = torch.randint(0, vals.numel(), (sample_k,), device=device)
            vals = vals[idx]

        if vals.numel() == 1:
            out[i] = vals[0]
            continue

        q1 = torch.quantile(vals, 0.25)
        q3 = torch.quantile(vals, 0.75)
        iqr = q3 - q1

        if iqr > 0:
            lower = q1 - iqr_k * iqr
            upper = q3 + iqr_k * iqr
            vals_f = vals[(vals >= lower) & (vals <= upper)]
            if vals_f.numel() > 0:
                vals = vals_f

        out[i] = vals.median()

    return out.detach().cpu().numpy().astype(np.float32)


# ============================================================
# 2. GPU pseudo RGB
# ============================================================
def depth_to_pseudo_rgb_gpu(depth_tensor):
    d = depth_tensor

    d_min = torch.quantile(d, 0.01)
    d_max = torch.quantile(d, 0.99)
    d = (d - d_min) / (d_max - d_min + 1e-8)
    d = d.clamp(0, 1)

    sobel_x = torch.tensor(
        [[1, 0, -1], [2, 0, -2], [1, 0, -1]],
        dtype=d.dtype,
        device=d.device,
    ).unsqueeze(0).unsqueeze(0)
    sobel_y = torch.tensor(
        [[1, 2, 1], [0, 0, 0], [-1, -2, -1]],
        dtype=d.dtype,
        device=d.device,
    ).unsqueeze(0).unsqueeze(0)
    d2 = d.unsqueeze(0).unsqueeze(0)

    gx = F.conv2d(d2, sobel_x, padding=1)
    gy = F.conv2d(d2, sobel_y, padding=1)
    grad = torch.sqrt(gx * gx + gy * gy).squeeze(0).squeeze(0)
    grad = grad / (grad.max() + 1e-8)

    lap_kernel = torch.tensor(
        [[0, 1, 0], [1, -4, 1], [0, 1, 0]],
        dtype=d.dtype,
        device=d.device,
    ).unsqueeze(0).unsqueeze(0)
    lap = F.conv2d(d2, lap_kernel, padding=1).squeeze(0).squeeze(0)
    lap = (lap - lap.min()) / (lap.max() - lap.min() + 1e-8)

    out = torch.stack([d, grad, lap], dim=2)
    return (out * 255).clamp(0, 255).byte().cpu().numpy()


# ============================================================
# 3. Detection fusion
# ============================================================
def _make_branch_weights(scores: torch.Tensor,
                         depth_vals_np: np.ndarray,
                         device,
                         branch: str = "rgb",
                         gate_threshold: float = 3.3) -> torch.Tensor:
    w = torch.ones_like(scores, device=device)
    if depth_vals_np is None or len(depth_vals_np) == 0:
        return w

    d_all = torch.as_tensor(depth_vals_np, device=device, dtype=torch.float32)
    length = min(d_all.numel(), w.numel())
    if length == 0:
        return w

    d = d_all[:length]
    w_sub = w[:length]

    valid = torch.isfinite(d)
    near = (d >= gate_threshold) & valid
    far = (d < gate_threshold) & valid

    if branch == "rgb":
        if near.any():
            w_sub[near] = 1
        if far.any():
            w_sub[far] = 0
    else:
        if near.any():
            w_sub[near] = w_sub[near] * d[near]
        if far.any():
            w_sub[far] = 0

    w[:length] = w_sub
    return w


def compute_iou_single(box1, box2):
    x1 = torch.max(box1[0], box2[0])
    y1 = torch.max(box1[1], box2[1])
    x2 = torch.min(box1[2], box2[2])
    y2 = torch.min(box1[3], box2[3])

    inter_w = (x2 - x1).clamp(min=0)
    inter_h = (y2 - y1).clamp(min=0)
    inter = inter_w * inter_h

    area1 = (box1[2] - box1[0]).clamp(min=0) * (box1[3] - box1[1]).clamp(min=0)
    area2 = (box2[2] - box2[0]).clamp(min=0) * (box2[3] - box2[1]).clamp(min=0)
    union = area1 + area2 - inter + 1e-6
    return inter / union


def dynamic_nms(boxes: torch.Tensor, scores: torch.Tensor, depths: torch.Tensor,
                iou_close: float = 0.3, iou_far: float = 0.6, depth_delta: float = 0.7):
    if boxes.numel() == 0:
        return torch.zeros((0,), dtype=torch.long, device=boxes.device)

    order = scores.argsort(descending=True)
    boxes_sorted = boxes[order]
    depths_sorted = depths[order]

    num_boxes = boxes_sorted.size(0)
    suppressed = torch.zeros(num_boxes, dtype=torch.bool, device=boxes.device)
    keep_local = []

    for i in range(num_boxes):
        if suppressed[i]:
            continue
        keep_local.append(i)
        box_i = boxes_sorted[i]
        depth_i = depths_sorted[i]

        for j in range(i + 1, num_boxes):
            if suppressed[j]:
                continue
            box_j = boxes_sorted[j]
            depth_j = depths_sorted[j]
            iou_2d = compute_iou_single(box_i, box_j)

            if torch.isfinite(depth_i) and torch.isfinite(depth_j):
                thr = iou_close if torch.abs(depth_i - depth_j) < depth_delta else iou_far
            else:
                thr = iou_close

            if iou_2d > thr:
                suppressed[j] = True

    keep_local = torch.as_tensor(keep_local, device=boxes.device, dtype=torch.long)
    return order[keep_local]


def fuse_results(res_rgb, depth_vals_rgb, res_d, depth_vals_d,
                 iou_close=0.3, iou_far=0.6, depth_delta=0.7,
                 gate_threshold: float = 3.3):
    b1, s1, c1 = res_rgb.boxes.xyxy, res_rgb.boxes.conf, res_rgb.boxes.cls
    b2, s2, c2 = res_d.boxes.xyxy, res_d.boxes.conf, res_d.boxes.cls

    if b1.numel() == 0 and b2.numel() == 0:
        return None

    device = b1.device if b1.numel() > 0 else b2.device

    depth_vals_rgb_t = None
    depth_vals_d_t = None
    if depth_vals_rgb is not None and len(depth_vals_rgb) > 0:
        depth_vals_rgb_t = torch.from_numpy(depth_vals_rgb).to(device=device, dtype=torch.float32)
    if depth_vals_d is not None and len(depth_vals_d) > 0:
        depth_vals_d_t = torch.from_numpy(depth_vals_d).to(device=device, dtype=torch.float32)

    s1_orig = s1.clone()
    s2_orig = s2.clone()

    w1 = _make_branch_weights(s1, depth_vals_rgb, device, branch="rgb", gate_threshold=gate_threshold)
    w2 = _make_branch_weights(s2, depth_vals_d, device, branch="dep", gate_threshold=gate_threshold)

    s1_adj = s1 * w1
    s2_adj = s2 * w2

    if s1_adj.numel() > 0:
        keep1 = s1_adj >= s1_orig
        b1 = b1[keep1]
        s1_adj = s1_adj[keep1]
        c1 = c1[keep1]
        if depth_vals_rgb_t is not None:
            depth_vals_rgb_t = depth_vals_rgb_t[keep1]

    if s2_adj.numel() > 0:
        keep2 = s2_adj >= s2_orig
        b2 = b2[keep2]
        s2_adj = s2_adj[keep2]
        c2 = c2[keep2]
        if depth_vals_d_t is not None:
            depth_vals_d_t = depth_vals_d_t[keep2]

    if b1.numel() == 0 and b2.numel() == 0:
        return None

    if b1.numel() > 0:
        n1 = b1.shape[0]
        d1 = torch.full((n1,), float("nan"), device=device, dtype=torch.float32)
        if depth_vals_rgb_t is not None and depth_vals_rgb_t.numel() > 0:
            l1 = min(n1, depth_vals_rgb_t.numel())
            d1[:l1] = depth_vals_rgb_t[:l1]
    else:
        d1 = torch.zeros((0,), device=device, dtype=torch.float32)

    if b2.numel() > 0:
        n2 = b2.shape[0]
        d2 = torch.full((n2,), float("nan"), device=device, dtype=torch.float32)
        if depth_vals_d_t is not None and depth_vals_d_t.numel() > 0:
            l2 = min(n2, depth_vals_d_t.numel())
            d2[:l2] = depth_vals_d_t[:l2]
    else:
        d2 = torch.zeros((0,), device=device, dtype=torch.float32)

    if b1.numel() > 0 and b2.numel() > 0:
        boxes = torch.cat([b1, b2], dim=0)
        scores = torch.cat([s1_adj, s2_adj], dim=0)
        classes = torch.cat([c1, c2], dim=0)
        depths = torch.cat([d1, d2], dim=0)
    elif b1.numel() > 0:
        boxes, scores, classes, depths = b1, s1_adj, c1, d1
    else:
        boxes, scores, classes, depths = b2, s2_adj, c2, d2

    final_b, final_s, final_c, final_d = [], [], [], []
    for cid in classes.unique():
        idx = classes == cid
        b_cls = boxes[idx]
        s_cls = scores[idx]
        d_cls = depths[idx]
        if b_cls.numel() == 0:
            continue

        keep_idx_local = dynamic_nms(
            b_cls,
            s_cls,
            d_cls,
            iou_close=iou_close,
            iou_far=iou_far,
            depth_delta=depth_delta,
        )

        final_b.append(b_cls[keep_idx_local])
        final_s.append(s_cls[keep_idx_local])
        final_d.append(d_cls[keep_idx_local])
        final_c.append(torch.full((len(keep_idx_local),), int(cid), device=device))

    if not final_b:
        return None

    return (
        torch.cat(final_b, dim=0),
        torch.cat(final_s, dim=0),
        torch.cat(final_c, dim=0),
        torch.cat(final_d, dim=0),
    )


# ============================================================
# 4. OCR timestamp / DB helpers
# ============================================================
def build_timestamp_decoder():
    (text_w, text_h), baseline = cv2.getTextSize(
        TIMESTAMP_SAMPLE,
        TIMESTAMP_FONT,
        TIMESTAMP_FONT_SCALE,
        TIMESTAMP_THICKNESS,
    )
    origin_y = TIMESTAMP_TEXT_Y + text_h
    roi_w = TIMESTAMP_TEXT_X + text_w + TIMESTAMP_SLOT_PAD + 2
    roi_h = origin_y + baseline + TIMESTAMP_SLOT_PAD + 2
    slots = []

    for idx, sample_char in enumerate(TIMESTAMP_SAMPLE):
        prev_w = cv2.getTextSize(
            TIMESTAMP_SAMPLE[:idx],
            TIMESTAMP_FONT,
            TIMESTAMP_FONT_SCALE,
            TIMESTAMP_THICKNESS,
        )[0][0]
        curr_w = cv2.getTextSize(
            TIMESTAMP_SAMPLE[:idx + 1],
            TIMESTAMP_FONT,
            TIMESTAMP_FONT_SCALE,
            TIMESTAMP_THICKNESS,
        )[0][0]
        slot_x1 = max(0, TIMESTAMP_TEXT_X + prev_w - TIMESTAMP_SLOT_PAD)
        slot_x2 = min(roi_w, TIMESTAMP_TEXT_X + curr_w + TIMESTAMP_SLOT_PAD)
        allowed_chars = "0123456789" if sample_char.isdigit() else sample_char
        templates = {}

        for char in allowed_chars:
            canvas = np.zeros((roi_h, roi_w), dtype=np.uint8)
            cv2.putText(
                canvas,
                char,
                (TIMESTAMP_TEXT_X + prev_w, origin_y),
                TIMESTAMP_FONT,
                TIMESTAMP_FONT_SCALE,
                255,
                TIMESTAMP_THICKNESS,
                cv2.LINE_AA,
            )
            template = canvas[:, slot_x1:slot_x2]
            templates[char] = (template > 32).astype(np.uint8)

        slots.append(
            {
                "slot_x1": slot_x1,
                "slot_x2": slot_x2,
                "allowed_chars": allowed_chars,
                "templates": templates,
            }
        )

    return {"roi_h": roi_h, "roi_w": roi_w, "slots": slots}


TIMESTAMP_DECODER = build_timestamp_decoder()


def extract_frame_timestamp(frame, return_debug=False):
    debug = {
        "reason": "",
        "parsed_text": "",
        "avg_digit_score": 0.0,
        "roi": None,
        "mask": None,
    }
    roi_h = TIMESTAMP_DECODER["roi_h"]
    roi_w = TIMESTAMP_DECODER["roi_w"]
    if frame is None or frame.shape[0] < roi_h or frame.shape[1] < roi_w:
        debug["reason"] = "frame_too_small"
        return (None, debug) if return_debug else None

    roi = frame[:roi_h, :roi_w]
    debug["roi"] = roi.copy()
    blue = roi[:, :, 0].astype(np.int16)
    green = roi[:, :, 1].astype(np.int16)
    red = roi[:, :, 2].astype(np.int16)
    blue_mask = (
        (blue >= TIMESTAMP_BLUE_MIN)
        & (blue - green >= TIMESTAMP_BLUE_GAP)
        & (blue - red >= TIMESTAMP_BLUE_GAP)
    )
    white_mask = (
        (blue >= TIMESTAMP_WHITE_MIN)
        & (green >= TIMESTAMP_WHITE_MIN)
        & (red >= TIMESTAMP_WHITE_MIN)
        & (
            np.maximum(np.maximum(blue, green), red)
            - np.minimum(np.minimum(blue, green), red)
            <= TIMESTAMP_WHITE_MAX_DELTA
        )
    )
    mask = np.logical_or(blue_mask, white_mask).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), dtype=np.uint8))
    debug["mask"] = mask.copy()

    chars = []
    digit_scores = []
    for slot in TIMESTAMP_DECODER["slots"]:
        slot_mask = (mask[:, slot["slot_x1"]:slot["slot_x2"]] > 0).astype(np.uint8)
        observed_count = int(slot_mask.sum())
        best_char = None
        best_score = -1.0

        for char in slot["allowed_chars"]:
            template = slot["templates"][char]
            union = np.logical_or(slot_mask, template).sum()
            score = 1.0 if union == 0 else np.logical_and(slot_mask, template).sum() / union
            if char.isdigit():
                count_delta = abs(observed_count - int(template.sum()))
                score -= count_delta / max(int(template.sum()), 1) * 0.05
            if score > best_score:
                best_char = char
                best_score = score

        if best_char is None:
            debug["reason"] = "slot_decode_failed"
            return (None, debug) if return_debug else None
        chars.append(best_char)
        if best_char.isdigit():
            digit_scores.append(best_score)

    parsed_text = "".join(chars)
    debug["parsed_text"] = parsed_text
    debug["avg_digit_score"] = sum(digit_scores) / len(digit_scores) if digit_scores else 0.0
    if digit_scores and debug["avg_digit_score"] < 0.30:
        debug["reason"] = "low_digit_score"
        return (None, debug) if return_debug else None

    try:
        ts = datetime.strptime(parsed_text, TIMESTAMP_FORMAT)
        debug["reason"] = "ok"
        return (ts, debug) if return_debug else ts
    except ValueError:
        debug["reason"] = "invalid_datetime"
        return (None, debug) if return_debug else None


def sql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def run_mysql(sql: str, *, fetch_last_line=False):
    if MYSQL_BIN is None or not MYSQL_USER:
        return None

    env = os.environ.copy()
    env["MYSQL_PWD"] = MYSQL_PASSWORD
    cmd = [
        MYSQL_BIN,
        "-h", MYSQL_HOST,
        "-P", str(MYSQL_PORT),
        "-u", MYSQL_USER,
        "-D", MYSQL_DATABASE,
        "-e", sql,
    ]
    if fetch_last_line:
        cmd.insert(1, "-B")
        cmd.insert(1, "-N")

    try:
        result = subprocess.run(
            cmd,
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        error_text = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        print(f"[mysql] failed: {error_text}")
        return None

    if not fetch_last_line:
        return True

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    return lines[-1]


def insert_event_to_db(start_time: datetime):
    if start_time is None:
        return None
    sql = (
        "INSERT INTO `events` (`start_time`, `end_time`, `camera_id`, `location_id`) "
        f"VALUES ('{start_time.strftime('%Y-%m-%d %H:%M:%S')}', NULL, "
        f"{MYSQL_CAMERA_ID}, {MYSQL_LOCATION_ID}); "
        "SELECT LAST_INSERT_ID();"
    )
    result = run_mysql(sql, fetch_last_line=True)
    return int(result) if result is not None else None


def update_event_end_time(event_id: int, end_time: datetime):
    if event_id is None or end_time is None:
        return
    sql = (
        "UPDATE `events` "
        f"SET `end_time` = '{end_time.strftime('%Y-%m-%d %H:%M:%S')}' "
        f"WHERE `id` = {int(event_id)};"
    )
    run_mysql(sql)


def update_event_start_time(event_id: int, start_time: datetime):
    if event_id is None or start_time is None:
        return
    sql = (
        "UPDATE `events` "
        f"SET `start_time` = '{start_time.strftime('%Y-%m-%d %H:%M:%S')}' "
        f"WHERE `id` = {int(event_id)};"
    )
    run_mysql(sql)


def insert_person_track_to_db(track_key: str, start_time: datetime, linked_event_id=None):
    if start_time is None:
        return None
    event_id_sql = "NULL" if linked_event_id is None else str(int(linked_event_id))
    sql = (
        "INSERT INTO `person_tracks` "
        "(`event_id`, `start_time`, `end_time`, `category`, `image_path`, `location_id`, `camera_id`, `track_key`) "
        f"VALUES ({event_id_sql}, '{start_time.strftime('%Y-%m-%d %H:%M:%S')}', NULL, NULL, NULL, "
        f"{MYSQL_LOCATION_ID}, {MYSQL_CAMERA_ID}, '{sql_escape(track_key)}'); "
        "SELECT LAST_INSERT_ID();"
    )
    result = run_mysql(sql, fetch_last_line=True)
    return int(result) if result is not None else None


def update_person_track_start_time(person_track_id: int, start_time: datetime):
    if person_track_id is None or start_time is None:
        return
    sql = (
        "UPDATE `person_tracks` "
        f"SET `start_time` = '{start_time.strftime('%Y-%m-%d %H:%M:%S')}' "
        f"WHERE `id` = {int(person_track_id)};"
    )
    run_mysql(sql)


def update_person_track_end_time(person_track_id: int, end_time: datetime):
    if person_track_id is None or end_time is None:
        return
    sql = (
        "UPDATE `person_tracks` "
        f"SET `end_time` = '{end_time.strftime('%Y-%m-%d %H:%M:%S')}' "
        f"WHERE `id` = {int(person_track_id)};"
    )
    run_mysql(sql)


def update_person_track_event_id(person_track_id: int, event_id: int):
    if person_track_id is None or event_id is None:
        return
    sql = (
        "UPDATE `person_tracks` "
        f"SET `event_id` = {int(event_id)} "
        f"WHERE `id` = {int(person_track_id)};"
    )
    run_mysql(sql)


def delete_person_track_from_db(person_track_id: int):
    if person_track_id is None:
        return
    sql = f"DELETE FROM `person_tracks` WHERE `id` = {int(person_track_id)};"
    run_mysql(sql)


def normalize_frame_time(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def get_backfilled_track_start_time(provisional_start: datetime, recognized_time: datetime) -> datetime:
    provisional_second = normalize_frame_time(provisional_start)
    recognized_second = normalize_frame_time(recognized_time)
    corrected_time = recognized_second
    if recognized_second > provisional_second:
        corrected_time = recognized_second - timedelta(seconds=1)
    return min(provisional_second, corrected_time)


def count_subfolders(main_dir):
    target_folders = ["indoor", "outdoor", "pass", "unclasses"]
    total_subfolders = 0

    for folder in target_folders:
        folder_path = os.path.join(main_dir, folder)
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            subfolders = [
                f for f in os.listdir(folder_path)
                if os.path.isdir(os.path.join(folder_path, f))
            ]
            total_subfolders += len(subfolders)
        else:
            return 0
    return total_subfolders


def prepare_event_dir(output_root: str, frame_time: datetime):
    event_time = frame_time.strftime("%Y%m%d %H%M%S")
    date_part, time_part = event_time.split(" ")
    event_dir = os.path.join(output_root, date_part, time_part)
    os.makedirs(os.path.join(event_dir, "unclasses"), exist_ok=True)
    os.makedirs(os.path.join(event_dir, "pics"), exist_ok=True)
    event_file_path = os.path.join(event_dir, f"event{date_part}{time_part}.txt")
    return event_dir, open(event_file_path, "w+", encoding="utf-8")


def enqueue_latest_frame(frame, capture_time, ocr_ok):
    global queue_drop_count, last_queue_log_time

    dropped = 0
    try:
        frame_queue.put_nowait((frame, capture_time, ocr_ok))
    except queue.Full:
        try:
            frame_queue.get_nowait()
            dropped = 1
        except queue.Empty:
            pass

        try:
            frame_queue.put_nowait((frame, capture_time, ocr_ok))
        except queue.Full:
            dropped = 1

    queue_size = frame_queue.qsize()
    queue_capacity = frame_queue.maxsize
    queue_ratio = queue_size / queue_capacity if queue_capacity else 0
    queue_drop_count += dropped

    now_ts = time.time()
    if now_ts - last_queue_log_time >= QUEUE_LOG_INTERVAL_SEC:
        if queue_ratio >= QUEUE_WARN_THRESHOLD or queue_drop_count > 0:
            print(
                f"[frame_queue] size={queue_size}/{queue_capacity}, "
                f"dropped={queue_drop_count} in last {QUEUE_LOG_INTERVAL_SEC}s"
            )
        queue_drop_count = 0
        last_queue_log_time = now_ts


def _save_crop_worker():
    while True:
        person_roi, image_name, person_dir, base_dir = save_crop_queue.get()
        try:
            save_image(person_roi, image_name, person_dir, base_dir)
        finally:
            save_crop_queue.task_done()


def ensure_save_crop_workers_started():
    global save_crop_workers_started

    if save_crop_workers_started:
        return

    with save_crop_workers_lock:
        if save_crop_workers_started:
            return
        for idx in range(SAVE_CROP_WORKERS):
            thread = threading.Thread(
                target=_save_crop_worker,
                name=f"save-crop-{idx}",
                daemon=True,
            )
            thread.start()
        save_crop_workers_started = True


def frame_capture_thread(rtsp_url):
    ocr_total = 0
    ocr_success = 0
    ocr_fail_streak = 0
    ocr_dump_count = 0
    last_queue_status_log_time = 0.0

    def _maybe_dump_ocr_failure(debug_info, capture_time):
        nonlocal ocr_dump_count
        if debug_info is None or ocr_dump_count >= OCR_DEBUG_MAX_DUMPS:
            return
        os.makedirs(OCR_DEBUG_DIR, exist_ok=True)
        stamp = capture_time.strftime("%Y%m%d_%H%M%S")
        dump_name = f"{stamp}_fail{ocr_dump_count:03d}"
        roi = debug_info.get("roi")
        mask = debug_info.get("mask")
        if roi is not None:
            cv2.imwrite(os.path.join(OCR_DEBUG_DIR, f"{dump_name}_roi.jpg"), roi)
        if mask is not None:
            cv2.imwrite(os.path.join(OCR_DEBUG_DIR, f"{dump_name}_mask.png"), mask)
        meta_path = os.path.join(OCR_DEBUG_DIR, f"{dump_name}.txt")
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(f"reason={debug_info.get('reason', '')}\n")
            f.write(f"parsed_text={debug_info.get('parsed_text', '')}\n")
            f.write(f"avg_digit_score={debug_info.get('avg_digit_score', 0.0):.4f}\n")
        ocr_dump_count += 1

    def _open_cap():
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, FPS)
        return cap

    cap = _open_cap()

    while True:
        if not cap.isOpened():
            print("正在连接到RTSP流...")
            time.sleep(3)
            cap.release()
            cap = _open_cap()
            continue

        start_time = datetime.now()
        ret, frame = cap.read()
        if not ret or frame is None:
            print("读帧失败，尝试重连...")
            time.sleep(1)
            cap.release()
            cap = _open_cap()
            continue

        capture_time, ocr_debug = extract_frame_timestamp(frame, return_debug=True)
        ocr_total += 1
        if capture_time is None:
            capture_time = normalize_frame_time(datetime.now())
            ocr_ok = False
            ocr_fail_streak += 1
            if ocr_fail_streak <= 3 or ocr_fail_streak % OCR_DEBUG_DUMP_EVERY_FAILS == 0:
                _maybe_dump_ocr_failure(ocr_debug, capture_time)
        else:
            capture_time = normalize_frame_time(capture_time)
            ocr_ok = True
            ocr_success += 1
            ocr_fail_streak = 0

        enqueue_latest_frame(frame, capture_time, ocr_ok)

        now_ts = time.time()
        if now_ts - last_queue_status_log_time >= QUEUE_STATUS_LOG_INTERVAL_SEC:
            print(
                f"[frame_queue_status] size={frame_queue.qsize()}/{frame_queue.maxsize}, "
                f"ocr_ok={ocr_success}/{ocr_total}, fail_streak={ocr_fail_streak}"
            )
            last_queue_status_log_time = now_ts

        frame_seconds = (datetime.now() - start_time).total_seconds()
        wait_time = max(0.0, 1.0 / FPS - frame_seconds)
        time.sleep(wait_time)


def save_crop_async(person_roi, image_name, person_dir, base_dir):
    global save_crop_drop_count, last_save_crop_log_time

    ensure_save_crop_workers_started()
    task = (person_roi.copy(), image_name, person_dir, base_dir)
    dropped = 0

    try:
        save_crop_queue.put_nowait(task)
    except queue.Full:
        try:
            save_crop_queue.get_nowait()
            save_crop_queue.task_done()
            dropped = 1
        except queue.Empty:
            pass

        try:
            save_crop_queue.put_nowait(task)
        except queue.Full:
            dropped += 1

    save_crop_drop_count += dropped
    now_ts = time.time()
    if now_ts - last_save_crop_log_time >= QUEUE_LOG_INTERVAL_SEC:
        if save_crop_drop_count > 0:
            print(
                f"[save_crop_queue] size={save_crop_queue.qsize()}/{save_crop_queue.maxsize}, "
                f"dropped={save_crop_drop_count} in last {QUEUE_LOG_INTERVAL_SEC}s"
            )
        save_crop_drop_count = 0
        last_save_crop_log_time = now_ts


class BTArgs:
    def __init__(self, track_thresh=0.25, match_thresh=0.8, track_buffer=30, w_app=0.45, app_gate=0.75):
        self.track_thresh = track_thresh
        self.match_thresh = match_thresh
        self.track_buffer = track_buffer
        self.w_app = w_app
        self.app_gate = app_gate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtsp_url", default=RTSP_URL)
    parser.add_argument("--input_size", type=int, default=384)
    parser.add_argument("--display_fps", type=int, default=15)
    parser.add_argument("--max_side", type=int, default=960)
    parser.add_argument("--output_dir", default=OUTPUT_DIR)
    parser.add_argument("--no_imshow", action="store_true")
    parser.add_argument("--bt_track_thresh", type=float, default=0.1)
    parser.add_argument("--bt_match_thresh", type=float, default=0.8)
    parser.add_argument("--bt_track_buffer", type=int, default=60)
    parser.add_argument("--depth_gate", type=float, default=1.2)
    parser.add_argument("--depth_lambda", type=float, default=0.05)
    parser.add_argument("--w_app", type=float, default=0.45)
    parser.add_argument("--app_gate", type=float, default=0.75)

    parser.add_argument("--roi", default="800,1000,1200,1100")
    parser.add_argument("--default_depth_threshold", type=float, default=2.5)
    parser.add_argument("--roi_percentile", type=float, default=50.0)
    args = parser.parse_args()

    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    depth_model = VideoDepthAnything(
        encoder="vits",
        features=64,
        out_channels=[48, 96, 192, 384],
    ).to(device).eval()
    weights_dir = os.path.join(PROJECT_ROOT, "weights")
    depth_model.load_state_dict(
        torch.load(os.path.join(weights_dir, "video_depth_anything_vits.pth"), map_location="cpu")
    )

    yolo_rgb = YOLO(os.path.join(weights_dir, "yolo11m-seg.pt"))
    yolo_depth = YOLO(os.path.join(weights_dir, "yolo11m_depth.pt"))
    yolo_rgb.model.fuse = lambda *a, **k: yolo_rgb.model
    yolo_depth.model.fuse = lambda *a, **k: yolo_depth.model
    yolo_rgb.model.half().to(device)
    yolo_depth.model.half().to(device)

    tracker = BYTETracker(
        BTArgs(
            track_thresh=args.bt_track_thresh,
            match_thresh=args.bt_match_thresh,
            track_buffer=args.bt_track_buffer,
            w_app=args.w_app,
            app_gate=args.app_gate,
        ),
        frame_rate=args.display_fps,
        depth_gate=args.depth_gate,
        depth_lambda=args.depth_lambda,
    )

    os.makedirs(args.output_dir, exist_ok=True)

    in_event = False
    event_dir = ""
    event_file = None
    event_db_id = None
    event_started_at = None
    absence_started_at = None
    consecutive_frames = 0
    last_roi_depth = None

    current_ids = set()
    trackers_files = {}
    first_time = {}
    person_dir = {}
    prev_positions = {}
    track_event_dir = {}
    track_db_id = {}
    track_frame_count = {}
    track_started_at = {}
    track_start_needs_backfill = {}
    track_image_sequence = {}
    pending_removed_ids = set()

    def _parse_roi(roi_str):
        if not roi_str:
            return None
        parts = [p.strip() for p in roi_str.split(",") if p.strip()]
        if len(parts) != 4:
            return None
        try:
            x1, y1, x2, y2 = [int(float(p)) for p in parts]
        except ValueError:
            return None
        if x2 <= x1 or y2 <= y1:
            return None
        return x1, y1, x2, y2

    roi_orig = _parse_roi(args.roi)

    capture_thread = threading.Thread(
        target=frame_capture_thread,
        args=(args.rtsp_url,),
        daemon=True,
    )
    capture_thread.start()

    warm_frame = None
    while warm_frame is None:
        try:
            warm_frame, _, _ = frame_queue.get(timeout=10.0)
        except queue.Empty:
            print("等待 RTSP 首帧...")
    warm_bgr = warm_frame.copy()
    h0, w0 = warm_bgr.shape[:2]
    if args.max_side > 0 and max(h0, w0) > args.max_side:
        scale = args.max_side / max(h0, w0)
        warm_bgr = cv2.resize(warm_bgr, (int(w0 * scale), int(h0 * scale)), interpolation=cv2.INTER_LINEAR)
    warm_rgb = cv2.cvtColor(warm_bgr, cv2.COLOR_BGR2RGB)

    with torch.no_grad():
        for _ in range(2):
            _ = depth_model.infer_video_depth_one(warm_rgb, args.input_size, device)
        _ = yolo_rgb(warm_rgb, classes=[0], verbose=False)[0]
        dummy_depth = torch.rand((warm_bgr.shape[0], warm_bgr.shape[1]), device=device)
        dummy_pseudo = depth_to_pseudo_rgb_gpu(dummy_depth)
        _ = yolo_depth(dummy_pseudo, classes=[0], verbose=False)[0]

    frame_count = 0

    try:
        while True:
            frame, frame_time, frame_time_ocr_ok = frame_queue.get()

            frame_bgr = frame
            orig_h, orig_w = frame_bgr.shape[:2]
            infer_frame = frame_bgr
            if args.max_side > 0 and max(orig_h, orig_w) > args.max_side:
                scale = args.max_side / max(orig_h, orig_w)
                infer_frame = cv2.resize(
                    frame_bgr,
                    (int(orig_w * scale), int(orig_h * scale)),
                    interpolation=cv2.INTER_LINEAR,
                )

            infer_h, infer_w = infer_frame.shape[:2]
            scale_w = orig_w / infer_w
            scale_h = orig_h / infer_h
            frame_rgb = cv2.cvtColor(infer_frame, cv2.COLOR_BGR2RGB)

            t_frame_start = time.time()
            depth = depth_model.infer_video_depth_one(frame_rgb, args.input_size, device)
            depth_tensor = torch.from_numpy(depth).to(device).float()

            roi_box_infer = None
            if roi_orig is not None:
                rx1_o, ry1_o, rx2_o, ry2_o = roi_orig
                rx1 = int(rx1_o / scale_w)
                ry1 = int(ry1_o / scale_h)
                rx2 = int(rx2_o / scale_w)
                ry2 = int(ry2_o / scale_h)
                rx1 = max(0, min(rx1, infer_w - 1))
                ry1 = max(0, min(ry1, infer_h - 1))
                rx2 = max(0, min(rx2, infer_w))
                ry2 = max(0, min(ry2, infer_h))
                if rx2 > rx1 and ry2 > ry1:
                    roi_box_infer = (rx1, ry1, rx2, ry2)
                    roi_patch = depth[ry1:ry2, rx1:rx2]
                    if roi_patch.size > 0:
                        roi_patch = roi_patch[np.isfinite(roi_patch)]
                        if roi_patch.size > 0:
                            last_roi_depth = float(np.percentile(roi_patch, args.roi_percentile))

            pseudo = depth_to_pseudo_rgb_gpu(depth_tensor)
            res_rgb = yolo_rgb(frame_rgb, classes=[0], verbose=False)[0]
            res_d = yolo_depth(pseudo, classes=[0], verbose=False)[0]

            depth_vals_rgb = np.zeros((0,), dtype=np.float32)
            depth_vals_d = np.zeros((0,), dtype=np.float32)
            base_masks = None
            if res_rgb.masks is not None and res_rgb.masks.data.numel() > 0:
                base_masks = res_rgb.masks
            elif res_d.masks is not None and res_d.masks.data.numel() > 0:
                base_masks = res_d.masks

            if base_masks is not None:
                h_m, w_m = base_masks.data.shape[-2:]
                depth_resized = F.interpolate(
                    depth_tensor.unsqueeze(0).unsqueeze(0),
                    size=(h_m, w_m),
                    mode="bilinear",
                    align_corners=False,
                ).squeeze(0).squeeze(0)

                if res_rgb.masks is not None and res_rgb.masks.data.numel() > 0:
                    depth_vals_rgb = extract_mask_depths_gpu_iqr(res_rgb, depth_resized, sample_k=4096, use_sampling=True)
                if res_d.masks is not None and res_d.masks.data.numel() > 0:
                    depth_vals_d = extract_mask_depths_gpu_iqr(res_d, depth_resized, sample_k=4096, use_sampling=True)

            gate_threshold = (last_roi_depth if last_roi_depth is not None else args.default_depth_threshold) + 0.4
            fused = fuse_results(res_rgb, depth_vals_rgb, res_d, depth_vals_d, gate_threshold=gate_threshold)
            if fused is None:
                boxes = torch.empty((0, 4), device=device)
                scores = torch.empty((0,), device=device)
                depths = torch.empty((0,), device=device)
            else:
                boxes, scores, _, depths = fused

            person_count = int(boxes.shape[0])
            if person_count > 0 and not in_event:
                in_event = True
                event_started_at = frame_time
                absence_started_at = None
                consecutive_frames = 0
                event_dir, event_file = prepare_event_dir(args.output_dir, frame_time)
                event_db_id = insert_event_to_db(event_started_at)
                print(f"[events] started: {event_started_at.strftime('%Y-%m-%d %H:%M:%S')}")
            elif person_count > 0 and in_event:
                consecutive_frames = 0
                absence_started_at = None
            elif person_count == 0 and in_event:
                if consecutive_frames == 0:
                    absence_started_at = frame_time
                consecutive_frames += 1
                if consecutive_frames >= MAX_CONSECUTIVE_NO_PERSON:
                    event_finished_at = absence_started_at or frame_time
                    update_event_end_time(event_db_id, event_finished_at)
                    in_event = False
                    if event_file is not None:
                        event_file.close()
                        event_file = None
                    peoples = count_subfolders(event_dir)
                    pic_dir = os.path.join(event_dir, "pics")
                    file_count = len(os.listdir(pic_dir)) if os.path.isdir(pic_dir) else 0
                    if (peoples == 1 or not os.path.exists(os.path.join(event_dir, "indoor"))) or file_count > 800:
                        if os.path.isdir(pic_dir):
                            shutil.rmtree(pic_dir)
                    event_db_id = None
                    event_started_at = None
                    absence_started_at = None
                    consecutive_frames = 0

            if boxes.numel() > 0:
                dets = torch.cat([boxes, scores.unsqueeze(1)], dim=1).detach().cpu().numpy().astype(np.float32)
                det_depths = depths.detach().cpu().numpy().astype(np.float32)
            else:
                dets = np.zeros((0, 5), dtype=np.float32)
                det_depths = np.zeros((0,), dtype=np.float32)

            tracks = tracker.update(
                dets,
                img_info=(infer_h, infer_w),
                img_size=(infer_h, infer_w),
                img_bgr=infer_frame,
                depths=det_depths,
            )

            event_pic_name = ""
            if in_event and event_dir:
                event_pic_name = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]
                cv2.imwrite(os.path.join(event_dir, "pics", f"{event_pic_name}.jpg"), frame_bgr)

            if roi_box_infer is not None:
                rx1, ry1, rx2, ry2 = roi_box_infer
                cv2.rectangle(infer_frame, (rx1, ry1), (rx2, ry2), (0, 165, 255), 2)
                cv2.putText(
                    infer_frame,
                    f"ROI p{args.roi_percentile:.0f}:{gate_threshold:.2f}",
                    (rx1, max(0, ry1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 165, 255),
                    1,
                )

            def process_track(track):
                tid = int(track.track_id)
                x1, y1, x2, y2 = track.tlbr
                x1i, y1i, x2i, y2i = int(x1), int(y1), int(x2), int(y2)
                center_x = (x1i + x2i) // 2
                center_y = (y1i + y2i) // 2
                dval = float(track.depth) if np.isfinite(track.depth) else 0.0

                if tid not in current_ids:
                    current_ids.add(tid)
                    first_time[tid] = frame_time.strftime("%Y%m%d_%H%M%S.%f")[:-3]
                    person_dir[tid] = f"{first_time[tid]}_ID{tid}"
                    track_event_dir[tid] = event_dir
                    full_dir = os.path.join(event_dir, "unclasses", person_dir[tid], "body")
                    os.makedirs(full_dir, exist_ok=True)
                    file_path = os.path.join(event_dir, "unclasses", person_dir[tid], f"{person_dir[tid]}.txt")
                    trackers_files[tid] = open(file_path, "w+", encoding="utf-8")
                    track_started_at[tid] = frame_time
                    track_start_needs_backfill[tid] = not frame_time_ocr_ok
                    track_frame_count[tid] = 0
                    track_db_id[tid] = insert_person_track_to_db(
                        person_dir[tid],
                        datetime.strptime(first_time[tid], "%Y%m%d_%H%M%S.%f"),
                        event_db_id,
                    )
                    prev_positions[tid] = (center_x, center_y)
                elif track_start_needs_backfill.get(tid) and frame_time_ocr_ok:
                    corrected_start_time = get_backfilled_track_start_time(track_started_at[tid], frame_time)
                    track_started_at[tid] = corrected_start_time
                    first_time[tid] = corrected_start_time.strftime("%Y%m%d_%H%M%S.%f")[:-3]
                    update_person_track_start_time(track_db_id.get(tid), corrected_start_time)
                    track_start_needs_backfill[tid] = False

                prev_x, prev_y = prev_positions[tid]
                delta_x = center_x - prev_x
                delta_y = center_y - prev_y
                prev_positions[tid] = (center_x, center_y)
                track_frame_count[tid] = track_frame_count.get(tid, 0) + 1

                x1o = int(x1i * scale_w)
                y1o = int(y1i * scale_h)
                x2o = int(x2i * scale_w)
                y2o = int(y2i * scale_h)
                w_box = x2o - x1o
                h_box = y2o - y1o

                trackers_files[tid].write(f"{x1o},{y1o},{w_box},{h_box},{dval:.4f},{orig_h},{orig_w}\n")
                trackers_files[tid].flush()

                if event_pic_name and event_file is not None:
                    with event_file_lock:
                        event_file.write(
                            f"{event_pic_name}.jpg,{tid},{x1o},{y1o},{w_box},{h_box},-1,-1,-1,-1\n"
                        )
                        event_file.flush()

                x1c = max(0, min(orig_w - 1, x1o))
                y1c = max(0, min(orig_h - 1, y1o))
                x2c = max(0, min(orig_w, x2o))
                y2c = max(0, min(orig_h, y2o))
                if x2c > x1c and y2c > y1c:
                    person_roi = frame_bgr[y1c:y2c, x1c:x2c]
                    if person_roi.size > 0:
                        track_dir = track_event_dir.get(tid, event_dir)
                        image_second = frame_time.strftime("%Y%m%d_%H%M%S")
                        last_second, image_index = track_image_sequence.get(tid, (None, -1))
                        image_index = image_index + 1 if last_second == image_second else 0
                        track_image_sequence[tid] = (image_second, image_index)
                        image_name = f"{image_second}_{image_index:03d}.jpg"
                        save_crop_async(
                            person_roi,
                            image_name,
                            person_dir[tid],
                            os.path.join(track_dir, "unclasses"),
                        )

                cv2.rectangle(infer_frame, (x1i, y1i), (x2i, y2i), (0, 255, 0), 2)
                cv2.putText(
                    infer_frame,
                    f"id{tid}|{float(track.score):.2f}|{dval:.2f}|dx{delta_x}|dy{delta_y}",
                    (x1i, max(0, y1i - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    1,
                )

            for track in tracks:
                process_track(track)

            for track in getattr(tracker, "removed_stracks", []):
                pending_removed_ids.add(int(track.track_id))

            for tid in list(pending_removed_ids):
                try:
                    writer = trackers_files.pop(tid, None)
                    if writer is not None:
                        writer.flush()
                        writer.close()

                    track_dir = track_event_dir.get(tid, event_dir)
                    person_track_id = track_db_id.get(tid)
                    frame_num = track_frame_count.get(tid, 0)

                    if frame_num == 0 and writer is None and tid not in person_dir and person_track_id is None:
                        pending_removed_ids.discard(tid)
                        continue

                    if frame_num < MIN_TRACK_FRAMES:
                        delete_person_track_from_db(person_track_id)
                        short_track_dir = os.path.join(track_dir, "unclasses", person_dir[tid])
                        shutil.rmtree(short_track_dir, ignore_errors=True)
                        print(f"[person_tracks] dropped short track: id={tid}, frames={frame_num}")
                    else:
                        removed_time = frame_time
                        if tid in track_started_at and removed_time < track_started_at[tid]:
                            removed_time = track_started_at[tid]
                        update_person_track_end_time(person_track_id, removed_time)
                        task_data = {
                            "person_track_id": person_track_id,
                            "event_dir": track_dir,
                            "person_dir": person_dir[tid],
                            "first_time": first_time[tid],
                        }
                        redis_client.rpush("missing_tasks", json.dumps(task_data, ensure_ascii=False))

                    prev_positions.pop(tid, None)
                    first_time.pop(tid, None)
                    person_dir.pop(tid, None)
                    track_db_id.pop(tid, None)
                    track_event_dir.pop(tid, None)
                    track_image_sequence.pop(tid, None)
                    track_frame_count.pop(tid, None)
                    track_started_at.pop(tid, None)
                    track_start_needs_backfill.pop(tid, None)
                    current_ids.discard(tid)
                    pending_removed_ids.discard(tid)
                except Exception as exc:
                    print(f"[person_tracks] cleanup failed: id={tid}, err={exc}")

            t_frame_end = time.time()
            frame_time_cost = t_frame_end - t_frame_start
            fps = 1.0 / frame_time_cost if frame_time_cost > 0 else 0.0

            fps_text = f"FPS:{fps:.1f}"
            (text_w, text_h), baseline = cv2.getTextSize(
                fps_text,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                2,
            )
            text_x = max(10, infer_w - text_w - 10)
            text_y = max(text_h + 10, infer_h - baseline - 10)
            cv2.putText(
                infer_frame,
                fps_text,
                (text_x, text_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 0, 0),
                2,
            )

            if not args.no_imshow:
                src = cv2.resize(
                    infer_frame,
                    (FRAME_HEIGHT // 3, FRAME_WIDTH // 3),
                    interpolation=cv2.INTER_CUBIC,
                )
                cv2.imshow("Tracking", src)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            frame_count += 1
    finally:
        if event_file is not None:
            event_file.close()
        for writer in trackers_files.values():
            try:
                writer.close()
            except Exception:
                pass
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
