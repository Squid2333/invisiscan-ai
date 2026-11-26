"""Runtime configuration for the extracted AdaFace service.

All paths are relative to this project by default.  Deployment-specific values
can be supplied through environment variables so the copy can be committed to
GitHub without leaking a workstation path, password, or private endpoint.
"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default


LOCATION_ID = int(os.getenv("ADAFACE_LOCATION_ID", "1"))
CAMERA_ID = int(os.getenv("ADAFACE_CAMERA_ID", "1"))

# Model/checkpoint paths.  These files are intentionally kept locally and are
# ignored by Git; users may also point to an external copy with the variables.
ADAFACE_MODEL_PATH = _path_from_env(
    "ADAFACE_MODEL_PATH", PROJECT_ROOT / "pretrained" / "adaface_ir50_ms1mv2.ckpt"
)
DSLFIQA_CONFIG_PATH = _path_from_env(
    "DSLFIQA_CONFIG_PATH", PROJECT_ROOT / "DSLFIQA" / "config.conf"
)
DSLFIQA_DE_PATH = _path_from_env(
    "DSLFIQA_DE_PATH", PROJECT_ROOT / "DSLFIQA" / "ckpt" / "custom" / "DE.pt"
)
DSLFIQA_IQA_PATH = _path_from_env(
    "DSLFIQA_IQA_PATH", PROJECT_ROOT / "DSLFIQA" / "ckpt" / "custom" / "IQA.pt"
)
LANDMARK_MODEL_PATH = _path_from_env(
    "ADAFACE_LANDMARK_MODEL_PATH",
    PROJECT_ROOT / "DSLFIQA" / "landmark_detection" / "ckpts" / "model.pt",
)

# Runtime output directories.  The original service created these relative to
# whichever directory happened to be current; keeping them under the project
# makes the copied service predictable and portable.
RECEIVED_PATH = _path_from_env("ADAFACE_RECEIVED_PATH", PROJECT_ROOT / "received_image")
LOADED_FACES_PATH = _path_from_env("ADAFACE_LOADED_FACES_PATH", PROJECT_ROOT / "loaded_faces")
NEW_FACES_PATH = _path_from_env("ADAFACE_NEW_FACES_PATH", PROJECT_ROOT / "new_faces")
INDOOR_OUTPUT_PATH = _path_from_env("ADAFACE_INDOOR_OUTPUT_PATH", PROJECT_ROOT / "indoor")

# External services.  An empty Fay URL disables the optional notification.
FAY_URL = os.getenv("ADAFACE_FAY_URL", "").rstrip("/")
REDIS_HOST = os.getenv("ADAFACE_REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("ADAFACE_REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("ADAFACE_REDIS_DB", "0"))
REDIS_QUEUE = os.getenv("ADAFACE_REDIS_QUEUE", "adaface_master")

# Database credentials deliberately have no private defaults.
DB_CONFIG = {
    "host": os.getenv("ADAFACE_DB_HOST", "localhost"),
    "user": os.getenv("ADAFACE_DB_USER", ""),
    "password": os.getenv("ADAFACE_DB_PASSWORD", ""),
    "database": os.getenv("ADAFACE_DB_NAME", "laboratory"),
}
