import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from face_alignment import align
import glob
import subprocess
import os
import time
from datetime import datetime, timedelta


def delete_old_videos(video_dir, max_videos=30*24):
    """
    删除video_dir下最旧的视频文件，直到文件数量不超过max_videos。
    """
    # 获取video_dir下所有视频文件的路径
    video_files = glob.glob(os.path.join(video_dir, '*.mp4'))
    # 如果视频文件数量超过了最大限制
    if len(video_files) > max_videos:
        # 按照文件的创建时间排序
        video_files.sort(key=os.path.getctime)
        # 删除最旧的视频文件
        for video_file in video_files[:-max_videos]:
            os.remove(video_file)
            print(f'Deleted old video: {video_file}')

def make_video(frame_dir, video_dir, delete_pics=True):

    # 图片的分辨率，需要与你的图片一致
    image_size = '1440x2560'
    # 帧率
    framerate = '30'
    # 视频码率（例如：4M 表示 4Mbps）
    video_bitrate = "1M"
    # 最大码率（用于突发场景）
    max_bitrate = "3M"

    # 获取图片文件列表
    images = [img for img in os.listdir(frame_dir) if img.endswith(".jpg")]

    last_image = images[-1] if images else None

    output_video = video_dir + '/' + last_image.split('.')[0] + '.mp4'

    # 对图片文件进行排序，确保按顺序合成视频
    images.sort()

    # 构建FFmpeg命令
    command = [
        'ffmpeg',
        '-y',  # 覆盖输出文件
        '-hwaccel', 'cuda',  # 使用 CUDA 硬件加速
        '-f', 'image2',
        '-pattern_type', 'glob',
        '-i', f'{frame_dir}/*.jpg',
        '-vf', f'fps={framerate},scale={image_size}:flags=lanczos',  # 转换图片为视频流
        '-c:v', 'hevc_nvenc',  # 使用 HEVC NVENC 编码器
        '-b:v', video_bitrate,  # 设置视频码率
        '-maxrate', max_bitrate,  # 设置最大码率（用于 VBR）
        '-pix_fmt', 'yuv420p',  # 设置像素格式
        output_video
    ]

    # 执行FFmpeg命令
    subprocess.run(command)

    # 删除用于合成视频的图片
    if delete_pics:
        for img in images:
            img_path = os.path.join(frame_dir, img)
            os.remove(img_path)
        print(f'Deleted old image.')


def save_video(video_dir, frame_dir):
    wait_time = 0
    while True:
        # 等待到下一个整点
        time.sleep(wait_time)
        # 更新下一次等待时间为1小时
        wait_time = 3600

        # 尝试保存视频
        try:
            make_video(frame_dir, video_dir)

            delete_old_videos(video_dir)
            # 获取当前时间
            current_time = datetime.now()
            # 计算下一个整点时间
            next_hour = (current_time + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            # 计算需要等待的时间
            wait_time = (next_hour - current_time).total_seconds()
            print("等待:" + str(wait_time))

        except Exception as e:
            print(f'{frame_dir}下暂无照片')

def save_image(image, image_name, person_dir, output_dir):
    image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    body_path = os.path.join(output_dir, person_dir, 'body', image_name)
    os.makedirs(os.path.join(output_dir, person_dir, 'body'), exist_ok=True)
    try:
        image.save(body_path)
    except Exception as e:
        print(e)

def save_segs(person_dir, seg_model, per=1):
    body_path = os.path.join(person_dir, "body")
    seg_dir = os.path.join(person_dir, "segs")
    os.makedirs(seg_dir, exist_ok=True)

    file_count = 0
    for img_name in os.listdir(body_path):
        file_count += 1
        if file_count % per != 0:
            continue

        try:
            img_path = os.path.join(body_path, img_name)
            image = cv2.imread(img_path)
            if image is None:
                continue

            results = seg_model(image, classes=[0], conf=0.8, verbose=False)

            masks_with_area = []
            for r in results:
                if r.masks is None or r.masks.xy is None:
                    continue
                for mask_xy in r.masks.xy:
                    if mask_xy is None or len(mask_xy) < 3:
                        continue
                    mask = np.array(mask_xy, dtype=np.int32)
                    area = cv2.contourArea(mask)
                    if area > 0:
                        masks_with_area.append((mask, area))

            if not masks_with_area:
                continue

            masks_with_area.sort(key=lambda x: x[1], reverse=True)
            largest_mask, _ = masks_with_area[0]

            mask = np.zeros(image.shape[:2], dtype=np.uint8)
            cv2.fillPoly(mask, [largest_mask], 255)
            person_only = cv2.bitwise_and(image, image, mask=mask)

            cv2.imwrite(os.path.join(seg_dir, img_name), person_only)

        except Exception as e:
            # 🔥 关键：只跳过这一帧，不影响后续
            print(f"[seg fail] {person_dir}/{img_name}: {e}")
            continue

def save_faces(person_dir, mtcnn_model):
    body_path = os.path.join(person_dir, 'segs')
    for img_name in os.listdir(body_path):
        img_path = os.path.join(body_path, img_name)
        image = Image.open(img_path)
        face_results = align.get_aligned_face(mtcnn_model, '', image)
        if face_results is not None:
            os.makedirs(person_dir + '/face', exist_ok=True)
            face_results.save(person_dir + '/face/' + img_name)

def rename_num(rename_file_path):
    if os.path.exists(rename_file_path):
        with open(rename_file_path, 'r') as file:
            rename_number = file.read().strip()
        try:
            number = int(rename_number)
            new_number = number + 1
        except ValueError:
            print("rename.txt 文件中不包含有效的数字。")
            exit()
        with open(rename_file_path, 'w') as file:
            file.write(str(new_number))
        return str(int(rename_number) + 1)

def save_frame(image, frame_dir):
    image = Image.fromarray(image)
    image = image.transpose(Image.ROTATE_90)

    video_time = datetime.now()
    timestamp = video_time.strftime("%Y-%m-%d %H:%M:%S")

    image = draw_time(image, timestamp)

    image.save(frame_dir + '/' + timestamp + '.jpg')

    return np.array(image)

def draw_time(image, text):

    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(50)
    text_position = (10, 10)
    text_color = (255, 0, 0)
    draw.text(text_position, text, font=font, fill=text_color)

    return image


def delete_directory_contents(path):
    # 遍历文件夹中的所有文件和子文件夹
    for filename in os.listdir(path):
        file_path = os.path.join(path, filename)
        try:
            # 如果是文件夹，递归删除
            if os.path.isdir(file_path):
                delete_directory_contents(file_path)
                os.rmdir(file_path)
            # 如果是文件，直接删除
            else:
                os.unlink(file_path)
        except Exception as e:
            print(f'Failed to delete {file_path}. Reason: {e}')

def delete_directory(path):
    delete_directory_contents(path)
    try:
        os.rmdir(path)
    except Exception as e:
        delete_directory_contents(path)
        os.rmdir(path)
