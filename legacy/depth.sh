#!/usr/bin/env bash

# Portable launcher for the two project components that were referenced by
# the original server-side depth.sh.  The original script is outside this
# repository and is intentionally left unchanged.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RGB_DIR="$SCRIPT_DIR/RGBDSort"
ADA_DIR="$SCRIPT_DIR/AdaFace-master"

# Load an optional local environment file without adding it to Git.
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +a
fi

TERMINAL_BIN="${TERMINAL_BIN:-gnome-terminal}"
RGB_PYTHON_BIN="${RGB_PYTHON_BIN:-python}"
ADAFACE_PYTHON_BIN="${ADAFACE_PYTHON_BIN:-python}"

if ! command -v "$TERMINAL_BIN" >/dev/null 2>&1; then
    echo "错误：未找到 $TERMINAL_BIN。请在 Linux 图形终端环境运行，或分别启动各 Python 入口。" >&2
    exit 1
fi

launch_python() {
    local label="$1"
    local workdir="$2"
    local python_bin="$3"
    local script="$4"

    echo "启动 $label"
    "$TERMINAL_BIN" -- bash -lc 'cd "$1" && exec "$2" "$3"' \
        -- "$workdir" "$python_bin" "$script"
}

# MediaMTX is an external service.  Set MEDIAMTX_DIR if it should be started
# by this launcher; otherwise start it separately with your service manager.
if [[ -n "${MEDIAMTX_DIR:-}" ]]; then
    MEDIAMTX_BIN="${MEDIAMTX_BIN:-mediamtx}"
    "$TERMINAL_BIN" -- bash -lc 'cd "$1" && exec "$2"' \
        -- "$MEDIAMTX_DIR" "$MEDIAMTX_BIN"
else
    echo "提示：未设置 MEDIAMTX_DIR，跳过 MediaMTX 启动。"
fi

launch_python "RGBDSort RTSP" "$RGB_DIR" "$RGB_PYTHON_BIN" "rtsp_server.py"
launch_python "RGBDSort 检测跟踪" "$RGB_DIR" "$RGB_PYTHON_BIN" "RGBsort.py"
launch_python "RGBDSort 轨迹消费者" "$RGB_DIR" "$RGB_PYTHON_BIN" "track_comsumer.py"
launch_python "AdaFace Redis 消费者" "$ADA_DIR" "$ADAFACE_PYTHON_BIN" "redis_comsumer.py"

echo "两个项目的核心进程已提交到独立终端。PersonViT 未包含在本次提取范围内，请按部署需要单独启动。"
