# 运行链路核心代码

这里保存 `depth.sh` 实际调用到的两个项目的核心代码副本：

- `RGBDSort/`：视频流发布、RGB/深度检测、跟踪和轨迹分类；入口是
  `rtsp_server.py`、`RGBsort.py`、`track_comsumer.py`。
- `AdaFace-master/`：人脸特征识别和图像质量筛选；入口是
  `redis_comsumer.py`。

原始项目位于外部磁盘，未被修改。这里的副本使用项目内的相对资源路径，部署相关的路径、数据库连接和服务地址通过环境变量提供。

## 权重和数据

运行所需的模型权重已经放在对应项目目录中，但仓库根目录的 `.gitignore` 会忽略它们，因此不会随 Git 提交到 GitHub。当前副本包含的主要权重包括：

- `RGBDSort/weights/` 下的 YOLO、VideoDepthAnything 和 LSTM 权重；
- `AdaFace-master/pretrained/` 下的 AdaFace 权重；
- `AdaFace-master/DSLFIQA/ckpt/` 下的质量评估和关键点模型权重；
- 两个项目各自的 MTCNN 权重。

完整文件名见 [WEIGHTS.md](WEIGHTS.md)。

数据库中的人脸图片、接收目录、运行输出、日志、测试数据和 Python 缓存没有复制进来。

## 运行前提

两个项目原本使用不同的 Conda/PyTorch 环境，建议分别安装各自的依赖文件，不要强行合并成一个环境。除了 Python 依赖外，服务端还需要 Redis、MySQL 客户端/服务、FFmpeg，以及单独部署的 MediaMTX。`depth.sh` 中的 PersonViT 程序不属于本次提取范围，未复制。

示例（在服务端 Linux 环境执行）：

```bash
cd legacy/RGBDSort
python rtsp_server.py
python RGBsort.py
python track_comsumer.py

cd ../AdaFace-master
python redis_comsumer.py
```

如果服务器有图形终端，也可以先复制并填写 `legacy/.env.example` 为
`legacy/.env`，再运行 `legacy/depth.sh`；它会用相对路径启动这两个项目的四个入口进程。MediaMTX 只有在设置 `MEDIAMTX_DIR` 后才会由该脚本启动。

启动前请通过环境变量设置数据库、Redis、RTSP/MediaMTX 和输出目录。没有数据库配置时，相关写库逻辑会跳过或失败记录，但不会把密码写入源码。详细变量见各项目源码顶部的配置段。

## GitHub 提交

从 `invisiscan-ai` 仓库根目录执行 `git add` 时，权重和运行生成物会被忽略；源码、依赖说明和目录占位文件可以正常提交。权重只保留在本地工作区，重新克隆仓库后需要按项目中的权重清单重新放置。
