import argparse
import io
import math
import os
import shutil
from datetime import datetime, time
from io import BytesIO

import mysql
import requests
import torch
import uuid
from PIL import Image
from tqdm import tqdm
from werkzeug.datastructures import FileStorage

from landmark_detect import landmark_detect
from DSLFIQA.util import dsl_fiqa, load_fiqa
from face_alignment import align
from inference import to_input, load_pretrained_model
from mysql.connector import Error
from DSLFIQA.landmark_detection.network import FaceXFormer
from runtime_config import (
    CAMERA_ID,
    DB_CONFIG,
    LANDMARK_MODEL_PATH,
    LOADED_FACES_PATH,
    LOCATION_ID,
    NEW_FACES_PATH,
)

_LANDMARK_MODEL = None
_LANDMARK_ARGS = None
_PICTURES_VERSION_SUPPORTS_UPDATED_AT = None

# Credentials come from environment variables via runtime_config.  Keeping the
# mapping in one place avoids committing the server account used by the source
# project.
db_config = DB_CONFIG


def _normalize_occurred_at(current_time):
    if isinstance(current_time, datetime):
        return current_time.replace(microsecond=0)
    if isinstance(current_time, str):
        value = current_time.strip()
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    raise ValueError(f'不支持的时间格式: {current_time}')


def update_list(image_path, identities, faces, model):
    name = os.path.splitext(os.path.basename(image_path))[0]
    identities.append({'user_id': None, 'name': name, 'picture_id': None})
    faces = face_embedding(image_path, model, faces)
    return identities, faces

def find_best_face(folder_path, images):
    for image in images:
        image_path = folder_path + image
        try:
            aligned_rgb_img = align.get_aligned_face(image_path)
            if aligned_rgb_img is None:
                print(image_path + ' 无人脸，尝试下一张')
                continue

            return image_path
        except Exception as e:
            print(image + ' 无人脸')
            print(e)

    return  None

def add_user(folder_path, images):
    image_path = find_best_face(folder_path, images)

    if image_path is None:
        print("add_user: 没找到可用人脸图，跳过入库")
        return None

    score = image_path.split('/')[-1].split('.')[0] + '.' + image_path.split('/')[-1].split('.')[1]
    print(score)
    if float(score) < 0.15:
        return None
    unique_id = uuid.uuid4()
    name = '陌生人' + str(unique_id)
    NEW_FACES_PATH.mkdir(parents=True, exist_ok=True)
    new_path = str(NEW_FACES_PATH / (name + '.jpg'))
    shutil.copy2(image_path, new_path)

    if image_path.endswith('.jpg'):
        connection = None
        cursor = None
        try:
            # 连接到MySQL数据库
            connection = mysql.connector.connect(**db_config)

            with io.open(image_path, 'rb') as image_file:
                binary_data = image_file.read()

            if connection.is_connected():
                # 创建游标对象
                cursor = connection.cursor()

                # SQL 插入语句
                insert_query = """
                INSERT INTO pictures (user_id, name, face_image, score)
                VALUES (%s, %s, %s, %s)
                """

                # 执行SQL语句
                cursor.execute(insert_query, (None, name, binary_data, score))

                # 提交到数据库执行
                connection.commit()

                print('陌生人像' + str(unique_id).split('-')[0] + '保存成功!')

        except Error as e:
            print(f"Error: {e}")

        finally:
            if connection and connection.is_connected():
                # 关闭游标和连接
                if cursor:
                    cursor.close()
                connection.close()

    return new_path

def add_log(current_time, username, score, action, path=None, user_id=None, picture_id=None):
    if score is None or (isinstance(score, float) and math.isnan(score)):  # 检查是否为 NaN
        score = 0  # 如果是 NaN 或 None，设置默认值为 0

    try:
        occurred_at = _normalize_occurred_at(current_time)
    except ValueError:
        occurred_at = datetime.now().replace(microsecond=0)
        print(f"Warning: 无法解析时间 {current_time}，已使用当前时间 {occurred_at}")

    if occurred_at.time() <= time(8, 40, 0):
        note = '正常'
    else:
        note = '迟到'

    new_log_id = None
    connection = None
    cursor = None

    try:
        # 连接到MySQL数据库
        connection = mysql.connector.connect(**db_config)

        if connection.is_connected():
            # 创建游标对象
            cursor = connection.cursor()

            # SQL 插入语句
            insert_query = """
            INSERT INTO logs (
                location_id, camera_id, user_id, date, time, occurred_at,
                username, score, action, note, path, picture_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """

            # 执行SQL语句
            cursor.execute(
                insert_query,
                (
                    LOCATION_ID,
                    CAMERA_ID,
                    user_id,
                    occurred_at.date(),
                    occurred_at.time(),
                    occurred_at,
                    username,
                    score,
                    action,
                    note,
                    path,
                    picture_id,
                ),
            )

            # 提交到数据库执行
            connection.commit()
            new_log_id = cursor.lastrowid

            print(f"日志保存成功!")

    except Error as e:
        print(f"Error: {e}")

    finally:
        if connection and connection.is_connected():
            # 关闭游标和连接
            if cursor:
                cursor.close()
            connection.close()

    return new_log_id

def load_faces(model):
    identities = []
    faces = []
    connection = None
    cursor = None
    try:
        # 连接到MySQL数据库
        connection = mysql.connector.connect(**db_config)

        if connection.is_connected():
            # 创建游标对象
            cursor = connection.cursor()

            # SQL 语句: 优先使用 users.name 展示，兼容历史数据回退到 pictures.name
            sql = """
            SELECT p.user_id, COALESCE(u.name, p.name) AS display_name, p.face_image, p.id
            FROM pictures p
            LEFT JOIN users u ON p.user_id = u.id
            """

            # 执行SQL语句
            cursor.execute(sql)

            # 获取所有记录列表
            results = cursor.fetchall()

            for row in tqdm(results, desc="正在处理数据库人像"):
                user_id = row[0]
                name = row[1]
                image_bytes = row[2]
                picture_id = row[3]
                # print(name)
                if image_bytes is not None:
                    file = FileStorage(BytesIO(image_bytes))

                    LOADED_FACES_PATH.mkdir(parents=True, exist_ok=True)
                    file_tag = str(user_id) if user_id is not None else 'legacy'
                    file.save(str(LOADED_FACES_PATH / (name + '_' + file_tag + '_' + str(picture_id) + '.jpg')))

                    try:
                        aligned_rgb_img = align.get_aligned_face(file)
                        identities.append({'user_id': user_id, 'name': name, 'picture_id': picture_id})
                        bgr_tensor_input = to_input(aligned_rgb_img)
                        with torch.no_grad():
                            feature, _ = model(bgr_tensor_input)
                        faces.append(feature.detach())
                    except Exception as e:
                        print(name + '_' + file_tag + '_' + str(picture_id) + '.jpg 无人脸')
                else:
                    continue

        print(f"Faces loaded successfully.")

    except Error as e:
        print(f"Error: {e}")

    finally:
        if connection and connection.is_connected():
            # 关闭游标和连接
            if cursor:
                cursor.close()
            connection.close()
            print("MySQL connection is closed")

        return identities, faces


def load_picture_metadata():
    """
    读取 pictures 当前元数据，用于增量同步 identities（不读取人脸图像）。
    """
    rows = []
    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            cursor = connection.cursor()
            sql = """
            SELECT p.id, p.user_id, COALESCE(u.name, p.name) AS display_name
            FROM pictures p
            LEFT JOIN users u ON p.user_id = u.id
            """
            cursor.execute(sql)
            for picture_id, user_id, display_name in cursor.fetchall():
                rows.append({
                    'picture_id': picture_id,
                    'user_id': user_id,
                    'name': display_name,
                })
    except Error as e:
        print(f"Error: {e}")
    finally:
        if connection and connection.is_connected():
            if cursor:
                cursor.close()
            connection.close()
    return rows


def get_pictures_version():
    """
    返回 pictures 的版本标识。
    优先使用 COUNT(*) + MAX(updated_at)，若无 updated_at 列则退化到轻量聚合签名。
    """
    global _PICTURES_VERSION_SUPPORTS_UPDATED_AT

    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        if not connection.is_connected():
            return None

        cursor = connection.cursor()

        if _PICTURES_VERSION_SUPPORTS_UPDATED_AT is not False:
            try:
                sql = """
                SELECT
                    COUNT(*) AS cnt,
                    COALESCE(DATE_FORMAT(MAX(updated_at), '%Y-%m-%d %H:%i:%s.%f'), '')
                FROM pictures
                """
                cursor.execute(sql)
                cnt, max_updated_at = cursor.fetchone()
                _PICTURES_VERSION_SUPPORTS_UPDATED_AT = True
                return f"u:{cnt}:{max_updated_at}"
            except Error as e:
                if "Unknown column 'updated_at'" not in str(e):
                    raise
                _PICTURES_VERSION_SUPPORTS_UPDATED_AT = False

        # fallback: 无 updated_at 时，做轻量聚合签名（仍是单条 SQL）
        fallback_sql = """
        SELECT
            COUNT(*) AS cnt,
            COALESCE(MAX(id), 0) AS max_id,
            COALESCE(SUM(COALESCE(user_id, 0)), 0) AS sum_user_id,
            COALESCE(SUM(CRC32(COALESCE(name, ''))), 0) AS sum_name_crc
        FROM pictures
        """
        cursor.execute(fallback_sql)
        cnt, max_id, sum_user_id, sum_name_crc = cursor.fetchone()
        return f"f:{cnt}:{max_id}:{sum_user_id}:{sum_name_crc}"
    except Error as e:
        print(f"Error: {e}")
        return None
    finally:
        if connection and connection.is_connected():
            if cursor:
                cursor.close()
            connection.close()


def load_faces_by_picture_ids(model, picture_ids):
    """
    仅加载指定 picture_id 的人脸特征（用于新增记录增量同步）。
    """
    identities = []
    faces = []
    if not picture_ids:
        return identities, faces

    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            cursor = connection.cursor()

            placeholders = ','.join(['%s'] * len(picture_ids))
            sql = f"""
            SELECT p.user_id, COALESCE(u.name, p.name) AS display_name, p.face_image, p.id
            FROM pictures p
            LEFT JOIN users u ON p.user_id = u.id
            WHERE p.id IN ({placeholders})
            """
            cursor.execute(sql, tuple(picture_ids))
            results = cursor.fetchall()

            for row in results:
                user_id = row[0]
                name = row[1]
                image_bytes = row[2]
                picture_id = row[3]
                if image_bytes is None:
                    continue

                file = FileStorage(BytesIO(image_bytes))
                try:
                    aligned_rgb_img = align.get_aligned_face(file)
                    if aligned_rgb_img is None:
                        print(f"{name}_{picture_id}.jpg 无人脸")
                        continue
                    identities.append({'user_id': user_id, 'name': name, 'picture_id': picture_id})
                    bgr_tensor_input = to_input(aligned_rgb_img)
                    with torch.no_grad():
                        feature, _ = model(bgr_tensor_input)
                    faces.append(feature.detach())
                except Exception:
                    print(f"{name}_{picture_id}.jpg 无人脸")
    except Error as e:
        print(f"Error: {e}")
    finally:
        if connection and connection.is_connected():
            if cursor:
                cursor.close()
            connection.close()

    return identities, faces

def face_embedding(file, model, faces):
    with Image.open(file) as image:
        bgr_tensor_input = to_input(image)
    with torch.no_grad():
        feature, _ = model(bgr_tensor_input)
    faces.append(feature.detach())
    return faces

def compute_user(identities, received_features, face_features, return_picture_id=False):
    if not identities or not received_features or not face_features:
        if return_picture_id:
            return 'unknown', None, 0.0, None
        return 'unknown', None, 0.0

    try:
        similarity_scores = torch.cat(received_features) @ torch.cat(face_features).T
    except Exception as e:
        print("人脸不完全")
        if return_picture_id:
            return 'unknown', None, 0.0, None
        return 'unknown', None, 0.0

    sum_scores = similarity_scores.sum(dim=0)

    # 找到 sum_scores 中的最大值及其索引
    max_value, max_index = torch.max(sum_scores, dim=0)
    max_index = int(max_index.item())
    if max_index >= len(identities):
        if return_picture_id:
            return 'unknown', None, 0.0, None
        return 'unknown', None, 0.0

    avg_scores = float((max_value / len(received_features)).item())
    final_user = identities[max_index].get('name', 'unknown')
    final_user_id = identities[max_index].get('user_id')
    final_picture_id = identities[max_index].get('picture_id')

    if return_picture_id:
        return final_user, final_user_id, avg_scores, final_picture_id

    return final_user, final_user_id, avg_scores

def process_folder(image_path, de_model, iqa_model, conf):
    landmark(image_path)
    os.makedirs(image_path + '/dsl', exist_ok=True)
    dsl_fiqa(image_path, de_model, iqa_model, conf)

def re_compute_user(images, identities, face_features, image_path, face_model, return_picture_id=False):
    faces = []
    for image in images:
        faces = face_embedding(image_path + '/dsl/' + image, face_model, faces)

    if return_picture_id:
        result = compute_user(
            identities,
            faces,
            face_features,
            return_picture_id=True,
        )
    else:
        result = compute_user(identities, faces, face_features)

    img_dir = os.path.join(image_path, 'img')
    if os.path.isdir(img_dir):
        for filename in os.listdir(img_dir):
            src = os.path.join(img_dir, filename)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(image_path, filename)
            if os.path.exists(dst):
                stem, ext = os.path.splitext(filename)
                dst = os.path.join(image_path, f'{stem}_img{ext}')
                idx = 1
                while os.path.exists(dst):
                    dst = os.path.join(image_path, f'{stem}_img_{idx}{ext}')
                    idx += 1
            shutil.move(src, dst)
        shutil.rmtree(img_dir, ignore_errors=True)

    return result

def print_user(user, score, user_id=None):
    # 打印最大值和其索引位置
    print(f"最匹配: {user}")
    if user_id is not None:
        print(f"用户ID: {user_id}")
    print(f"置信度: {score}")

def _init_landmark_model():
    global _LANDMARK_MODEL, _LANDMARK_ARGS
    if _LANDMARK_MODEL is not None:
        return

    requested_device = os.getenv(
        'ADAFACE_DEVICE', 'cuda:0' if torch.cuda.is_available() else 'cpu'
    )
    _LANDMARK_ARGS = argparse.Namespace(
        bbox_margin_percentage=80,
        gpu_num=requested_device.split(':')[-1] if requested_device.startswith('cuda') else '0',
        device=requested_device,
        img_size=512, model_path=str(LANDMARK_MODEL_PATH),
        task='landmarks')

    device = _LANDMARK_ARGS.device
    model = FaceXFormer().to(_LANDMARK_ARGS.device)
    checkpoint = torch.load(_LANDMARK_ARGS.model_path, map_location=device)
    model.load_state_dict(checkpoint['state_dict_backbone'])
    model.eval()

    os.environ['CUDA_LAUNCH_BLOCKING'] = '1'
    _LANDMARK_MODEL = model


def landmark(image_path):
    _init_landmark_model()
    landmark_detect(_LANDMARK_MODEL, image_path, image_path, _LANDMARK_ARGS)

def sort_images(image_path, num=1):
    # 列出目录中的所有文件
    files = os.listdir(image_path)

    # 筛选出以.jpg结尾的文件，并提取文件名中的数字
    image_files = [f for f in files if f.endswith('.jpg')]
    image_scores = [float(f.split('.')[0]) for f in image_files]

    # 将数字和文件名组合成元组，并按数字排序
    sorted_images = sorted(zip(image_scores, image_files), reverse=True)

    images = [image[1] for image in sorted_images[:num]]

    return images

def chat_user(fay_url, user):
    if not user or not fay_url:
        return
    try:
        user_response = requests.get(fay_url + '/face/hello', params={'user': user.split('-')[0]})
    except Exception as e:
        print('fay请求失败')

if __name__ == '__main__':

    # landmark('received_image/2025-06-10/13:47:57')
    # DE, net, config = load_fiqa()
    # dsl_fiqa('received_image/2025-06-10/13:47:57', DE, net, config)
    #
    # face_model = load_pretrained_model('ir_50')
    # images = sort_images('received_image/2025-06-10/13:47:57', 3)
    # faces = []
    # for image in images:
    #     faces = face_embedding('received_image/2025-06-10/13:47:57' + '/' + image, face_model, faces)
    #     print(faces)

    current_time = datetime.now()
    add_log(current_time, '张学凯', 0, '进入')
