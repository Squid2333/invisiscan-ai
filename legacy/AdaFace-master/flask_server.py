import gc
import os
import shutil
import threading
from io import BytesIO

from PIL import Image
from werkzeug.datastructures import FileStorage

import requests
import torch
from datetime import datetime

from DSLFIQA.util import load_fiqa, predict_image_quality, dsl_fiqa
from inference import load_pretrained_model, to_input
from flask import Flask, request, jsonify
from utils import load_faces, add_log, compute_user, chat_user, re_compute_user, face_embedding, print_user, \
    update_list, process_folder, sort_images, add_user
from runtime_config import FAY_URL, RECEIVED_PATH

torch.set_num_threads(1)

received_path = str(RECEIVED_PATH)
tolerance = 0.35
fay_url = FAY_URL

face_model = load_pretrained_model('ir_50')
de_model, iqa_model, iqa_config = load_fiqa()
faces = []
identities = []


app = Flask(__name__)
@app.route('/face/undetected', methods=['POST'])
def face_undetected():
    global identities, faces

    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(current_time)
    folder_path = os.path.join(received_path, current_time.split(' ')[0], current_time.split(' ')[1])
    os.makedirs(folder_path, exist_ok=True)

    print("========Start========")
    pic_path = os.path.dirname(request.get_json()['filepath'])
    filenames = [f for f in os.listdir(pic_path) if f.endswith('.jpg')]
    for filename in filenames:
        src_file = os.path.join(pic_path, filename)
        dst_file = os.path.join(folder_path, filename)
        shutil.move(src_file, dst_file)
    print('未识别到人脸')
    print('=========Done========')


    add_log(current_time, None, 0, '未识别到人脸', folder_path + '_fail')
    os.rename(folder_path, folder_path + '_fail')


    return jsonify({
        'message': 'Files uploaded and save_face recognized',
        'final_user': 'No face detected',
    }), 200

@app.route('/face/matrix', methods=['POST'])
def matrix_recognize():
    global identities, faces
    current_time = datetime.now()

    print(current_time.strftime('%Y-%m-%d %H:%M:%S'))

    folder_path = os.path.join(received_path, current_time.strftime('%Y-%m-%d'), current_time.strftime('%H:%M:%S'))
    os.makedirs(folder_path, exist_ok=True)

    received_features = []
    print("========Start========")
    for file in request.files.values():
        file_path = os.path.join(folder_path, file.filename)
        file.save(file_path)

        try:
            received_features = face_embedding(file.stream, face_model, received_features)
        except Exception as e:
            continue

    user, user_id, score = compute_user(identities, received_features, faces)

    try:
        if score <= tolerance:
            process_folder(folder_path, de_model, iqa_model, iqa_config)
            images = sort_images(folder_path + '/dsl')
            user, user_id, score = re_compute_user(images, identities, faces, folder_path, face_model)
            if score < tolerance - 0.05:
                user = 'unknown'
                user_id = None
                path = add_user(folder_path + '/dsl/', images)
                if path is not None:
                    identities, faces = update_list(path, identities, faces, face_model)
    except Exception as e:
        user = 'unknown'
        user_id = None
        print("人脸图像不完整, 精确检测失败")

    print_user(user, score, user_id)
    print('=========Done========')

    add_log(current_time, user, float(score), '进入', folder_path, user_id=user_id)
    os.rename(folder_path, folder_path + '_' + user)
    chat_user(fay_url, user)


    return jsonify({
        'message': 'Files uploaded and save_face recognized',
        'final_user': user
    }), 200

@app.route('/face/local', methods=['POST'])
def local_recognize():
    global identities, faces
    current_time = request.get_json()['time']

    folder_path = os.path.join(received_path, current_time.split(' ')[0], current_time.split(' ')[1])
    os.makedirs(folder_path, exist_ok=True)

    received_features = []
    print("========Start========")
    print(current_time)

    pic_path = request.get_json()['filepath']
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

    user, user_id, score = compute_user(identities, received_features, faces)

    try:
        if score <= tolerance:
            process_folder(folder_path, de_model, iqa_model, iqa_config)
            images = sort_images(folder_path + '/dsl')
            user, user_id, score = re_compute_user(images, identities, faces, folder_path, face_model)
            if score < tolerance - 0.05:
                user = 'unknown'
                user_id = None
                path = add_user(folder_path + '/dsl/', images)
                if path is not None:
                    identities, faces = update_list(path, identities, faces, face_model)
    except Exception as e:
        user = 'unknown'
        user_id = None
        print("人脸图像不完整, 精确检测失败")

    print_user(user, score, user_id)
    print('=========Done========')

    add_log(current_time, user, float(score), '进入', folder_path, user_id=user_id)
    os.rename(folder_path, folder_path + '_' + user)
    chat_user(fay_url, user)


    return jsonify({
        'message': 'Files uploaded and save_face recognized',
        'final_user': user
    }), 200

if __name__ == "__main__":

    # 确保上传文件夹存在
    if not os.path.exists(received_path):
        os.makedirs(received_path)

    identities, faces = load_faces(face_model)

    app.run(debug=False, host='127.0.0.1', port=8766, threaded=False)
