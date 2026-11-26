import argparse
import json
import os
import queue
import shutil
import threading
import time
from datetime import datetime

import cv2
import numpy as np
import redis
import torch
import torch.nn.functional as F
from ultralytics import YOLO
from ultralytics.engine.results import Boxes
from ultralytics.trackers.bot_sort import BOTSORT

from sort import (
    FRAME_HEIGHT,
    FRAME_WIDTH,
    MAX_CONSECUTIVE_NO_PERSON,
    MIN_TRACK_FRAMES,
    OUTPUT_DIR,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PORT,
    RTSP_URL,
    count_subfolders,
    delete_person_track_from_db,
    dynamic_nms,
    event_file_lock,
    extract_mask_depths_gpu_iqr,
    frame_capture_thread,
    frame_queue,
    get_backfilled_track_start_time,
    insert_event_to_db,
    insert_person_track_to_db,
    prepare_event_dir,
    save_crop_async,
    update_event_end_time,
    update_event_start_time,
    update_person_track_end_time,
    update_person_track_event_id,
    update_person_track_start_time,
)
from video_depth_anything.video_depth_stream import VideoDepthAnything

torch.backends.cudnn.benchmark = True

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_DIR = os.path.join(PROJECT_ROOT, "weights")


def extract_box_depths_iqr(
    depth_tensor: torch.Tensor,
    boxes_xyxy: torch.Tensor,
    sample_k: int = 4096,
    iqr_k: float = 1.5,
):
    if boxes_xyxy.numel() == 0:
        return np.zeros((0,), dtype=np.float32)

    height, width = depth_tensor.shape[-2:]
    out = torch.full(
        (boxes_xyxy.shape[0],),
        float("nan"),
        device=depth_tensor.device,
        dtype=torch.float32,
    )

    for idx, box in enumerate(boxes_xyxy):
        x1 = max(0, min(int(box[0].item()), width - 1))
        y1 = max(0, min(int(box[1].item()), height - 1))
        x2 = max(0, min(int(box[2].item()), width))
        y2 = max(0, min(int(box[3].item()), height))
        if x2 <= x1 or y2 <= y1:
            continue

        vals = depth_tensor[y1:y2, x1:x2].reshape(-1).float()
        vals = vals[torch.isfinite(vals)]
        if vals.numel() == 0:
            continue

        if vals.numel() > sample_k:
            sample_idx = torch.randint(0, vals.numel(), (sample_k,), device=vals.device)
            vals = vals[sample_idx]

        if vals.numel() == 1:
            out[idx] = vals[0]
            continue

        q1 = torch.quantile(vals, 0.25)
        q3 = torch.quantile(vals, 0.75)
        iqr = q3 - q1
        if iqr > 0:
            lower = q1 - iqr_k * iqr
            upper = q3 + iqr_k * iqr
            vals_iqr = vals[(vals >= lower) & (vals <= upper)]
            if vals_iqr.numel() > 0:
                vals = vals_iqr

        out[idx] = vals.median()

    return out.detach().cpu().numpy().astype(np.float32)


def _build_depth_keep_mask(depths: torch.Tensor, gate_threshold: float, filter_mode: str) -> torch.Tensor:
    if filter_mode == "off":
        return torch.ones_like(depths, dtype=torch.bool)

    finite = torch.isfinite(depths)
    if filter_mode == "ge":
        keep = depths >= gate_threshold
    else:
        keep = depths <= gate_threshold

    # Depth estimation may be missing on a subset of boxes; keep them instead of dropping blindly.
    return (~finite) | keep


def filter_rgb_candidates(
    result,
    depth_tensor: torch.Tensor,
    gate_threshold: float,
    filter_mode: str,
    nms_iou_close: float,
    nms_iou_far: float,
    nms_depth_delta: float,
):
    boxes = result.boxes.xyxy
    scores = result.boxes.conf
    classes = result.boxes.cls
    indices = torch.arange(boxes.shape[0], device=boxes.device, dtype=torch.long)
    if boxes.numel() == 0:
        device = depth_tensor.device
        return (
            torch.empty((0, 4), device=device),
            torch.empty((0,), device=device),
            torch.empty((0,), device=device),
            torch.empty((0,), device=device, dtype=torch.long),
        )

    device = boxes.device
    if result.masks is not None and result.masks.data.numel() > 0:
        mask_h, mask_w = result.masks.data.shape[-2:]
        depth_resized = F.interpolate(
            depth_tensor.unsqueeze(0).unsqueeze(0),
            size=(mask_h, mask_w),
            mode="bilinear",
            align_corners=False,
        ).squeeze(0).squeeze(0)
        depth_vals = extract_mask_depths_gpu_iqr(result, depth_resized, sample_k=4096, use_sampling=True)
    else:
        depth_vals = extract_box_depths_iqr(depth_tensor, boxes)

    depths = torch.full((boxes.shape[0],), float("nan"), device=device, dtype=torch.float32)
    if len(depth_vals) > 0:
        depth_vals_t = torch.from_numpy(depth_vals).to(device=device, dtype=torch.float32)
        fill_len = min(depths.numel(), depth_vals_t.numel())
        depths[:fill_len] = depth_vals_t[:fill_len]

    keep_mask = _build_depth_keep_mask(depths, gate_threshold, filter_mode)
    boxes = boxes[keep_mask]
    scores = scores[keep_mask]
    classes = classes[keep_mask]
    depths = depths[keep_mask]
    indices = indices[keep_mask]

    if boxes.numel() == 0:
        return (
            torch.empty((0, 4), device=device),
            torch.empty((0,), device=device),
            torch.empty((0,), device=device),
            torch.empty((0,), device=device, dtype=torch.long),
        )

    final_boxes = []
    final_scores = []
    final_depths = []
    final_indices = []
    for class_id in classes.unique():
        class_mask = classes == class_id
        keep_idx = dynamic_nms(
            boxes[class_mask],
            scores[class_mask],
            depths[class_mask],
            iou_close=nms_iou_close,
            iou_far=nms_iou_far,
            depth_delta=nms_depth_delta,
        )
        final_boxes.append(boxes[class_mask][keep_idx])
        final_scores.append(scores[class_mask][keep_idx])
        final_depths.append(depths[class_mask][keep_idx])
        final_indices.append(indices[class_mask][keep_idx])

    return (
        torch.cat(final_boxes, dim=0),
        torch.cat(final_scores, dim=0),
        torch.cat(final_depths, dim=0),
        torch.cat(final_indices, dim=0),
    )


def build_native_tracker_args(args) -> argparse.Namespace:
    return argparse.Namespace(
        track_high_thresh=args.bt_track_thresh,
        track_low_thresh=args.bt_low_thresh,
        new_track_thresh=args.bt_new_track_thresh,
        track_buffer=args.bt_track_buffer,
        match_thresh=args.bt_match_thresh,
        fuse_score=True,
        gmc_method=args.bt_gmc_method,
        proximity_thresh=args.bt_proximity_thresh,
        appearance_thresh=args.bt_appearance_thresh,
        with_reid=True,
        model=args.bt_reid_model,
    )


def build_tracker_boxes(boxes: torch.Tensor, scores: torch.Tensor, orig_shape: tuple[int, int]) -> Boxes:
    classes = torch.zeros((boxes.shape[0], 1), device=boxes.device, dtype=boxes.dtype)
    data = torch.cat([boxes, scores.unsqueeze(1), classes], dim=1)
    return Boxes(data.detach().cpu().numpy().astype(np.float32), orig_shape=orig_shape)


def update_track_depth_cache(
    track_depths: dict[int, float],
    track_id: int,
    track_score: float,
    track_det_idx: int,
    det_depths: np.ndarray,
    high_det_indices: np.ndarray,
    low_det_indices: np.ndarray,
    high_thresh: float,
    alpha: float = 0.8,
) -> float:
    det_idx = None
    if track_det_idx >= 0:
        if track_score >= high_thresh:
            if track_det_idx < len(high_det_indices):
                det_idx = int(high_det_indices[track_det_idx])
        elif track_det_idx < len(low_det_indices):
            det_idx = int(low_det_indices[track_det_idx])

    det_depth = np.nan
    if det_idx is not None and 0 <= det_idx < len(det_depths):
        det_depth = float(det_depths[det_idx])

    prev_depth = track_depths.get(track_id, np.nan)
    if np.isfinite(det_depth):
        if np.isfinite(prev_depth):
            prev_depth = alpha * prev_depth + (1.0 - alpha) * det_depth
        else:
            prev_depth = det_depth
        track_depths[track_id] = prev_depth

    return float(track_depths.get(track_id, 0.0)) if np.isfinite(track_depths.get(track_id, np.nan)) else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtsp_url", default=RTSP_URL)
    parser.add_argument("--input_size", type=int, default=384)
    parser.add_argument("--display_fps", type=int, default=15)
    parser.add_argument("--max_side", type=int, default=960)
    parser.add_argument("--output_dir", default=OUTPUT_DIR)
    parser.add_argument("--no_imshow", action="store_true")

    parser.add_argument("--rgb_weights", default=os.path.join(WEIGHTS_DIR, "yolo26m-seg.pt"))
    parser.add_argument("--rgb_conf", type=float, default=0.05)
    parser.add_argument("--rgb_iou", type=float, default=0.80)

    parser.add_argument("--bt_track_thresh", type=float, default=0.1)
    parser.add_argument("--bt_low_thresh", type=float, default=0.1)
    parser.add_argument("--bt_new_track_thresh", type=float, default=0.1)
    parser.add_argument("--bt_match_thresh", type=float, default=0.8)
    parser.add_argument("--bt_track_buffer", type=int, default=18)
    parser.add_argument("--bt_gmc_method", default="sparseOptFlow")
    parser.add_argument("--bt_proximity_thresh", type=float, default=0.5)
    parser.add_argument("--bt_appearance_thresh", type=float, default=0.8)
    parser.add_argument("--bt_reid_model", default="")
    parser.add_argument("--depth_gate", type=float, default=1.2)
    parser.add_argument("--depth_lambda", type=float, default=0.05)
    parser.add_argument("--w_app", type=float, default=0.45)
    parser.add_argument("--app_gate", type=float, default=0.75)

    parser.add_argument("--roi", default="800,1000,1200,1100")
    parser.add_argument("--default_depth_threshold", type=float, default=2.5)
    parser.add_argument("--roi_percentile", type=float, default=50.0)
    parser.add_argument("--depth_filter_mode", choices=["ge", "le", "off"], default="ge")
    parser.add_argument("--nms_iou_close", type=float, default=0.3)
    parser.add_argument("--nms_iou_far", type=float, default=0.6)
    parser.add_argument("--nms_depth_delta", type=float, default=0.7)
    args = parser.parse_args()
    if not args.bt_reid_model:
        args.bt_reid_model = args.rgb_weights

    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    depth_fp32 = device != "cuda"

    depth_model = VideoDepthAnything(
        encoder="vits",
        features=64,
        out_channels=[48, 96, 192, 384],
    ).to(device).eval()
    depth_model.load_state_dict(
        torch.load(os.path.join(WEIGHTS_DIR, "video_depth_anything_vits.pth"), map_location="cpu")
    )

    yolo_rgb = YOLO(args.rgb_weights)
    yolo_rgb.model.fuse = lambda *a, **k: yolo_rgb.model
    yolo_rgb.model.half().to(device)

    tracker = BOTSORT(
        build_native_tracker_args(args),
        frame_rate=args.display_fps,
    )

    os.makedirs(args.output_dir, exist_ok=True)

    in_event = False
    event_dir = ""
    event_file = None
    event_db_id = None
    event_detected_at = None
    event_started_at = None
    absence_started_at = None
    consecutive_frames = 0
    event_last_confirmed_track_end_at = None
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
    track_last_seen_at = {}
    track_event_end_cap = {}
    track_start_needs_backfill = {}
    track_image_sequence = {}
    track_depths = {}
    pending_removed_ids = set()

    def _parse_roi(roi_str):
        if not roi_str:
            return None
        parts = [part.strip() for part in roi_str.split(",") if part.strip()]
        if len(parts) != 4:
            return None
        try:
            x1, y1, x2, y2 = [int(float(part)) for part in parts]
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
    warm_h, warm_w = warm_bgr.shape[:2]
    if args.max_side > 0 and max(warm_h, warm_w) > args.max_side:
        warm_scale = args.max_side / max(warm_h, warm_w)
        warm_bgr = cv2.resize(
            warm_bgr,
            (int(warm_w * warm_scale), int(warm_h * warm_scale)),
            interpolation=cv2.INTER_LINEAR,
        )
    warm_rgb = cv2.cvtColor(warm_bgr, cv2.COLOR_BGR2RGB)

    with torch.no_grad():
        for _ in range(2):
            _ = depth_model.infer_video_depth_one(warm_rgb, args.input_size, device, fp32=depth_fp32)
        _ = yolo_rgb(
            warm_rgb,
            classes=[0],
            conf=args.rgb_conf,
            iou=args.rgb_iou,
            verbose=False,
        )[0]

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

            frame_start = time.time()
            depth = depth_model.infer_video_depth_one(frame_rgb, args.input_size, device, fp32=depth_fp32)
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

            result_rgb = yolo_rgb(
                frame_rgb,
                classes=[0],
                conf=args.rgb_conf,
                iou=args.rgb_iou,
                verbose=False,
            )[0]

            gate_threshold = (last_roi_depth if last_roi_depth is not None else args.default_depth_threshold) + 0.4
            boxes, scores, depths, _keep_indices = filter_rgb_candidates(
                result_rgb,
                depth_tensor,
                gate_threshold,
                args.depth_filter_mode,
                args.nms_iou_close,
                args.nms_iou_far,
                args.nms_depth_delta,
            )

            person_count = int(boxes.shape[0])
            if person_count > 0 and not in_event:
                in_event = True
                event_detected_at = frame_time
                event_started_at = None
                absence_started_at = None
                consecutive_frames = 0
                event_last_confirmed_track_end_at = None
                event_dir, event_file = prepare_event_dir(args.output_dir, event_detected_at)
                print(f"[events] started: {event_detected_at.strftime('%Y-%m-%d %H:%M:%S')}")
            elif person_count > 0 and in_event:
                consecutive_frames = 0
                absence_started_at = None
            elif person_count == 0 and in_event:
                if consecutive_frames == 0:
                    absence_started_at = frame_time
                consecutive_frames += 1
                if consecutive_frames >= MAX_CONSECUTIVE_NO_PERSON:
                    if event_db_id is not None:
                        event_finished_at = event_last_confirmed_track_end_at or event_started_at
                        if event_finished_at is not None and event_started_at is not None and event_finished_at < event_started_at:
                            event_finished_at = event_started_at
                        update_event_end_time(event_db_id, event_finished_at)
                        for tid, track_dir in list(track_event_dir.items()):
                            if track_dir == event_dir:
                                track_event_end_cap[tid] = event_finished_at
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
                    event_detected_at = None
                    event_started_at = None
                    absence_started_at = None
                    consecutive_frames = 0
                    event_last_confirmed_track_end_at = None

            tracker_boxes = build_tracker_boxes(boxes, scores, (infer_h, infer_w))
            det_depths = depths.detach().cpu().numpy().astype(np.float32)
            det_scores = scores.detach().cpu().numpy().astype(np.float32)
            high_det_indices = np.flatnonzero(det_scores >= tracker.args.track_high_thresh)
            low_det_indices = np.flatnonzero(
                (det_scores > tracker.args.track_low_thresh) & (det_scores < tracker.args.track_high_thresh)
            )

            tracks = tracker.update(
                tracker_boxes,
                infer_frame,
                None,
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
                    f"ROI p{args.roi_percentile:.0f}:{gate_threshold:.2f}|mode:{args.depth_filter_mode}",
                    (rx1, max(0, ry1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 165, 255),
                    1,
                )

            def process_track(track_row):
                nonlocal event_db_id, event_started_at, event_last_confirmed_track_end_at
                tid = int(track_row[4])
                x1, y1, x2, y2 = track_row[:4]
                x1i, y1i, x2i, y2i = int(x1), int(y1), int(x2), int(y2)
                center_x = (x1i + x2i) // 2
                center_y = (y1i + y2i) // 2
                score_val = float(track_row[5]) if len(track_row) > 5 else 0.0
                det_idx_local = int(track_row[7]) if len(track_row) > 7 else -1
                dval = update_track_depth_cache(
                    track_depths,
                    tid,
                    score_val,
                    det_idx_local,
                    det_depths,
                    high_det_indices,
                    low_det_indices,
                    tracker.args.track_high_thresh,
                )

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
                    if track_frame_count.get(tid, 0) >= MIN_TRACK_FRAMES:
                        if event_started_at is None or corrected_start_time < event_started_at:
                            event_started_at = corrected_start_time
                            if event_db_id is not None:
                                update_event_start_time(event_db_id, event_started_at)

                prev_x, prev_y = prev_positions[tid]
                delta_x = center_x - prev_x
                delta_y = center_y - prev_y
                prev_positions[tid] = (center_x, center_y)
                track_last_seen_at[tid] = frame_time
                track_frame_count[tid] = track_frame_count.get(tid, 0) + 1
                if track_frame_count[tid] >= MIN_TRACK_FRAMES:
                    if event_last_confirmed_track_end_at is None or frame_time > event_last_confirmed_track_end_at:
                        event_last_confirmed_track_end_at = frame_time
                if track_frame_count[tid] == MIN_TRACK_FRAMES:
                    confirmed_start_time = track_started_at.get(tid, frame_time)
                    if event_started_at is None or confirmed_start_time < event_started_at:
                        event_started_at = confirmed_start_time
                        if event_db_id is not None:
                            update_event_start_time(event_db_id, event_started_at)
                    if event_db_id is None and event_started_at is not None:
                        event_db_id = insert_event_to_db(event_started_at)
                        if event_db_id is not None:
                            for person_track_id in track_db_id.values():
                                update_person_track_event_id(person_track_id, event_db_id)

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
                    f"id{tid}|{score_val:.2f}|{dval:.2f}|dx{delta_x}|dy{delta_y}",
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
                    track_person_dir = person_dir.get(tid)
                    track_first_time = first_time.get(tid)

                    if frame_num == 0 and writer is None and track_person_dir is None and person_track_id is None:
                        pending_removed_ids.discard(tid)
                        continue

                    if frame_num < MIN_TRACK_FRAMES:
                        delete_person_track_from_db(person_track_id)
                        if track_person_dir:
                            short_track_dir = os.path.join(track_dir, "unclasses", track_person_dir)
                            shutil.rmtree(short_track_dir, ignore_errors=True)
                        print(f"[person_tracks] dropped short track: id={tid}, frames={frame_num}")
                    else:
                        removed_time = track_last_seen_at.get(tid, frame_time)
                        if tid in track_started_at and removed_time < track_started_at[tid]:
                            removed_time = track_started_at[tid]
                        event_end_cap = track_event_end_cap.get(tid)
                        if event_end_cap is not None and removed_time > event_end_cap:
                            removed_time = event_end_cap
                        update_person_track_end_time(person_track_id, removed_time)
                        task_data = {
                            "person_track_id": person_track_id,
                            "event_dir": track_dir,
                            "person_dir": track_person_dir,
                            "first_time": track_first_time,
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
                    track_last_seen_at.pop(tid, None)
                    track_event_end_cap.pop(tid, None)
                    track_start_needs_backfill.pop(tid, None)
                    track_depths.pop(tid, None)
                    current_ids.discard(tid)
                    pending_removed_ids.discard(tid)
                except Exception as exc:
                    print(
                        f"[person_tracks] cleanup failed: id={tid}, "
                        f"type={type(exc).__name__}, err={exc!r}"
                    )

            frame_cost = time.time() - frame_start
            fps = 1.0 / frame_cost if frame_cost > 0 else 0.0
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
                cv2.imshow("RGB Tracking", src)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
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
