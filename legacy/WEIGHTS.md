# 本地权重清单

这些文件已放在工作区，但被仓库根目录 `.gitignore` 排除，不会提交到 GitHub。

## RGBDSort

```text
weights/yolo26m-seg.pt                 # RGBsort.py
weights/video_depth_anything_vits.pth  # RGBsort.py / sort.py
weights/yolo11m-seg.pt                 # sort.py / track_comsumer.py
weights/yolo11m_depth.pt               # sort.py
weights/lstm.pth                        # track_comsumer.py
weights/lstm_norm.npz                   # track_comsumer.py
weights/lstm_labels.json                # track_comsumer.py（轻量元数据，可提交）
weights/lstm_best.pth                   # 训练/备用权重
weights/door.pth                        # 备用权重
face_alignment/mtcnn_pytorch/src/weights/{pnet,rnet,onet}.npy
```

## AdaFace-master

```text
pretrained/adaface_ir50_ms1mv2.ckpt
DSLFIQA/ckpt/custom/DE.pt
DSLFIQA/ckpt/custom/IQA.pt
DSLFIQA/landmark_detection/ckpts/model.pt
face_alignment/mtcnn_pytorch/src/weights/{pnet,rnet,onet}.npy
```

若从 GitHub 重新克隆，需把上述文件从本地权重归档恢复到相同路径，或用各项目的 `*_PATH` 环境变量指向外部权重目录。
