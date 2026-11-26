import gc
import json
import os
import shutil
import time
import redis
from DSLFIQA.util import load_fiqa, predict_image_quality, dsl_fiqa
from inference import load_pretrained_model, to_input
from utils import load_faces, add_log, compute_user, chat_user, re_compute_user, face_embedding, print_user, \
    update_list, process_folder, sort_images, add_user, load_picture_metadata, load_faces_by_picture_ids, \
    get_pictures_version
from runtime_config import (
    FAY_URL,
    INDOOR_OUTPUT_PATH,
    RECEIVED_PATH,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PORT,
    REDIS_QUEUE,
)


received_path = str(RECEIVED_PATH)
tolerance = 0.35
fay_url = FAY_URL
FULL_RELOAD_INTERVAL_SECONDS = 6 * 60 * 60


def build_picture_index(identities):
    index = {}
    for i, identity in enumerate(identities):
        picture_id = identity.get('picture_id')
        if picture_id is not None:
            index[picture_id] = i
    return index


def remove_face_by_picture_id(picture_id, identities, faces, idx_by_picture_id):
    index = idx_by_picture_id.get(picture_id)
    if index is None:
        return False
    last_index = len(identities) - 1
    if index != last_index:
        identities[index] = identities[last_index]
        faces[index] = faces[last_index]
        moved_picture_id = identities[index].get('picture_id')
        if moved_picture_id is not None:
            idx_by_picture_id[moved_picture_id] = index
    identities.pop()
    faces.pop()
    del idx_by_picture_id[picture_id]
    return True


def sync_faces_incremental(face_model, identities, faces, idx_by_picture_id):
    metadata_rows = load_picture_metadata()
    if metadata_rows is None:
        return False

    db_by_picture_id = {row['picture_id']: row for row in metadata_rows}
    db_picture_ids = set(db_by_picture_id.keys())
    cache_picture_ids = set(idx_by_picture_id.keys())

    removed_ids = cache_picture_ids - db_picture_ids
    added_ids = db_picture_ids - cache_picture_ids
    kept_ids = db_picture_ids & cache_picture_ids

    removed_count = 0
    updated_count = 0
    added_count = 0

    for picture_id in list(removed_ids):
        if remove_face_by_picture_id(picture_id, identities, faces, idx_by_picture_id):
            removed_count += 1

    for picture_id in kept_ids:
        index = idx_by_picture_id.get(picture_id)
        if index is None:
            continue
        identity = identities[index]
        row = db_by_picture_id[picture_id]
        next_user_id = row['user_id']
        next_name = row['name']
        if identity.get('user_id') != next_user_id or identity.get('name') != next_name:
            identity['user_id'] = next_user_id
            identity['name'] = next_name
            updated_count += 1

    if added_ids:
        new_identities, new_faces = load_faces_by_picture_ids(face_model, sorted(added_ids))
        for identity, face_feature in zip(new_identities, new_faces):
            picture_id = identity.get('picture_id')
            if picture_id is None:
                continue
            if picture_id in idx_by_picture_id:
                continue
            idx_by_picture_id[picture_id] = len(identities)
            identities.append(identity)
            faces.append(face_feature)
            added_count += 1

    if removed_count or updated_count or added_count:
        print(
            f'增量同步完成: +{added_count} / ~{updated_count} / -{removed_count}, '
            f'当前缓存 {len(identities)} 张'
        )
    return True

def select_images_from_body_dir(body_dir: str, target_dir: str, id: str, num_select: int = 6):
    """
    从body_dir中筛选图片，排除前20张和后20张，剩余图片中平均选取指定数量（默认6张）到目标路径

    Args:
        body_dir: 源图片目录路径（即body_dir = person_dir + '/body'）
        target_dir: 目标保存路径（需提前创建或由函数自动创建）
        num_select: 要选取的图片数量（6-7张，默认6张）
    """
    # 验证参数合法性
    if not os.path.exists(body_dir):
        print(f"错误：源目录不存在 -> {body_dir}")
        return

    # 自动创建目标目录（若不存在）
    os.makedirs(target_dir, exist_ok=True)

    # 1. 获取目录中所有图片文件
    image_suffixes = ('.jpg', '.jpeg', '.png', '.bmp', '.gif')  # 常见图片格式
    image_files = []
    for filename in os.listdir(body_dir):
        # 筛选图片文件（忽略大小写）
        if filename.lower().endswith(image_suffixes):
            # 存储完整路径
            image_path = os.path.join(body_dir, filename)
            # 确保是文件（不是子目录）
            if os.path.isfile(image_path):
                image_files.append(image_path)

    # 3. 按文件名排序
    image_files.sort(key=lambda x: os.path.basename(x))  # 按文件名排序（而非路径）

    # 4. 排除前20张和后20张
    filtered_images = image_files[3:-2]  # 切片：从索引20到倒数第20个（不包含倒数20个）
    filtered_count = len(filtered_images)

    if filtered_count < num_select:
        print(f"警告：筛选后剩余 {filtered_count} 张图片，不足 {num_select} 张，将全部选取")
        selected_images = filtered_images
    else:
        # 5. 平均选取指定数量的图片（按索引间隔采样）
        step = filtered_count // num_select  # 基础步长
        remainder = filtered_count % num_select  # 余数（用于分配额外的1张）

        selected_images = []
        index = 0
        for i in range(num_select):
            selected_images.append(filtered_images[index])
            index += step + 1 if i < remainder else step

    for index, img_path in enumerate(selected_images):
        # 保留原文件名
        # target_path = os.path.join(target_dir, os.path.basename(img_path))
        target_path = os.path.join(target_dir, f'{id}_{index}.jpg')

        # 复制图片（若目标文件已存在，会覆盖，可改为shutil.copy2保留元数据）
        shutil.copy(img_path, target_path)

def local_recognize(person_data, identities, faces):
    person_dir = person_data['person_dir']
    pic_path = os.path.join(person_dir, 'face')
    current_time = person_data['first_time']

    folder_path = os.path.join(received_path, current_time.split(' ')[0], current_time.split(' ')[1])
    os.makedirs(folder_path, exist_ok=True)

    print("========开始========")
    print(current_time)

    if os.path.exists(pic_path):
        received_features = []
        for filename in os.listdir(pic_path):
            src_file = os.path.join(pic_path, filename)
            dst_file = os.path.join(folder_path, filename)
            shutil.move(src_file, dst_file)

        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)
            try:
                received_features = face_embedding(file_path, face_model, received_features)
            except Exception as e:
                continue

        user, user_id, score, picture_id = compute_user(
            identities,
            received_features,
            faces,
            return_picture_id=True,
        )

        if score <= tolerance:
            process_folder(folder_path, de_model, iqa_model, iqa_config)
            images = sort_images(folder_path + '/dsl')
            user, user_id, score, picture_id = re_compute_user(
                images,
                identities,
                faces,
                folder_path,
                face_model,
                return_picture_id=True,
            )
            if score < tolerance - 0.001:
                user = 'unknown'
                user_id = None
                picture_id = None
                path = add_user(folder_path + '/dsl/', images)
                if path is not None:
                    identities, faces = update_list(path, identities, faces, face_model)

        new_id = add_log(
            current_time,
            user,
            float(score),
            '进入',
            folder_path,
            user_id=user_id,
            picture_id=picture_id,
        )
        # if os.path.exists(folder_path + '_' + user):
        #     shutil.rmtree(folder_path)
        # else:
        #     os.rename(folder_path, folder_path + '_' + user)
        chat_user(fay_url, user)
        print_user(user, score, user_id)
        # 清理 received_features 变量，释放内存
        del received_features
        gc.collect()  # 强制进行垃圾回收
    else:
        print('未识别到人脸')
        new_id = add_log(
            current_time,
            None,
            0,
            '进入',
            folder_path,
            picture_id=None,
        )
        # os.rename(folder_path, folder_path + '_fail')

    body_dir = person_dir + '/body'
    target_dir = os.path.join(str(INDOOR_OUTPUT_PATH), current_time.split(' ')[0], str(new_id))
    select_images_from_body_dir(body_dir, target_dir, new_id)

    print('=========完成========')


if __name__ == "__main__":
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)

    face_model = load_pretrained_model('ir_50')
    de_model, iqa_model, iqa_config = load_fiqa()

    # 确保上传文件夹存在
    if not os.path.exists(received_path):
        os.makedirs(received_path)

    identities, faces = load_faces(face_model)
    idx_by_picture_id = build_picture_index(identities)
    last_full_reload_ts = time.time()
    known_version = get_pictures_version()
    print('人脸识别系统已启动')

    while True:
        try:
            task = r.blpop(REDIS_QUEUE, timeout=1)  # 队列名可通过 ADAFACE_REDIS_QUEUE 配置

            now = time.time()
            if now - last_full_reload_ts >= FULL_RELOAD_INTERVAL_SECONDS:
                identities[:], faces[:] = load_faces(face_model)
                idx_by_picture_id = build_picture_index(identities)
                known_version = get_pictures_version()
                last_full_reload_ts = now
                print(f'定时全量重载完成，当前缓存 {len(identities)} 张')

            if task is None:
                continue

            _, task_json = task
            latest_version = get_pictures_version()
            if latest_version is not None and latest_version != known_version:
                success = sync_faces_incremental(face_model, identities, faces, idx_by_picture_id)
                if success:
                    known_version = latest_version
            person_data = json.loads(task_json)  # 反序列化任务数据
            local_recognize(person_data, identities, faces)  # 处理任务
        except Exception as e:
            print(e)
