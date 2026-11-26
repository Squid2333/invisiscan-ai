import numpy

import net
import torch
from face_alignment import align
import numpy as np

from runtime_config import ADAFACE_MODEL_PATH


adaface_models = {
    'ir_50': ADAFACE_MODEL_PATH,
}

def load_pretrained_model(architecture='ir_50'):
    # load model and pretrained statedict
    assert architecture in adaface_models.keys()
    model = net.build_model(architecture)
    statedict = torch.load(adaface_models[architecture], map_location='cpu')['state_dict']
    model_statedict = {key[6:]:val for key, val in statedict.items() if key.startswith('model.')}
    model.load_state_dict(model_statedict)
    model.eval()
    return model

def to_input(pil_rgb_image):
    np_img = np.array(pil_rgb_image)
    brg_img = ((np_img[:,:,::-1] / 255.) - 0.5) / 0.5

    # tensor = torch.tensor([brg_img.transpose(2,0,1)]).float()
    tensor = torch.tensor(numpy.array([brg_img.transpose(2, 0, 1)]), dtype=torch.float32)
    return tensor

if __name__ == '__main__':

    model = load_pretrained_model('ir_50')


    # saved_faces_features = []
    # saved_faces_path = r'./save_face'
    # for fname in sorted(os.listdir(saved_faces_path)):
    #     path = os.path.join(saved_faces_path, fname)
    #     aligned_rgb_img = align.get_aligned_face(path)
    #     bgr_tensor_input = to_input(aligned_rgb_img)
    #     feature, _ = model(bgr_tensor_input)
    #     saved_faces_features.append(feature)

    # feature, norm = model(torch.cat(saved_faces_features))
    feature, norm = model(torch.randn(2,3,112,112))
    face_image_path = r'./save_face'
    test_image_path = r'received_image_old2/20240925135025_刘宜岭'


    face_features = []
    test_features = []
    names = []
    num = 0
    for fname in sorted(os.listdir(face_image_path)):
        names.append(fname)
        path = os.path.join(face_image_path, fname)
        aligned_rgb_img = align.get_aligned_face(path)
        bgr_tensor_input = to_input(aligned_rgb_img)
        feature, _ = model(bgr_tensor_input)
        face_features.append(feature)

    for fname in sorted(os.listdir(test_image_path)):
        path = os.path.join(test_image_path, fname)
        aligned_rgb_img = align.get_aligned_face(path)
        if aligned_rgb_img is not None:
            num += 1
            bgr_tensor_input = to_input(aligned_rgb_img)
            feature, _ = model(bgr_tensor_input)
            test_features.append(feature)

    print(face_features)
    print(test_features)

    similarity_scores = torch.cat(test_features) @ torch.cat(face_features).T

    sum_scores = torch.zeros_like(similarity_scores[0])

    # 累加 similarity_scores 中的每个张量
    for scores in similarity_scores:
        # print(scores)
        sum_scores = sum_scores + scores

    # 找到 sum_scores 中的最大值及其索引
    max_value, max_index = torch.max(sum_scores, dim=0)

    print(sum_scores, len(sum_scores))

    # 打印最大值和其索引位置
    print(f"最大值的位置: {max_index}")
    print(f"最匹配: {names[max_index]}")
    print(f"{num}")
    
