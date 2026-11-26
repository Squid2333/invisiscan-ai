# consumer.py
import json
import os
import shutil
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import redis
from ultralytics import YOLO
from face_alignment import mtcnn

from LSTM.DoubleLSTM import load_LSTMmodel, lstm_predict_varlen
from utils.track_utils import save_faces, save_segs


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_DIR = os.path.join(PROJECT_ROOT, "weights")


# -------------------------
# config
# -------------------------
@dataclass(frozen=True)
class AppConfig:
    # redis
    redis_host: str = os.getenv("REDIS_HOST", "localhost")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))
    redis_db: int = int(os.getenv("REDIS_DB", "0"))

    missing_tasks_queue: str = "missing_tasks"
    adaface_master_queue: str = "adaface_master"
    outdoor_master_queue: str = "outdoor_master"

    # weights / devices
    norm_path: str = os.getenv("RGBDSORT_LSTM_NORM", os.path.join(WEIGHTS_DIR, "lstm_norm.npz"))
    lstm_weights: str = os.getenv("RGBDSORT_LSTM_WEIGHTS", os.path.join(WEIGHTS_DIR, "lstm.pth"))
    lstm_labels: str = os.getenv("RGBDSORT_LSTM_LABELS", os.path.join(WEIGHTS_DIR, "lstm_labels.json"))
    yolo_seg_weights: str = os.getenv(
        "RGBDSORT_SEG_WEIGHTS", os.path.join(WEIGHTS_DIR, "yolo11m-seg.pt")
    )

    lstm_device: str = os.getenv("RGBDSORT_LSTM_DEVICE", "cuda")
    mtcnn_device: str = os.getenv("RGBDSORT_MTCNN_DEVICE", "cuda:0")
    mtcnn_crop_size: Tuple[int, int] = (112, 112)

    # logic params
    short_track_drop_len: int = 5
    lstm_confidence_threshold: float = 0.3
    seg_per: int = 1

    mysql_host: str = os.getenv("MYSQL_HOST", "127.0.0.1")
    mysql_port: int = int(os.getenv("MYSQL_PORT", "3306"))
    mysql_user: str = os.getenv("MYSQL_USER", "")
    mysql_password: str = os.getenv("MYSQL_PASSWORD", "")
    mysql_database: str = os.getenv("MYSQL_DATABASE", "laboratory")


CFG = AppConfig()


# -------------------------
# logging
# -------------------------
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("consumer")
    if logger.handlers:
        return logger  # avoid duplicate handlers if re-imported
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.propagate = False
    return logger


LOG = setup_logger()


# -------------------------
# init models
# -------------------------
def load_norm(path: str) -> Tuple[Optional[float], Optional[float]]:
    norm = np.load(path)
    d_p1 = float(norm["d_p1"])
    d_p99 = float(norm["d_p99"])
    if np.isnan(d_p1) or np.isnan(d_p99):
        return None, None
    return d_p1, d_p99


D_P1, D_P99 = load_norm(CFG.norm_path)

LSTM_MODEL = load_LSTMmodel(CFG.lstm_weights, device=CFG.lstm_device, labels_path=CFG.lstm_labels)
MTCNN_MODEL = mtcnn.MTCNN(device=CFG.mtcnn_device, crop_size=CFG.mtcnn_crop_size)
SEG_MODEL = YOLO(CFG.yolo_seg_weights)
LOG.info("LSTM classes: %s", getattr(LSTM_MODEL, "class_names", []))

REDIS = redis.Redis(host=CFG.redis_host, port=CFG.redis_port, db=CFG.redis_db)
MYSQL_BIN = shutil.which("mysql")


# -------------------------
# helpers
# -------------------------
def fmt_time(first_time: str) -> str:
    dt = datetime.strptime(first_time, "%Y%m%d_%H%M%S.%f")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def body_ok(person_path: str) -> bool:
    body_dir = os.path.join(person_path, "body")
    if not os.path.isdir(body_dir):
        return False
    return len(os.listdir(body_dir)) > 0


def read_lines(txt_path: str) -> Optional[List[str]]:
    try:
        with open(txt_path, "r") as f:
            return f.readlines()
    except Exception:
        LOG.exception("[read fail] %s", txt_path)
        return None


def predict_event(lines: List[str]) -> Tuple[str, float, Dict[str, float]]:
    return lstm_predict_varlen(
        LSTM_MODEL,
        lines,
        confidence_threshold=CFG.lstm_confidence_threshold,
        d_p1=D_P1,
        d_p99=D_P99,
        xy_is_center=False,
        return_details=True,
    )


def sql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def body_dir_path(person_path: str) -> Optional[str]:
    body_dir = os.path.join(person_path, "body")
    if not os.path.isdir(body_dir):
        return None
    return os.path.abspath(body_dir)


def update_person_track_result(person_track_id: Optional[int], category: str, image_path: Optional[str]) -> None:
    if person_track_id is None or MYSQL_BIN is None or not CFG.mysql_user:
        return

    image_sql = "NULL" if image_path is None else f"'{sql_escape(image_path)}'"
    sql = (
        "UPDATE `person_tracks` "
        f"SET `category` = '{sql_escape(category)}', `image_path` = {image_sql} "
        f"WHERE `id` = {int(person_track_id)};"
    )
    env = os.environ.copy()
    env["MYSQL_PWD"] = CFG.mysql_password

    try:
        subprocess.run(
            [
                MYSQL_BIN,
                "-h", CFG.mysql_host,
                "-P", str(CFG.mysql_port),
                "-u", CFG.mysql_user,
                "-D", CFG.mysql_database,
                "-e", sql,
            ],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        LOG.exception("update person_tracks failed: id=%s", person_track_id)


@dataclass(frozen=True)
class MissingTask:
    event_dir: str
    person_dir: str
    first_time: str
    person_track_id: Optional[int] = None

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MissingTask":
        # 缺字段则抛异常，让外层记录；保持“硬失败”行为
        return MissingTask(
            event_dir=d["event_dir"],
            person_dir=d["person_dir"],
            first_time=d["first_time"],
            person_track_id=d.get("person_track_id"),
        )

    @property
    def target_time(self) -> str:
        return fmt_time(self.first_time)

    @property
    def src_dir(self) -> str:
        return os.path.join(self.event_dir, "unclasses", self.person_dir)

    @property
    def txt_path(self) -> str:
        return os.path.join(self.src_dir, f"{self.person_dir}.txt")


# -------------------------
# core
# -------------------------
def process_missing_track_from_queue(task_data: Dict[str, Any]) -> None:
    task = MissingTask.from_dict(task_data)

    # 1) read track file
    lines = read_lines(task.txt_path)
    if lines is None:
        return

    # 2) short track strategy: drop (keep original behavior)
    if len(lines) < CFG.short_track_drop_len:
        LOG.info("track too short (%d), drop %s", len(lines), task.person_dir)
        LOG.info("=================================================")
        try:
            shutil.rmtree(task.src_dir, ignore_errors=True)
        except Exception:
            LOG.exception("failed to rmtree(%s)", task.src_dir)
        return

    # 3) LSTM predict
    try:
        prediction, confidence, class_probs = predict_event(lines)
    except Exception:
        LOG.exception("LSTM predict failed for %s", task.person_dir)
        return

    LOG.info(
        "%s: 检测到 %s 事件, confidence=%.4f, indoor=%.4f, outdoor=%.4f",
        task.target_time,
        prediction,
        confidence,
        class_probs.get("indoor", 0.0),
        class_probs.get("outdoor", 0.0),
    )

    # 4) move
    dst_root = os.path.join(task.event_dir, prediction)
    try:
        os.makedirs(dst_root, exist_ok=True)
        shutil.move(task.src_dir, dst_root)
    except Exception:
        LOG.exception("move failed: %s -> %s", task.src_dir, dst_root)
        return

    # 5) cleanup unclasses
    unclasses_dir = os.path.join(task.event_dir, "unclasses")
    try:
        if os.path.isdir(unclasses_dir) and not os.listdir(unclasses_dir):
            os.rmdir(unclasses_dir)
    except Exception:
        # 清理失败不影响主流程
        LOG.exception("cleanup unclasses failed: %s", unclasses_dir)

    # 6) compute moved path
    person_path = os.path.join(dst_root, task.person_dir)
    update_person_track_result(task.person_track_id, prediction, body_dir_path(person_path))

    # 7) body check (keep your rule)
    if not body_ok(person_path):
        LOG.warning("检测异常")
        return

    # 8) segmentation
    try:
        save_segs(person_path, SEG_MODEL, per=CFG.seg_per)
    except Exception:
        LOG.exception("save_segs failed: %s", person_path)
        return

    # 9) indoor -> face + enqueue
    if prediction == "indoor":
        try:
            save_faces(person_path, MTCNN_MODEL)
        except Exception:
            LOG.exception("save_faces failed: %s", person_path)
            return

        person_data = {
            "person_dir": os.path.abspath(person_path),
            "first_time": task.target_time,
        }
        try:
            REDIS.rpush(
                CFG.adaface_master_queue,
                json.dumps(person_data, ensure_ascii=False),
            )
        except Exception:
            LOG.exception("redis rpush failed: queue=%s", CFG.adaface_master_queue)
            return

    if prediction == "outdoor":
        person_data = {
            "person_dir": os.path.abspath(person_path),
            "first_time": task.target_time,
        }
        try:
            REDIS.rpush(
                CFG.outdoor_master_queue,
                json.dumps(person_data, ensure_ascii=False),
            )
        except Exception:
            LOG.exception("redis rpush failed: queue=%s", CFG.outdoor_master_queue)
            return

    LOG.info("%s done.", person_path)
    LOG.info("=================================================")


def main() -> None:
    LOG.info("消费者启动，开始监听任务队列...")
    while True:
        try:
            _, task_json = REDIS.blpop(CFG.missing_tasks_queue)
            task_data = json.loads(task_json)
        except Exception:
            LOG.exception("failed to blpop/json loads from queue=%s", CFG.missing_tasks_queue)
            continue

        try:
            process_missing_track_from_queue(task_data)
        except Exception:
            # 兜底：保证消费者不断
            LOG.exception("unexpected error while processing task: %s", task_data)


if __name__ == "__main__":
    main()
