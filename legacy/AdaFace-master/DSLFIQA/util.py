import os
from datetime import datetime
from pathlib import Path

import torch
import numpy as np
import random
import cv2
from pyhocon import ConfigFactory

from torchvision import transforms
from torch.utils.data import DataLoader
from DSLFIQA.utils.inference_process import ToTensor, Normalize
from DSLFIQA.models.iqa import IQA, degradation_Encoder
from runtime_config import DSLFIQA_CONFIG_PATH, DSLFIQA_DE_PATH, DSLFIQA_IQA_PATH


def _runtime_device():
    """Return the requested torch device, falling back to CPU when needed."""
    requested = os.getenv('ADAFACE_DEVICE')
    if requested:
        if requested.startswith('cuda') and not torch.cuda.is_available():
            return torch.device('cpu')
        return torch.device(requested)
    return torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')


def setup_seed(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

class Image(torch.utils.data.Dataset):
    def __init__(self, image_path, dataset, transform, num_crops=20, num_landmark=500, normalization=True, img_size=224):
        super(Image, self).__init__()

        self.img_name = image_path

        self.img = cv2.imread(image_path, cv2.IMREAD_COLOR)
        self.img = cv2.cvtColor(self.img, cv2.COLOR_BGR2RGB)
        self.img = np.array(self.img).astype('float32') / 255
        self.img = np.transpose(self.img, (2, 0, 1))
        self.landmark = np.load(image_path.replace('img', 'landmark').replace('.png', '.npy'))

        self.transform = transform
        self.num_landmark = num_landmark
        self.normalization = normalization

        c, h, w = self.img.shape
        new_h = img_size
        new_w = img_size

        self.img_patches = []

        self.not_landmark = []
        other_point = len(self.landmark) + 1
        self.points_patch = []

        for i in range(num_crops):
            self.filtered_landmark = []
            points = []
            top = np.random.randint(0, h - new_h)
            left = np.random.randint(0, w - new_w)
            patch = self.img[:, top: top + new_h, left: left + new_w]
            self.img_patches.append(patch)

            for idx in range(len(self.landmark)):
                if top <= self.landmark[idx, 1] <= top+new_h and left <= self.landmark[idx, 0] <= left+new_w:
                    self.filtered_landmark.append(idx)
                else:
                    self.not_landmark.append(self.landmark[idx,:])
            if len(self.filtered_landmark) < self.num_landmark:
                res = self.num_landmark - len(self.filtered_landmark)
                points = self.filtered_landmark
                points += [other_point] * res
            else:
                points_idx = np.random.choice(len(self.filtered_landmark), size=self.num_landmark, replace=False)
                points += [self.filtered_landmark[i] for i in points_idx]

            if self.normalization:
                points = np.array([x for x in points])
                array_min, array_max = points.min(), points.max()
                points = ((points - array_min) / (array_max - array_min + 1e-6)) * 2 - 1
                self.points_patch.append(points)

        self.points_patch = np.array(self.points_patch)
        self.img_patches = np.array(self.img_patches)

    def get_patch(self, idx):
        patch = self.img_patches[idx]
        landmark_patch = self.points_patch[idx]
        sample = {'d_img_org': patch, 'score': 0, 'd_name': self.img_name, 'landmark_org': landmark_patch}
        if self.transform:
            sample = self.transform(sample)
        return sample

def predict_image_quality(path_data, config, DE, net):
    # 设置随机种子
    setup_seed(20)

    # 定义转换
    transform = transforms.Compose([Normalize(0.5, 0.5), ToTensor()])

    # 加载图像和npy文件
    img = cv2.imread(path_data, cv2.IMREAD_COLOR)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = np.array(img).astype('float32') / 255
    img = np.transpose(img, (2, 0, 1))
    landmark = np.load(path_data.replace('img', 'landmark').replace('.png', '.npy'))

    # 创建Image对象
    img_obj = Image(image_path=path_data, dataset='custom', transform=transform,
                    num_crops=config["Train.num_crops"], num_landmark=config["Train.num_landmark"],
                    normalization=config["Train.normalization"], img_size=config["Train.img_size"])

    img_obj.landmark = landmark

    # 进行预测
    avg_score = 0
    for i in range(config["Train.num_crops"]):
        with torch.no_grad():
            patch_sample = img_obj.get_patch(i)
            device = next(DE.parameters()).device
            patch = patch_sample['d_img_org'].to(device)
            patch = patch.unsqueeze(0)
            landmark = patch_sample['landmark_org'].to(device)
            landmark = landmark.unsqueeze(0)
            style_q, __ = DE(patch)
            score = net(patch, style_q, landmark)
            avg_score += score

    pred = avg_score / config["Train.num_crops"]
    pred = pred.cpu().numpy()

    return pred
def load_fiqa():

    f = open(DSLFIQA_CONFIG_PATH)
    conf_text = f.read()
    f.close()
    config = ConfigFactory.parse_string(conf_text)

    # 加载模型
    device = _runtime_device()
    DE = degradation_Encoder().to(device)
    DE.load_state_dict(torch.load(DSLFIQA_DE_PATH, map_location=device), strict=False)
    DE.eval()

    net = IQA(embed_dim=config["Train.embed_dim"], num_outputs=config["Train.num_outputs"],
              dim_mlp=config["Train.dim_mlp"],
              patch_size=config["Train.patch_size"], img_size=config["Train.img_size"],
              window_size=config["Train.window_size"],
              depths=config["Train.depths"], num_heads=config["Train.num_heads"], num_tab=config["Train.num_tab"],
              scale=config["Train.scale"], freq=config["Train.freq"],
              use_landmark=config["Train.use_landmark"], add_mlp=config["Train.add_mlp"])
    net = net.to(device)
    net.load_state_dict(torch.load(DSLFIQA_IQA_PATH, map_location=device), strict=False)
    net.eval()

    return DE, net, config

def dsl_fiqa(path, de_model, iqa_model, config):

    for filenames in os.listdir(path + '/img'):
        if filenames.endswith('.png'):
            pic_path = path + '/img/' + filenames
            # print(pic_path)
            try:
                pred = predict_image_quality(pic_path, config, de_model, iqa_model)
                image_path = path + '/' + filenames
                os.rename(image_path.replace('.png', '.jpg'), path + '/dsl/' + str(pred[0]) + '.jpg')
                print("评估成功")
            except Exception as e:
                print("人脸不完全")




if __name__ == '__main__':
    DE, net, config = load_fiqa()
    dsl_fiqa('received_image/2025-06-10/13:27:19', DE, net, config)
    # predict_image_quality('received_image/2025-06-10/13:27:19/img/0.5164885.png', DE, net, config)
