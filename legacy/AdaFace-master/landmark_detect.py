import os
from datetime import time, datetime

import numpy as np
import cv2
import torch
import torch.nn as nn
import torchvision
from torchvision.transforms import InterpolationMode
import argparse
from math import cos, sin
from PIL import Image
from DSLFIQA.landmark_detection.network import FaceXFormer
from facenet_pytorch import MTCNN
import json
from DSLFIQA.landmark_detection.postprocess import alignment_procedure, rotate_facial_area
from runtime_config import LANDMARK_MODEL_PATH, LOADED_FACES_PATH
from tqdm import tqdm
import torch.nn.functional as F


def denorm_points(points, h, w, align_corners=False):
    if align_corners:
        denorm_points = (points + 1) / 2 * torch.tensor([w - 1, h - 1], dtype=torch.float32).to(points).view(1, 1, 2)
    else:
        denorm_points = ((points + 1) * torch.tensor([w, h], dtype=torch.float32).to(points).view(1, 1, 2) - 1) / 2

    return denorm_points


def unnormalize(tensor, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]):
    mean = torch.tensor(mean).view(-1, 1, 1)
    std = torch.tensor(std).view(-1, 1, 1)
    tensor = tensor * std + mean
    tensor = torch.clamp(tensor, 0, 1)
    return tensor


def adjust_bbox(x_min, y_min, x_max, y_max, image_width, image_height, margin_percentage):
    width = x_max - x_min
    height = y_max - y_min

    increase_width = width * (margin_percentage / 100.0)
    increase_height = height * (margin_percentage / 100.0)

    x_min_adjusted = max(0, x_min - increase_width)
    y_min_adjusted = max(0, y_min - increase_height)
    x_max_adjusted = min(image_width, x_max + increase_width)
    y_max_adjusted = min(image_height, y_max + increase_height)

    return x_min_adjusted, y_min_adjusted, x_max_adjusted, y_max_adjusted


def crop_and_align(args, model):
    mtcnn = MTCNN(keep_all=True)
    image = Image.open(args.image_path)
    width, height = image.size
    boxes, probs = mtcnn.detect(image)

    transforms_image = torchvision.transforms.Compose([
        torchvision.transforms.Resize(size=(args.img_size, args.img_size), interpolation=InterpolationMode.BICUBIC),
        torchvision.transforms.ToTensor(),
        torchvision.transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    try:
        x_min, y_min, x_max, y_max = boxes[0][0], boxes[0][1], boxes[0][2], boxes[0][3]
        x_min, y_min, x_max, y_max = adjust_bbox(x_min, y_min, x_max, y_max, width, height,
                                                 margin_percentage=args.bbox_margin_percentage)
        image = image.crop((int(x_min), int(y_min), int(x_max), int(y_max)))

        image = transforms_image(image)
        task = torch.tensor([1])

        data = {'image': image,
                'label': {"segmentation": torch.zeros([args.img_size, args.img_size]), "lnm_seg": torch.zeros([5, 2]),
                          "landmark": torch.zeros([68, 2]), "headpose": torch.zeros([3]),
                          "attribute": torch.zeros([40]), "a_g_e": torch.zeros([3]), 'visibility': torch.zeros([29])},
                'task': task}
        images, labels, tasks = data["image"], data["label"], data["task"]
        images = images.unsqueeze(0).to(device=args.device)

        for k in labels.keys():
            labels[k] = labels[k].unsqueeze(0).to(device=args.device)

        tasks = tasks.to(device=args.device)

        landmark_output, headpose_output, attribute_output, visibility_output, age_output, gender_output, race_output, seg_output = model(
            images, labels, tasks)

        image = unnormalize(images[0].detach().cpu())
        denorm_landmarks = denorm_points(landmark_output.view(-1, 68, 2)[0], args.img_size, args.img_size)
        denorm_landmarks = denorm_landmarks.detach().cpu()
        # breakpoint()

        xmin, xmax = denorm_landmarks[0, :, 0].min(), denorm_landmarks[0, :, 0].max()
        ymin, ymax = denorm_landmarks[0, :, 1].min(), denorm_landmarks[0, :, 1].max()
        x, y, w, h = xmin, ymin, xmax - xmin, ymax - ymin

        im = image.permute(1, 2, 0).numpy()
        im = (im * 255).astype(np.uint8)
        im = np.ascontiguousarray(im)

        nose = denorm_landmarks[0, 33, :].tolist()
        left_eye = denorm_landmarks[0, 47, :].tolist()
        right_eye = denorm_landmarks[0, 38, :].tolist()

        aligned_img, rotate_angle, rotate_direction = alignment_procedure(
            img=im, left_eye=right_eye, right_eye=left_eye, nose=nose
        )

        # find new facial area coordinates after alignment
        rotated_x1, rotated_y1, rotated_x2, rotated_y2 = rotate_facial_area(
            (0, 0, args.img_size, args.img_size), rotate_angle, rotate_direction, (args.img_size, args.img_size)
        )

        facial_img = aligned_img[
                     int(rotated_y1): int(rotated_y2), int(rotated_x1): int(rotated_x2)
                     ]
        facial_img = cv2.resize(facial_img, (args.img_size, args.img_size))
        output_image = Image.fromarray(facial_img)

        return output_image

    except:
        output_image = Image.open(args.image_path)
        return output_image


def landmark_detection(args, model, aligned_img):
    transforms_image = torchvision.transforms.Compose([
        torchvision.transforms.Resize(size=(224, 224), interpolation=InterpolationMode.BICUBIC),
        torchvision.transforms.ToTensor(),
        torchvision.transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    mtcnn = MTCNN(keep_all=True)
    image = aligned_img
    width, height = image.size
    boxes, probs = mtcnn.detect(image)

    args.landmark_results_path = os.path.join(args.results_path, 'landmark')
    args.aligned_results_path = os.path.join(args.results_path, 'img')
    os.makedirs(args.landmark_results_path, exist_ok=True)
    os.makedirs(args.aligned_results_path, exist_ok=True)

    try:
        image = transforms_image(image)
        task = torch.tensor([1])

        data = {'image': image, 'label': {"segmentation": torch.zeros([224, 224]), "lnm_seg": torch.zeros([5, 2]),
                                          "landmark": torch.zeros([68, 2]), "headpose": torch.zeros([3]),
                                          "attribute": torch.zeros([40]), "a_g_e": torch.zeros([3]),
                                          'visibility': torch.zeros([29])}, 'task': task}
        images, labels, tasks = data["image"], data["label"], data["task"]
        images = images.unsqueeze(0).to(device=args.device)

        for k in labels.keys():
            labels[k] = labels[k].unsqueeze(0).to(device=args.device)

        tasks = tasks.to(device=args.device)

        landmark_output, headpose_output, attribute_output, visibility_output, age_output, gender_output, race_output, seg_output = model(
            images, labels, tasks)

        image = unnormalize(images[0].detach().cpu())

        denorm_landmarks = denorm_points(landmark_output.view(-1, 68, 2)[0], 224, 224)
        denorm_landmarks = denorm_landmarks.detach().cpu()

        im = image.permute(1, 2, 0).numpy()
        im = (im * 255).astype(np.uint8)
        im = np.ascontiguousarray(im)

        resized_image = F.interpolate(image.unsqueeze(0), size=(args.img_size, args.img_size), mode='bilinear',
                                      align_corners=False)
        final_image = resized_image.squeeze(0)

        scale_factor = args.img_size / 224
        denorm_landmarks = denorm_landmarks * scale_factor

        final_save_path = os.path.join(args.landmark_results_path, args.image_path.split('/')[-1][:-4] + '.npy')
        output_landmark = denorm_landmarks[0].numpy()
        np.save(final_save_path, output_landmark)
        image_save_path = os.path.join(args.aligned_results_path, args.image_path.split('/')[-1][:-4] + '.png')
        aligned_img.save(image_save_path)

    except:
        image_save_path = os.path.join(args.aligned_results_path, args.image_path.split('/')[-1][:-4] + '.png')
        aligned_img.save(image_save_path)
        print('No face detected in {} !!! '.format(args.image_path.split('/')[-1]))
        breakpoint()


def landmark_detect(model, image_path, results_path, args):
    args.image_path = image_path
    args.results_path = results_path

    image_files = [os.path.join(args.image_path, f) for f in os.listdir(args.image_path) if f.lower().endswith('.jpg')]
    image_files = sorted(image_files)
    total_images = len(image_files)

    # 限制最多选取20张图片，如果超过20张则平均采样
    max_samples = 20
    if total_images > max_samples:
        # 计算采样步长，确保均匀选取20张
        step = total_images // max_samples
        # 处理不能整除的情况，确保最后一张也能被选中
        sample_indices = list(range(0, total_images, step))[:max_samples]
        # 如果采样后不足20张（如总数不是step的整数倍），补充最后几张
        if len(sample_indices) < max_samples:
            remaining = max_samples - len(sample_indices)
            sample_indices.extend(range(total_images - remaining, total_images))
        # 根据采样索引获取图片路径
        selected_files = [image_files[i] for i in sample_indices]
    else:
        # 不足20张则全部选取
        selected_files = image_files

    # 构建完整图片路径列表
    image_list = [f for f in selected_files]

    for image_path in tqdm(image_list):
        args.image_path = image_path
        aligned_img = crop_and_align(args, model)
        landmark_detection(args, model, aligned_img)


def load_model(args):
    device = args.device
    model = FaceXFormer().to(device)
    weights_path = args.model_path
    checkpoint = torch.load(weights_path, map_location=device)
    model.load_state_dict(checkpoint['state_dict_backbone'])

    model.eval()

    os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

    return model


if __name__ == "__main__":
    image_path = str(LOADED_FACES_PATH)
    results_path = str(LOADED_FACES_PATH)
    default_device = os.getenv(
        'ADAFACE_DEVICE', 'cuda:0' if torch.cuda.is_available() else 'cpu'
    )

    args = argparse.Namespace(
        bbox_margin_percentage=80,
        gpu_num=default_device.split(':')[-1] if default_device.startswith('cuda') else '0',
        device=default_device,
        img_size=512, model_path=str(LANDMARK_MODEL_PATH),
        task='landmarks')

    device = args.device
    model = FaceXFormer().to(args.device)
    checkpoint = torch.load(args.model_path, map_location=device)
    model.load_state_dict(checkpoint['state_dict_backbone'])

    model.eval()

    os.environ['CUDA_LAUNCH_BLOCKING'] = '1'
    landmark_detect(model, image_path, results_path, args)

