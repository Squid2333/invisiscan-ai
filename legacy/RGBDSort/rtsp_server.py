import os
import cv2
import time
import shlex
import queue
import threading
import subprocess
import shutil
import glob
import tempfile
from datetime import datetime, timedelta

# ===================== 配置 =====================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

MTX_HOST = os.getenv("MEDIAMTX_HOST", "127.0.0.1")     # MediaMTX主机IP
PORT = int(os.getenv("MEDIAMTX_PORT", "8554"))
PATH = os.getenv("MEDIAMTX_PATH", "live")
PUBLISH_URL = f"rtsp://{MTX_HOST}:{PORT}/{PATH}"

_video_url = os.getenv("RGBDSORT_VIDEO_URL", "0")
try:
    VIDEO_URL = int(_video_url)
except ValueError:
    VIDEO_URL = _video_url  # 摄像头 / 或视频文件路径
FRAME_WIDTH = 2560  # 原始宽度
FRAME_HEIGHT = 1440  # 原始高度
FPS = int(os.getenv("RGBDSORT_FPS", "8"))
VIDEO_DURATION_SECONDS = int(os.getenv("RGBDSORT_VIDEO_DURATION", "3600"))

ROTATE_CCW_90 = os.getenv("RGBDSORT_ROTATE_CCW_90", "1").lower() not in {"0", "false", "no"}
FRAME_DIR = os.getenv("RGBDSORT_FRAME_DIR", os.path.join(PROJECT_ROOT, "runtime", "frames"))
VIDEO_DIR = os.getenv("RGBDSORT_VIDEO_DIR", os.path.join(PROJECT_ROOT, "runtime", "videos"))
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "laboratory")
MYSQL_CAMERA_ID = int(os.getenv("MYSQL_CAMERA_ID", "1"))
MYSQL_LOCATION_ID = int(os.getenv("MYSQL_LOCATION_ID", "1"))
MYSQL_BIN = shutil.which("mysql")

QUEUE_SIZE = 5
frame_queue = queue.Queue(maxsize=QUEUE_SIZE)

stop_event = threading.Event()
# =================================================


def draw_time_cv2(frame, text):
    # frame: BGR numpy array
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 2.0
    thickness = 2
    text_color = (255, 255, 255)  # white in BGR
    bg_color = (0, 0, 0)
    x, y = 10, 10
    pad_x = 12
    pad_y = 10
    (text_w, text_h), _ = cv2.getTextSize(text, font, font_scale, thickness)
    y = y + text_h
    cv2.rectangle(
        frame,
        (max(0, x - pad_x), max(0, y - text_h - pad_y)),
        (x + text_w + pad_x, y + pad_y),
        bg_color,
        -1,
    )
    cv2.putText(frame, text, (x, y), font, font_scale, text_color, thickness, cv2.LINE_AA)
    return frame


def save_frame(frame, frame_dir, text=None, filename=None):
    if text is None:
        video_time = datetime.now()
        text = video_time.strftime("%Y-%m-%d %H:%M:%S")

    if filename is None:
        filename = text.replace(" ", "_").replace(":", "-")

    frame_with_text = draw_time_cv2(frame.copy(), text)
    out_path = os.path.join(frame_dir, f"{filename}.jpg")
    cv2.imwrite(out_path, frame_with_text)
    return frame_with_text


def hour_bucket_start(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def hour_bucket_name(dt: datetime) -> str:
    return hour_bucket_start(dt).strftime("%Y-%m-%d_%H")


def hour_bucket_dir(frame_root_dir: str, dt: datetime) -> str:
    return os.path.join(frame_root_dir, hour_bucket_name(dt))


def list_completed_hour_dirs(frame_root_dir: str):
    current_bucket = hour_bucket_start(datetime.now())
    completed_dirs = []

    for entry in os.scandir(frame_root_dir):
        if not entry.is_dir():
            continue

        try:
            bucket_start = datetime.strptime(entry.name, "%Y-%m-%d_%H")
        except ValueError:
            continue

        if bucket_start < current_bucket:
            completed_dirs.append((bucket_start, entry.path))

    completed_dirs.sort(key=lambda item: item[0])
    return completed_dirs


def parse_frame_timestamp(filename: str) -> datetime:
    name, _ = os.path.splitext(filename)
    return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S_%f")


def ffconcat_escape(path: str) -> str:
    return path.replace("'", "'\\''")


def build_ffconcat_file(frame_dir: str, images):
    frame_times = [parse_frame_timestamp(img) for img in images]
    bucket_start = hour_bucket_start(frame_times[0])
    bucket_end = bucket_start + timedelta(hours=1)

    manifest = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".ffconcat",
        prefix="hourly_",
        delete=False,
        encoding="utf-8",
    )

    try:
        manifest.write("ffconcat version 1.0\n")

        for idx, img in enumerate(images):
            img_path = os.path.abspath(os.path.join(frame_dir, img))
            frame_start = bucket_start if idx == 0 else frame_times[idx]
            frame_end = frame_times[idx + 1] if idx + 1 < len(images) else bucket_end
            duration = max((frame_end - frame_start).total_seconds(), 0.001)
            manifest.write(f"file '{ffconcat_escape(img_path)}'\n")
            manifest.write(f"duration {duration:.6f}\n")

        last_img_path = os.path.abspath(os.path.join(frame_dir, images[-1]))
        manifest.write(f"file '{ffconcat_escape(last_img_path)}'\n")
    finally:
        manifest.close()

    return manifest.name


def sql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def save_video_record(video_path: str, start_time: datetime, end_time: datetime):
    if MYSQL_BIN is None or not MYSQL_USER:
        print("[videos] mysql integration disabled, skip insert")
        return

    file_name = os.path.basename(video_path)
    file_path = os.path.abspath(video_path)
    file_size = os.path.getsize(video_path)
    sql = (
        "INSERT INTO `videos` "
        "(`file_name`, `file_path`, `duration`, `size`, `start_time`, `end_time`, `location_id`, `camera_id`) "
        f"VALUES ('{sql_escape(file_name)}', '{sql_escape(file_path)}', {VIDEO_DURATION_SECONDS}, {file_size}, "
        f"'{start_time.strftime('%Y-%m-%d %H:%M:%S')}', '{end_time.strftime('%Y-%m-%d %H:%M:%S')}', "
        f"{MYSQL_LOCATION_ID}, {MYSQL_CAMERA_ID});"
    )
    env = os.environ.copy()
    env["MYSQL_PWD"] = MYSQL_PASSWORD

    try:
        subprocess.run(
            [
                MYSQL_BIN,
                "-h", MYSQL_HOST,
                "-P", str(MYSQL_PORT),
                "-u", MYSQL_USER,
                "-D", MYSQL_DATABASE,
                "-e", sql,
            ],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        print(
            f"[videos] saved: {file_name}, "
            f"{start_time.strftime('%Y-%m-%d %H:%M:%S')} -> {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    except subprocess.CalledProcessError as exc:
        error_text = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        print(f"[videos] insert failed: {error_text}")


def make_video(frame_dir, video_dir, output_name=None, delete_pics=True):
    image_size = '720x1280'  # 归档视频输出分辨率（旋转后的 9:16）
    video_bitrate = "1M"
    max_bitrate = "3M"

    images = sorted(img for img in os.listdir(frame_dir) if img.endswith(".jpg"))
    if not images:
        raise ValueError(f"{frame_dir} 下暂无照片")

    if output_name is None:
        output_name = os.path.splitext(images[-1])[0]
    output_video = os.path.join(video_dir, f"{output_name}.mp4")
    concat_file = build_ffconcat_file(frame_dir, images)

    try:
        command = [
            'ffmpeg',
            '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_file,
            '-t', str(VIDEO_DURATION_SECONDS),
            '-vf', f'scale={image_size}:flags=lanczos,fps={FPS}',
            '-c:v', 'h264_nvenc',
            '-g', str(FPS),
            '-keyint_min', str(FPS),
            '-sc_threshold', '0',
            '-bf', '0',
            '-forced-idr', '1',
            '-force_key_frames', 'expr:gte(t,n_forced*1)',
            '-rc-lookahead', '0',
            '-pix_fmt', 'yuv420p',
            '-b:v', video_bitrate,
            '-maxrate', max_bitrate,
            output_video,
        ]


        subprocess.run(command, check=True)
    finally:
        if os.path.exists(concat_file):
            os.remove(concat_file)

    if delete_pics:
        for img in images:
            img_path = os.path.join(frame_dir, img)
            os.remove(img_path)
        try:
            os.rmdir(frame_dir)
        except OSError:
            pass
        print(f'Deleted old images.')

    return output_video


def delete_old_videos(video_dir, max_videos=30 * 24):
    video_files = glob.glob(os.path.join(video_dir, '*.mp4'))
    if len(video_files) > max_videos:
        video_files.sort(key=os.path.getctime)
        for video_file in video_files[:-max_videos]:
            os.remove(video_file)
            print(f'Deleted old video: {video_file}')


def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)


def save_video(video_dir: str, frame_root_dir: str):
    ensure_dir(frame_root_dir)

    while not stop_event.is_set():
        completed_dirs = list_completed_hour_dirs(frame_root_dir)
        if not completed_dirs:
            stop_event.wait(5)
            continue

        for bucket_start, bucket_dir in completed_dirs:
            if stop_event.is_set():
                break

            output_name = bucket_start.strftime("%Y-%m-%d_%H-00-00")
            try:
                output_video = make_video(bucket_dir, video_dir, output_name=output_name)
                save_video_record(
                    output_video,
                    bucket_start,
                    bucket_start + timedelta(seconds=VIDEO_DURATION_SECONDS),
                )
                delete_old_videos(video_dir)
                print(f"已归档小时切片: {output_name}")
            except Exception as e:
                print(f"{bucket_dir} 合成失败: {e}")


def frame_capture_thread(video_url, frame_dir: str):
    ensure_dir(frame_dir)

    def _open_cap():
        c = cv2.VideoCapture(video_url)
        c.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        c.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        c.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        c.set(cv2.CAP_PROP_FPS, FPS)
        return c

    cap = _open_cap()

    while not stop_event.is_set():
        if not cap.isOpened():
            print("正在连接摄像头/视频源...")
            time.sleep(3)
            cap.release()
            cap = _open_cap()
            continue

        start_time = datetime.now()
        ret, frame = cap.read()
        if not ret or frame is None:
            print("读帧失败，尝试重连...")
            time.sleep(0.5)
            cap.release()
            cap = _open_cap()
            continue
        capture_time = datetime.now()
        timestamp = capture_time.strftime("%Y-%m-%d %H:%M:%S")
        filename_ts = capture_time.strftime("%Y-%m-%d_%H-%M-%S_%f")
        current_frame_dir = hour_bucket_dir(frame_dir, capture_time)
        ensure_dir(current_frame_dir)

        if ROTATE_CCW_90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

        try:
            save_frame(frame, current_frame_dir, timestamp, filename_ts)
        except Exception as e:
            print("save_frame failed:", e)

        try:
            if frame_queue.full():
                try:
                    frame_queue.get_nowait()
                except queue.Empty:
                    pass
            frame_queue.put_nowait((frame, timestamp))
        except queue.Full:
            pass

        frame_seconds = (datetime.now() - start_time).total_seconds()
        wait_time = max(0.0, 1.0 / FPS - frame_seconds)

        end = time.time() + wait_time
        while time.time() < end and not stop_event.is_set():
            time_to_sleep = max(0.0, end - time.time())
            time.sleep(min(0.01, time_to_sleep))  # 保证时间为非负数

    cap.release()


def start_ffmpeg_publisher():
    cmd = f"""
    ffmpeg -hide_banner -loglevel warning
    -re
    -f rawvideo -pix_fmt bgr24 -s {FRAME_HEIGHT}x{FRAME_WIDTH} -r {FPS} -i -
    -c:v libx264 -preset ultrafast -tune zerolatency
    -g {FPS} -keyint_min {FPS}
    -sc_threshold 0
    -bf 0
    -pix_fmt yuv420p
    -f rtsp -rtsp_transport tcp
    {PUBLISH_URL}
    """
    return subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE)


def publisher_loop():
    process = start_ffmpeg_publisher()
    print("正在发布到 MediaMTX：", PUBLISH_URL)
    print("客户端拉流：ffplay -rtsp_transport tcp", PUBLISH_URL)

    last_frame = None
    last_timestamp = None

    try:
        while not stop_event.is_set():
            try:
                frame, timestamp = frame_queue.get(timeout=1.0)
                while True:
                    try:
                        frame, timestamp = frame_queue.get_nowait()
                    except queue.Empty:
                        break
                last_frame = frame
                last_timestamp = timestamp
            except queue.Empty:
                frame = last_frame
                timestamp = last_timestamp

            if frame is None or timestamp is None:
                continue

            if frame.shape[1] != FRAME_HEIGHT or frame.shape[0] != FRAME_WIDTH:
                frame = cv2.resize(frame, (FRAME_HEIGHT, FRAME_WIDTH), interpolation=cv2.INTER_LINEAR)

            try:
                frame_to_send = draw_time_cv2(frame.copy(), timestamp)
                process.stdin.write(frame_to_send.tobytes())
            except BrokenPipeError:
                print("BrokenPipe：ffmpeg 已退出。请确认 MediaMTX 正在运行且监听 8554。")
                break

    finally:
        try:
            if process.stdin:
                process.stdin.close()
        except:
            pass
        try:
            process.terminate()
        except:
            pass


def main():
    ensure_dir(FRAME_DIR)
    ensure_dir(VIDEO_DIR)

    t_cap = threading.Thread(
        target=frame_capture_thread,
        args=(VIDEO_URL, FRAME_DIR),
        daemon=True
    )

    t_save = threading.Thread(
        target=save_video,
        args=(VIDEO_DIR, FRAME_DIR),
        daemon=True
    )

    t_cap.start()
    t_save.start()

    try:
        publisher_loop()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
