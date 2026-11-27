# 架构设计：实时人员进出感知链路（重构版）

本文描述新项目 `invisiscan-ai` 的架构。它承接旧链路（`RGBDSort` + `AdaFace-master`，四个 Python 进程 + Redis + 共享文件系统）的全部业务能力，但重新划分了进程边界、数据通道和存储模型。

设计目标按优先级排列：链路语义正确（尤其是时间）、单点故障可恢复、GPU 推理可独立扩容、数据可查询可追溯。性能是约束而非目标——输入为单路 2560×1440、8 fps，不需要为高吞吐做妥协设计。

## 1. 旧链路实际做了什么

重构前先把既有行为固定下来，作为验收基线。旧链路由四个进程串成：

`rtsp_server.py` 打开本地摄像头（2560×1440，`CAP_PROP_FPS` 8），逆时针旋转 90° 得到 1440×2560 的竖画幅，把 `%Y-%m-%d %H:%M:%S` 格式的时间文本用 `cv2.putText` 绘制在画面左上角。随后分两路：一路把带时间戳的帧写成 JPG，按 `frames/%Y-%m-%d_%H/` 小时桶存放，整点过后用 ffmpeg 的 concat demuxer 按真实帧间隔合成 1 小时 mp4（720×1280、h264_nvenc、1 Mbps），写一条 `videos` 记录并保留最近 720 个切片；另一路把裸 BGR 帧管道喂给 ffmpeg，以 libx264 zerolatency 推到 MediaMTX 的 RTSP 端点。

`RGBsort.py` 从 MediaMTX 把流拉回来，对每帧左上角做模板匹配，逐字符把时间戳**从像素里解码回 datetime**（`sort.py` 中的 `TIMESTAMP_*` 常量与解码函数），作为该帧的权威时间。然后 YOLO 检测行人、VideoDepthAnything 估计深度、按 ROI 分位数算出深度门限做候选过滤和 depth-aware NMS，再交给 ultralytics 8.3.229 的 `BOTSORT` 跟踪（GMC 与外观特征细节见第 5 节；深度只用于跟踪前的过滤和 NMS，不参与关联）。推理前画面按长边缩到 960，跟踪在缩放后的坐标系里进行，落盘时再换算回原分辨率。业务状态机在这一层：过滤后的检测框数由 0 变正即进入 event（此时只在内存里），连续 `MAX_CONSECUTIVE_NO_PERSON = 20` 帧检测框数为 0 则结束 event；轨迹被跟踪器输出满 `MIN_TRACK_FRAMES = 3` 帧才算确认，事件内第一条轨迹确认时才 INSERT `events`，之后每有更早的确认轨迹就反过来修正 event 的 `start_time`。每条轨迹对应 `output/<日期>/<时间>/unclasses/<时间戳_IDn>/` 目录，里面 `body/` 放人体裁剪图、同名 txt 每帧追加一行 `x,y,w,h,depth,orig_h,orig_w`。其中框是跟踪器输出的卡尔曼平滑框（不是检测框），depth 是该轨迹的深度 EMA（α=0.8），从未取到有效深度时写 0。事件期间每帧原图还会写进 `pics/`，事件结束时若只有 1 人、没有 indoor 轨迹或超过 800 张就删除。轨迹消失时，若帧数不足就删库删目录，否则写 `end_time` 并把目录路径 push 到 Redis `missing_tasks`。

`track_comsumer.py` 消费 `missing_tasks`，读 txt 跑双向 LSTM 得到 `indoor` / `outdoor` / `pass` 分类（少于 5 行直接丢弃；预处理后不足 15 步直接判 `pass`、置信度 0；最大概率低于 0.3 也判 `pass`），把目录从 `unclasses/` 移到分类子目录，更新 `person_tracks.category` 和 `image_path`，对所有类别都用 `yolo11m-seg`（conf 0.8）抠出人体掩码，indoor 再用 MTCNN 从分割图里对齐人脸，然后按分类分别 push 到 `adaface_master` 或 `outdoor_master`。

`redis_comsumer.py` 消费 `adaface_master`。它启动时把 `pictures` 表里所有人脸图 BLOB 读出来，逐张算 AdaFace ir_50 特征，连同身份信息存成两个并行的内存列表；运行期靠 `COUNT(*) + MAX(updated_at)` 组成的版本串轮询做增量同步，另外每 6 小时全量重载一次。识别时把轨迹的 `face/` 目录下所有人脸图算特征，得分是"查询图对每张底库模板的相似度取平均，再在模板间取最大"（`compute_user`：`sum(Q @ T.T, dim=0).max() / len(Q)`），按模板而不是按人取最大；得分 `<= tolerance = 0.35` 就先跑关键点 + DSL-FIQA 质量评估，只取质量最高的 1 张图再比一次；仍然 `< 0.349` 就判定为陌生人，生成 `陌生人<uuid>` 的名字入 `pictures` 表（质量分低于 0.15 则不入库），这条日志本身记 `unknown`、不关联新建的图。`face/` 目录不存在时（MTCNN 一张脸都没对齐出来）写一条 `username` 为空、分数 0 的日志；目录存在但特征全部提取失败时走 `unknown` 分支。最后写一条 `logs`（`occurred_at` 取轨迹首帧时间，晚于 08:40:00 标注"迟到"），按配置调用 Fay 的 `GET /face/hello?user=<名字>` 通知，并从 `body/` 按文件名排序后去掉前 3 张和后 2 张（代码是 `[3:-2]`，注释写的"前后各 20 张"与代码不符，以代码为准），再均匀采样 6 张图存到按日期和 log id 组织的输出目录。`flask_server.py` 是另一个不在 `depth.sh` 启动列表里的 HTTP 入口，不属于这条链路。

`outdoor_master` 队列在当前代码副本里没有消费者——它原本由 `depth.sh` 启动的 PersonViT ReID 程序消费，该程序不在本次提取范围内。

## 2. 需要重构的结构性问题

**帧时间走像素通道。** 采集端把时间烧进画面，消费端再用模板匹配 OCR 回来。整条链路的时间正确性依赖于编码质量、画面裁剪位置和字符模板匹配分数，解码失败时代码只能退化成"用当前墙钟时间，事后再 backfill 修正 `start_time`"。`sort.py` 里 `track_start_needs_backfill`、`get_backfilled_track_start_time`、`update_event_start_time` 这一整套补偿逻辑都是为了弥补这个通道的不可靠。新设计必须让时间走带内元数据，而不是走像素。

**SQL 字符串拼接 + 调用 mysql CLI。** `sort.py` 和 `rtsp_server.py` 通过 `subprocess.run([mysql, "-e", sql])` 执行 f-string 拼出来的 SQL，`sql_escape` 只处理反斜杠和单引号。每条语句都是一次进程启动加一次 TCP 握手，没有连接池、没有事务、没有预编译，`LAST_INSERT_ID()` 还要靠解析 stdout 最后一行取回。这既是正确性问题也是注入面。

**跨进程通道传本地绝对路径。** Redis 消息体是 `{"event_dir": "/abs/path", "person_dir": "..."}`，下游直接 `os.listdir` 和 `shutil.move`。四个进程因此被钉死在同一台主机的同一块磁盘上，无法把 GPU 推理拆到别的机器，也无法多副本消费（两个消费者会互相移动对方的目录）。

**队列无投递保证。** `BLPOP` 取出消息后若进程崩溃，消息就丢了，而对应的轨迹目录仍留在 `unclasses/` 下，没有重试也没有死信。

**跟踪与事件状态只存在于单进程内存。** `current_ids`、`track_db_id`、`track_started_at`、`in_event` 等十余个 dict 是进程内变量，`RGBsort.py` 一崩，正在进行的 event 的 `end_time` 永远是 NULL，正在跟踪的轨迹变成孤儿记录。

**轨迹序列存在文本文件里。** 每条轨迹的运动序列是 `<person_dir>.txt`，LSTM 直接读这个文件。轨迹数据因此无法查询、无法重算、无法用于训练集回溯，且与分类结果不在同一个事务边界内。

**人脸底库是关系库里的图片 BLOB。** `pictures.face_image` 存 JPG 二进制，每次启动全量取出逐张前向推理重算特征。底库上千张时启动就要跑上千次 GPU 推理，而且比对是 Python 列表上的线性遍历，没有索引。版本号轮询（`COUNT(*)` + `MAX(updated_at)`，还带一个无 `updated_at` 列时的 CRC32 回退分支）本质上是在数据库上模拟变更订阅。

**数据模型的冗余与回退。** `logs` 同时存 `date`、`time`、`occurred_at` 三个字段表达同一时刻；展示名靠 `COALESCE(users.name, pictures.name)` 回退，意味着 `pictures` 既是人脸图表又兼作临时身份表；"迟到"是写入时固化进 `note` 列的业务判断，阈值一改历史数据就不一致。

## 3. 新拓扑与职责边界

```
Host A (local GPU host, no public IP)
┌────────────┐ v4l2 ┌─────────────┐ RTSP  ┌──────────┐ RTSP  ┌──────────────────────────┐
│ USB camera │─────>│ ffmpeg      │──────>│ MediaMTX │──────>│ pipeline (Go)            │
└────────────┘      │ publisher   │publish│ + record │ read  │ decode → perceive (gRPC) │
                    │ (runOnInit) │       └────┬─────┘       │ → BOTSORT → event FSM    │
                    └─────────────┘            │ fmp4        │ → persist → post-track   │
                                               v segments    └──┬──────────┬────────┬───┘
                                        ┌─────────────┐         │ gRPC     │ SQL    │ HTTPS
                                        │ local disk  │         v          v        │ previews
                                        │ (BlobStore) │  ┌────────────┐ ┌────────┐  │
                                        └─────────────┘  │ infer (Py) │ │ PG (A) │  │
                                                         │ per-stream │ │ +pgvec │  │
                                                         │ session    │ └───┬────┘  │
                                                         └────────────┘     │       │
                                                  logical replication (both ways)   │
==================== WireGuard tunnel (A dials out) ========================│=======│====
Cloud server                                                                │       │
┌─────────────────┐  SQL  ┌─────────────┐                                   │       │
│ invisiscan-main │──────>│ PG (cloud)  │<──────────────────────────────────┘       │
│ API / frontend  │       │ + pgvector  │                                           │
└────────┬────────┘       └─────────────┘                                           v
         │ internal network                                             ┌──────────────────┐
         └─────────────────────────────────────────────────────────────>│ cloud object     │
                                                                        │ storage (S3 API) │
                                                                        └──────────────────┘
```

图中只有逻辑复制走 WireGuard 隧道。预览图由 A 经公网 HTTPS 直接上传到云厂商对象存储，不走隧道，因为对象存储不在云服务器上。日志、指标和录像代理也走隧道，图中没有画出，见 `IMPLEMENTATION_PLAN.md` 4.1 节。

**采集端（ffmpeg publisher）。** 旧链路读的是本地 USB 摄像头（`RGBDSORT_VIDEO_URL=0`），所以不存在"摄像头直推 RTSP"。新链路用一条 ffmpeg 命令替代 `rtsp_server.py` 的推流部分：`-f v4l2 -framerate 8 -video_size 2560x1440 -i /dev/video0 -vf transpose=2 -c:v libx264 -preset ultrafast -tune zerolatency -g 8 -f rtsp rtsp://127.0.0.1:8554/live`。`transpose=2` 等价于旧代码的 `cv2.ROTATE_90_COUNTERCLOCKWISE`。2560×1440@8 在 USB 上通常要求 MJPEG 输入（`-input_format mjpeg`），部署前用 `v4l2-ctl --list-formats-ext` 确认。这条命令挂在 MediaMTX 的 `runOnInit` + `runOnInitRestart: true` 下，由 MediaMTX 负责拉起和崩溃重启，不再需要单独的 Python 进程和 gnome-terminal。画面上不再烧时间戳。

**MediaMTX** 的职责从"中转站"扩大为"采集与归档的统一入口"。它负责 `record` 配置下的分段录制（`recordFormat: fmp4`，`recordSegmentDuration: 1h`），替掉旧链路"存 JPG → 整点 ffmpeg concat"的整套逻辑。旧链路保留最近 720 个 1 小时切片；新链路的录像只存在 A 本地磁盘、不上云，保留 4 天，对应 `recordDeleteAfter: 96h`（MediaMTX 默认只保留 1d，必须显式改）。切片完成后 MediaMTX 执行 `runOnRecordSegmentComplete` 命令（它是 shell 命令，不是 HTTP 回调），命令里用 curl 把 `MTX_PATH`、`MTX_SEGMENT_PATH`、`MTX_SEGMENT_DURATION` POST 给 Go 的内部接口，由 Go 登记 `recordings`。回放可以直接用 MediaMTX 自带的 playback server（`playback: true`）按绝对时间段取片段，不必自己拼文件。

存储量需要提前算：旧归档是 720×1280、1 Mbps，30 天约 324 GB；新链路直接录推流原码流（1440×2560），码率按 4 Mbps 算每天约 43 GB，4 天约 173 GB。实际码率在阶段二实测后再核算。如果磁盘不够，要么降低推流码率（推理侧会把画面缩到长边 960，降码率对检测影响有限，但要在阶段二实测），要么缩短保留期。

**pipeline（Go，单进程多 goroutine）** 承担全部有状态逻辑：

- *ingest*：用 `gortsplib`（MediaMTX 作者维护的 Go RTSP 库）拉流，按 RTP 时间戳和 RTCP SR 换算每帧墙钟时间（见第 4 节）。H.264 解码走 cgo 绑定的 libavcodec，这也是 gortsplib 官方示例的做法；想规避 cgo 时退化为 `ffmpeg -f rawvideo` 子进程，帧时间仍由 gortsplib 侧确定，两路按 PTS 对齐。
- *schedule*：单槽"最新帧"缓冲，推理跟不上时丢旧帧而不是积压。旧链路的队列容量是 1000，实际上会积压到几十秒延迟；新链路明确取舍为低延迟，丢帧数计入指标。
- *perceive client*：把整帧 BGR 原图（1440×2560，约 11 MB）发给 Python 感知服务的流式会话，拿回过滤后的检测框、深度、外观特征和 GMC 仿射矩阵。缩放到长边 960 放在 Python 侧用 `cv2.resize(INTER_LINEAR)` 做，保证与旧链路逐像素一致；本机 gRPC 每秒约 90 MB，可以接受，gRPC 最大消息长度需调到 16 MB。gRPC 地址同时支持 TCP 和 Unix domain socket，由配置决定：Go 客户端写 `unix:///run/invisiscan/perception.sock` 或 `127.0.0.1:50051` 都可以，代码里不区分；Python 服务端调用两次 `add_insecure_port`，同时监听 socket 和 TCP 端口。默认部署走 socket，调试时用 TCP 地址（grpcurl、宿主机直接运行 Python 服务）。TCP 端口只监听 127.0.0.1 或 WireGuard 地址。
- *track*：Go 移植的 BOTSORT（ultralytics 8.3.229 版本语义），只做纯数学部分：卡尔曼（`KalmanFilterXYWH`）、IoU、`fuse_score`、外观余弦距离、两阶段关联、`remove_duplicate_stracks`。依赖图像的两块（GMC 光流、ReID 特征提取）由 Python 算好随检测结果返回，Go 侧只应用矩阵和比特征。这样跟踪器的输入是纯数据，可以离线录制回放、做逐帧回归。
- *event FSM*：事件进入/结束、轨迹确认（`min_track_frames`）、事件起止时间推导。
- *persist*：轨迹点批量写 A 本地库、事件与轨迹状态落库、人体裁剪图（从原分辨率帧上裁）JPEG 编码后写 BlobStore。质量取 95，与 `cv2.imwrite` 默认一致；Go 标准库编码器与 libjpeg 的输出不逐字节相同，下游分割和人脸对齐的结果可能有微小差异，阶段四回归时需要留意。
- *post-track workers*：轨迹结束后的分类、分割、人脸识别编排，以及预览图的压缩上传。

**infer（Python）** 拆成两个 gRPC 服务，每个服务一个容器，依赖用 uv 生成带哈希的锁文件。RGBDSort 锁定 `torch==2.1.1` + `ultralytics==8.3.229`。AdaFace 运行链路用的 `requirements-runtime.txt` 对 torch 没有版本约束（`torch<=1.13.1` 只出现在留作参考和训练环境的 `requirements.txt` 里），所以依赖冲突不是拆分的理由。仍然拆分，是因为 FaceService 无状态，可以单独扩副本，人脸模型也可以单独升级。FaceService 的锁文件必须写死 torch 版本。

- `PerceptionService`（依赖与 RGBDSort 一致）：一个双向流 `Perceive`，每路摄像头一个流。会话内保有 VideoDepthAnything 的时序窗口和 GMC 的上一帧，这两样本来就是按流有状态的，所以服务明确设计为"按流有状态、跨流无状态"。每帧返回：经 ROI 深度门限过滤和 depth-aware NMS 后的检测框（推理坐标系）、每框深度（IQR 中位数）、每框 ReID 特征、本帧 GMC 2×3 矩阵、ROI 深度门限值（调试用）。另外提供一元接口 `ClassifyTrajectory`（LSTM）和 `SegmentBodies`（yolo11m-seg，取面积最大的掩码，抠图后返回）。
- `FaceService`（依赖与 AdaFace 一致）：`AlignFaces`（MTCNN）、`EmbedFaces`（AdaFace ir_50，返回 L2 归一化的 512 维特征）、`AssessQuality`（关键点 + DSL-FIQA）。全部无状态，可以多副本。

Go 侧 `TaskQueue`/客户端都按"一个摄像头流固定连一个 PerceptionService 副本"来路由。单路摄像头的部署里只有一个副本，这条约束不产生额外成本；它的作用是防止以后横向扩容时把同一路流打散到多个副本，静默降低深度精度。流断开重连时会话重建，前几帧深度窗口不完整，这与旧链路进程重启时的行为相同。

**PostgreSQL 16 + pgvector，A 本地库加云端主库。** A 本地运行一个 PostgreSQL，作为实时链路的状态库：pipeline、post-track worker、reconcile、相机锁、任务认领只访问这个本地库，延迟和可用性不受广域网影响。云端 PostgreSQL 是业务主库，invisiscan-main 只访问云端库。两边执行同一套迁移，用 PostgreSQL 原生逻辑复制同步，订阅端都通过 WireGuard 隧道连接发布端。A 没有公网 IP，隧道由 A 主动连接云服务器。每张表只有一个写入方（`subjects`、`face_templates` 两边都插入，但更新只归一方），见 `DATA_MODEL.md`"双库与写入方"和 `IMPLEMENTATION_PLAN.md` 4.2 节。

人脸特征以 `vector(512)` 存在 `face_embeddings` 表，由 A 在注册时算一次，取消启动时全量重算和版本号轮询。识别时每条轨迹读一次 A 本地库里的当前底库（见第 7 节打分规则），底库变更复制到 A 后，对下一次识别立即可见。

**A 内部不用 Redis。** 旧链路用 Redis 做进程间任务队列，而在新拓扑里"轨迹结束 → 分类 → 人脸识别"全部发生在 Go 进程内部的 goroutine 之间，用 channel 和数据库状态机即可。任务状态必须和轨迹状态在同一个事务里更新，所以任务队列放在 A 本地库：`TaskQueue` 接口的默认实现是 `tracks` 表上的任务认领（`SELECT ... FOR UPDATE SKIP LOCKED`），channel 只做唤醒信号，启动时按第 5 节的 reconcile 规则扫描非终态轨迹，恢复未完成任务。云端的 invisiscan-main 可以用 Redis（多实例广播实时推送、缓存、限流、会话存储），但它不参与 A 与云端之间的数据同步。

## 4. 帧时间：取消像素通道

这是重构里收益最大的一处改动，单独说明。

新方案中，采集端不再在画面上绘制时间，消费端也不再做模板匹配。每帧的时间由 Go 的解码层确定：

1. MediaMTX 保持默认 `useAbsoluteTimestamp: false`。这时它用自己收到帧时的系统时间作为该帧的绝对时间，并通过周期性 RTCP sender report（约每 10 秒一次）发给读端。Go 侧用 gortsplib 的 `PacketNTP` 按 `ref_ntp + (rtp_ts - ref_rtp) / 90000` 换算每帧时间。这个时间是"MediaMTX 接收时刻"，比旧链路烧屏的"采集时刻"晚一个编码加本机传输的延迟（ultrafast + zerolatency 下通常在 100 ms 量级，阶段二实测并写进文档）。旧链路的 OCR 时间只有秒级精度，所以新时间在精度上仍然更好，只是带一个固定的小偏移。
2. 不开 `useAbsoluteTimestamp: true`。开启后时间来自发布端，而 ffmpeg 发布端不保证发出带墙钟的 SR，MediaMTX issue #5355 记录了开启后因收不到 SR 丢掉全部 RTP 包的情况。
3. 拉流刚建立、第一个 SR 到达之前的帧没有绝对时间。处理方式是缓存这几秒内的 RTP 时间戳，SR 到达后统一回填再送入推理；为了简单也可以直接丢弃前 10 秒，启动时不会有人在画面里的概率很高。每帧记录 `clock_source`（`rtcp` / `fallback_local`），fallback 只用于 SR 长时间缺失的异常情况，并告警。
4. 时间水印如果回放时仍需要，在 MediaMTX playback 的前端或回放播放器上叠加，不进推理链路。

随之可以删掉的逻辑：`sort.py` 里的 `TIMESTAMP_*` 常量族与逐字符模板匹配解码、`frame_time_ocr_ok` 判定、`track_start_needs_backfill` 标记、`get_backfilled_track_start_time` 的秒级回退计算、`update_event_start_time` / `update_person_track_start_time` 这两个纯补偿性的写库路径。事件和轨迹的 `start_time` 一次写入即为最终值。

所有时间在库内统一为 `timestamptz`，应用层统一用 UTC 处理、展示层按 `Asia/Shanghai` 渲染。旧链路的 `date` / `time` 拆分列不再存储，需要按日聚合时用生成列或查询时 `date_trunc`。

## 5. 状态机与崩溃恢复

旧链路的事件与轨迹状态只存在于内存，进程重启后留下 `end_time IS NULL` 的孤儿记录。新设计把状态显式建模并落库。

**事件（event）** 的生命周期：`pending → open → closed`。语义与旧链路逐条对齐。过滤后检测框数由 0 变正时进入 `pending`，只在内存里。事件内第一条轨迹确认时才落库为 `open`，`started_at` 取该轨迹的首帧时间，之后若有更早开始的轨迹确认，再把 `started_at` 往前改。这次改写是业务语义本身要求的，不是时间补偿，和第 4 节删掉的 backfill 不是一回事。连续 `max_absent_frames = 20` 帧检测框数为 0 时关闭，`ended_at` 取事件内确认轨迹的最晚出现时间（旧链路 `event_last_confirmed_track_end_at`），没有确认轨迹的 `pending` 事件直接丢弃，不落库。事件关闭时，跨在事件外仍在跟踪的轨迹，`ended_at` 被截到事件结束时间（旧链路 `track_event_end_cap`）。

注意"开事件"和"关事件"看的是检测框数，不是确认轨迹数。这是旧链路的实际行为：1~2 帧的误检不会让事件落库，但会推迟事件关闭。重构保留这个口径，阶段三回归时按同一口径比对；以后要改成按确认轨迹判定，需要作为算法变更单独评估。

**跟踪器语义（Go 移植时必须保持）。** 旧参数是 `track_high_thresh = track_low_thresh = new_track_thresh = 0.1`、`match_thresh = 0.8`、`track_buffer = 18`、`frame_rate = 15`（取的是 `--display_fps`，不是真实的 8 fps），因此 `max_time_lost = int(15/30*18) = 9` 帧，在 8 fps 下约 1.1 秒。high 和 low 阈值相同，第二阶段低分关联实际上永远是空集。检测置信度阈值 `rgb_conf = 0.05` 低于跟踪阈值 0.1，所以 0.05~0.1 的框会计入事件开关判断的检测框数，但不进入跟踪。`with_reid = True` 且 `model` 指向 `yolo26m-seg.pt`，所以外观特征来自检测模型倒数第二层的 embedding，对每个检测框的裁剪图单独前向。GMC 是 `sparseOptFlow`。新轨迹在下一帧被关联上才 `is_activated`（第 1 帧除外），只有 activated 的轨迹才会被输出，"轨迹帧数"计的是被输出的帧数。输出框是卡尔曼更新后的状态框。深度参数 `depth_gate`、`depth_lambda`、`w_app`、`app_gate` 在 `RGBsort.py` 里解析了但没有传给 BOTSORT，属于死参数，不移植。`sort.py` 里带深度门控的 `byte_tracker_depth.py` 不在运行链路上，也不移植；如果论文需要比较深度门控，可以把它作为 Go 跟踪器的可选关联项后加，但默认关闭，保证基线一致。

匈牙利匹配旧链路用 `lap.lapjv(extend_cost=True, cost_limit=thresh)`。Go 侧需要实现同语义的 LAPJV，代价相等时的选择顺序可能和 C 实现不同，导致极少数 ID 分配差异。回归测试允许这类差异，但每一处都要能定位到相等代价。

**轨迹（track）** 的生命周期：

```
tentative ──(输出满 3 帧)──> confirmed ──(跟踪器 removed)──> ended
    │                                                        │
    └──(不足 3 帧即消失) 丢弃，不落库          ┌──────────────┼──────────────┐
                                        点数 < 5         分类完成
                                           │                 │
                                       discarded        classified
                                                   ┌─────────┼─────────┐
                                                indoor    outdoor     pass
                                                   │         │         │
                                              identified pending_reid  done
```

`tentative` 阶段不写库，轨迹点和裁剪图在内存里累积；输出满 `min_track_frames` 帧时在一个事务里 INSERT 轨迹、补写前几帧的 `track_points`，需要时同时 INSERT 事件。这样就不需要旧链路"先插入再按帧数不足删除"（`delete_person_track_from_db`），也不需要 `update_person_track_event_id` 那样的事后补外键。`started_at` 仍然是第一次输出的帧时间，与旧链路一致。

旧链路里点数不足 5 的轨迹会被 `track_comsumer.py` 删掉目录，但 `person_tracks` 行留着、`category` 为 NULL，无法和"还没处理"区分。新链路明确标记 `discarded`。

**恢复逻辑。** 进程启动时执行一次 reconcile，规则如下：

| 残留状态 | 处理 |
|---|---|
| 事件 `open` | `ended_at` 取事件内轨迹点的最晚时间，`status = 'closed'`，`closed_reason = 'recovered'` |
| 轨迹 `confirmed` | `ended_at` 取该轨迹最后一个点的时间，`status = 'ended'`，`end_reason = 'recovered'`，然后照常投入分类 |
| 轨迹 `ended` | 重新投入分类 |
| 轨迹 `classified` 且 indoor | 重新投入识别 |
| 轨迹 `pending_reid` | ReID 启用时重新投递，否则不动 |

被恢复的轨迹是截断的，LSTM 分类结果可能偏。保留 `end_reason` 是为了在统计和论文实验里能把它们筛掉。

**幂等。** `recognition_logs` 上建 `UNIQUE (track_id, modality)`，识别写入用 `INSERT ... ON CONFLICT DO NOTHING`；分类写入是 `UPDATE ... WHERE status = 'ended'` 的条件更新。自动注册陌生人和写识别日志放在同一个事务里，重复投递不会多建陌生人。

## 6. 图像产物的存储

旧链路把图像组织成一棵语义目录树，分类结果通过 `shutil.move` 在目录间移动来表达（`unclasses/` → `indoor/`），下游靠拼路径找文件。这让文件系统布局成了隐式的数据模型，且与数据库状态可能不一致（移动成功但更新失败）。

新设计反过来：**路径不携带语义，语义只在数据库里。**

对象键按内容类型和不可变标识组织，一次写入后永不移动：

```
tracks/{track_uuid}/body/{seq:05d}.jpg      人体裁剪（原分辨率帧上裁）
tracks/{track_uuid}/seg/{seq:05d}.jpg       分割抠图（最大掩码外置黑，与旧 segs/ 相同）
tracks/{track_uuid}/face/{seq:05d}.jpg      对齐人脸 112×112，seq 与来源 body 图一致
subjects/{subject_uuid}/{template_uuid}.jpg 底库模板原图
previews/tracks/{track_uuid}/{seq:05d}.jpg  上传到云端的压缩预览图，seq 与来源 body 图一致
recordings/...                              MediaMTX 录制切片，路径由 recordPath 决定
```

旧链路事件期间每帧原图写进 `pics/` 的做法不再保留，MediaMTX 已经在录整段视频，回放事件用 `recordings` 加时间偏移即可。

存储后端抽象为 `BlobStore` 接口，两种实现：本地文件系统（A 上的原图和录像）和云厂商的对象存储（通过 S3 兼容接口访问，用 aws-sdk-go-v2，换厂商只改 endpoint 和凭证）。数据库只存对象键，不存绝对路径，这样换存储、迁机器、改挂载点都不影响历史数据。

只有压缩图上传到云端。`tracks/` 下的原图（质量 95）只留在 A，供分割、人脸对齐和回归测试使用；每条非 `discarded` 的轨迹选出最多 10 张 body 图，长边缩到 512 像素、JPEG 质量 75，上传到 `previews/`，对象键记在 `track_images.preview_key`，上传成功后写 `uploaded_at`。`seg` 和 `face` 图不上传。例外是自动注册陌生人时的模板人脸图（112×112，几 KB），原样上传到 `subjects/`，供 invisiscan-main 的"待确认陌生人"界面显示；invisiscan-main 上传的模板原图也在 `subjects/`，A 计算特征时从对象存储下载。

旧链路的三类"文件即数据"用法相应改掉：轨迹运动序列从 txt 改为 `track_points` 表的行；事件内的逐帧框标注从 `event<日期><时间>.txt` 改为同一张表的查询结果；`body/` 均匀采样 6 张图的输出目录改为在 `track_images.selected` 上标记被选中的图（按时间等分取最多 10 张，规则见第 7 节），不复制文件。

## 7. 链路时序

**实时路径**（每帧，Go 主循环）：

```
gortsplib frame (rtp_ts → frame_time, clock_source)
  → 最新帧槽（推理忙则覆盖）
  → Perceive stream: send FrameRequest(bgr 原图, frame_time)
      Python: resize 长边 960 → VDA 深度 → ROI 分位数门限
              → YOLO26m-seg(conf 0.05) → 深度过滤 + depth-aware NMS
              → 每框 IQR 深度 → 每框 ReID 特征 → GMC 矩阵
  ← FrameResult(detections[], gmc, gate)
  → event FSM: 用 len(detections) 开 / 关事件
  → BOTSORT.Update(detections, gmc)  → 输出 activated 轨迹
  → for each output track:
        深度 EMA(α=0.8)，框换算回原分辨率
        tentative: 内存累积点 + 裁剪图
        第 3 帧:   事务 INSERT [event] + track + 已累积点
        confirmed: 缓冲 track_point，裁剪图交 worker pool
  → for each removed track:
        tentative → 丢弃；confirmed → ended_at = 最后输出时间（受事件截断），投递 post-track
  → 每 ~1s 或 ~200 行批量 COPY track_points
```

一个流上同一时刻只有一个在途帧，Go 拿到结果才发下一帧，这样 Python 会话内的时序状态天然有序。裁剪图编码和写存储走独立 worker pool，不阻塞主循环（对应旧链路的 `save_crop_async`）。

**轨迹结束后的异步路径**（Go worker，每条轨迹一次）：

```
track ended
  → 读 track_points；点数 < 5 → status='discarded'，结束
  → Perception.ClassifyTrajectory(points, frame_w, frame_h, norm 参数)
        Python 内完成旧 preprocess_xywhd_lines：中心点归一化、深度 EMA、
        p1/p99 归一化、差分与裁剪；步数 < 15 或最大概率 < 0.3 → pass
  → 所有类别: Perception.SegmentBodies(body 图) → 写 seg/（旧链路对 pass 也做）
  → UPDATE tracks SET category, probs, status = 
        indoor → 'classified' / outdoor → 'pending_reid' / pass → 'done'
    WHERE status = 'ended'
    （分割在状态推进之前做，崩溃重放时整段从分类重来，seg 图按 seq 覆盖写）
  → indoor:
        Face.AlignFaces(seg 图)                → 对齐人脸，写 face/
        没有任何人脸 → 写 decision='no_face' 的日志
        Face.EmbedFaces(faces) → Q[n×512]
        第一轮打分（见下）
        score <= 0.35 → Face.AssessQuality(faces) → 取质量最高 1 张的重对齐图
                      → EmbedFaces → 第二轮打分
        仍 < 0.349   → 质量 >= 0.15 则注册 unknown subject + template
                      → decision = enrolled 或 unmatched
        事务：INSERT recognition_log [+ subject + template + face_embedding]，status='identified'
  → outdoor: 已是 pending_reid，等待 ReID（见第 8 节）
  → pass:    已是 done，终态
```

打分规则保持旧 `compute_user` 的语义：对每张底库模板 `t`，算 `mean_i(q_i · t)`，取最大的模板作为结果。这个口径无法直接用 pgvector 的单向量 ANN 表达（ANN 只能对单个查询向量排序）。底库规模在千张以内时，Go 侧做法是一次 SQL 从 A 本地库取出全部可用模板在当前模型下 `status = 'ready'` 的特征（1000×512 float32 约 2 MB），在内存里算矩阵乘。底库变化后下一次识别就会读到，所以不需要缓存和版本轮询。不建 pgvector 的 HNSW 索引：系统面向 50 到 100 人，底库最多千级，底库查重直接全表扫描也只要毫秒级；以后确实需要单向量检索，再在 `face_embeddings` 上补建。如果以后想改成"先对查询特征求均值再 ANN"，数学上和旧口径等价（`mean_i(q_i·t) = (mean_i q_i)·t`），可以把计算下推到数据库，只是均值向量不再是单位向量，距离阈值要按未归一化的内积来设，用 `<#>` 而不是 `<=>`。这个等价关系让两种实现可以互相校验。

旧代码里两个阈值是 `<= 0.35` 和 `< 0.349`，新代码统一写成配置项 `match_threshold = 0.35`、`enroll_threshold = 0.349`，保持旧行为。陌生人注册所用的图是第二轮里质量最高的那张，与旧 `find_best_face` 一致。

识别日志的 `occurred_at` 取轨迹 `started_at`。

旧链路从 `body/` 去掉前 3 张、后 2 张再均匀采样 6 张，新链路改为按时间等分取最多 10 张：在轨迹的 `started_at` 到 `ended_at` 之间取 10 个时间点 `t_k = started_at + k × (ended_at − started_at) / 9`（k = 0…9，含首尾），每个时间点取 `frame_time` 最接近的 body 图并去重，不足 10 张就全取。按时间而不是按序号等分，是因为推理跟不上时会丢帧（第 3 节的最新帧槽），序号和时间不一定均匀对应。选中的图在 `track_images.selected` 上打标记，压缩后作为预览图上传（第 6 节）。旧规则去掉首尾，是因为轨迹刚出现和快消失时常常只拍到半个人；如果首尾图质量不好，可以改成去掉首尾几张后再等分，只需要改选图函数。

**归档路径**（事件驱动）：MediaMTX 完成一个录制切片 → `runOnRecordSegmentComplete` 里的 curl 把路径和时长 POST 给 Go → Go 从文件名解析起始时间（`recordPath` 含 `%Y-%m-%d_%H-%M-%S-%f_%z`），登记 `recordings` 行。Go 启动时再扫描一遍录制目录，补登 Go 停机期间完成的切片，保证 hook 丢失时不漏记。回放时按时间区间查 `recordings` ∩ `events` 得到某次事件对应的视频片段与时间偏移。

## 8. ReID 扩展点（预留，不实现）

轨迹分类为 `outdoor`（出门）时需要做人体 ReID 来确认身份——旧链路把这类轨迹推到 Redis `outdoor_master` 队列，由 `depth.sh` 启动的 PersonViT 程序消费，该程序不在当前代码副本中。你手上暂时没有这部分代码，所以这里只定义契约和落点，保证将来接入时不需要改动既有结构。

预留的三处：

**gRPC 接口。** `proto/invisiscan/v1/reid.proto` 中定义 `ReIDService`，含 `ExtractBodyEmbedding`（输入分割抠图，输出 N 维特征与质量分）。PersonViT 的依赖环境未知，所以它是第三个独立服务，不并入前两个。Go 侧通过配置项 `reid.enabled` 开关；关闭时相关调用短路，轨迹停留在 `pending_reid` 状态。

**数据模型。** `body_templates` 表与 `face_templates` 结构平行：`subject_id` 外键、`embedding vector(N)`、`model_name`、`quality`、来源 `track_id`。`recognition_log.modality` 列区分本条识别来自 `face` 还是 `body`，让人脸识别和 ReID 共用同一张结果表而不是各建一套。维度 N 取决于最终选用的 ReID 模型（PersonViT 常见为 768），`model_name` + 独立表让多模型共存成为可能。

**处理阶段。** 轨迹状态机里 `ended → pending_reid → identified` 这条分支已经留好：outdoor 轨迹分类时直接写成 `pending_reid`，ReID 未启用就停在这里。将来启用时，启动 reconcile 会自动把历史积压的 `pending_reid` 轨迹重新投递，无需补数据脚本。

分割掩码（`save_segs` 产出）在旧链路里是为 ReID 和人脸对齐服务的，新设计中继续产出并存入 `tracks/{uuid}/seg/`，这样 ReID 接入时输入已经就绪。

## 9. 仓库结构

```
invisiscan-ai/
├── buf.yaml                    proto 模块、lint 与 breaking 规则
├── buf.gen.yaml                固定版本的 Go/Python 代码生成插件
├── go.mod / go.sum             Go 依赖锁定
├── sqlc.yaml                   DDL 与 Go 查询代码生成配置
├── Makefile                    检查、生成和开发环境命令
├── config/
│   └── dev.yaml                本地 pipeline 配置
├── docs/
│   ├── ARCHITECTURE.md          本文
│   ├── DATA_MODEL.md            ER 模型、字段语义、人脸底库迁移
│   ├── IMPLEMENTATION_PLAN.md   部署、复制与运维细节
│   └── IMPLEMENTATION_STAGES.md 分阶段实施、验收与待决问题
├── proto/invisiscan/v1/
│   ├── common.proto             Image、BBox 等共享消息
│   ├── perception.proto         PerceptionService（RGBDSort 环境）
│   ├── face.proto               FaceService（AdaFace 环境）
│   └── reid.proto               ReIDService（预留）
├── gen/go/invisiscan/v1/        从 proto 生成的 Go 消息与客户端
├── go/
│   ├── cmd/pipeline/main.go     进程入口、配置加载、优雅退出
│   ├── internal/config/         配置结构与校验
│   ├── internal/ingest/         gortsplib 拉流、解码、帧时间
│   ├── internal/infer/          gRPC 客户端（流式会话 + 一元调用）
│   ├── internal/track/          BOTSORT 移植：kalman / matching / lapjv / tracker
│   ├── internal/pipeline/       事件/轨迹状态机、post-track worker
│   ├── internal/recognize/      底库打分、陌生人注册
│   ├── internal/store/          pgx 仓储、sqlc 查询和生成代码、reconcile
│   ├── internal/blob/           BlobStore 接口 + 本地/对象存储实现
│   └── internal/api/            基础设施端点：录制切片登记、健康检查、/metrics
├── python/
│   ├── perception/              PerceptionService，uv 锁文件，依赖与 RGBDSort 一致
│   │   └── mock_server.py        阶段一的固定响应开发服务
│   ├── gen/invisiscan/v1/        从 proto 生成的 Python 消息与服务桩
│   ├── face/                    FaceService，uv 锁文件，写死 torch 版本
│   └── tools/                   旧库人脸底库迁移、跟踪回归录制脚本
├── legacy/                      旧链路代码，重构期间保持可运行
│   ├── RGBDSort/                rtsp_server / RGBsort / track_comsumer
│   ├── AdaFace-master/          redis_comsumer
│   ├── depth.sh                 旧四进程启动脚本
│   └── WEIGHTS.md               旧项目权重清单
├── migrations/                  编号 SQL 迁移（A 和云端都执行）
│   └── site/                    按站点执行：ID 序列错开、publication、subscription
├── testdata/                    回归用录像与旧链路输出（不进 Git）
├── deploy/
│   ├── dev/                      本地 PostgreSQL 双库与 Perception mock
│   ├── docker-compose.yml       A 上的 postgres / mediamtx / perception / face / pipeline / vector / prometheus
│   ├── mediamtx.yml             runOnInit 推流、record、segment hook
│   └── wireguard/               A 侧 WireGuard 客户端配置模板（密钥不进仓库）
└── Makefile                     proto 生成、构建、迁移、测试
```

Go 与两个 Python 服务各自独立构建，唯一的共享契约是 `proto/`；`make proto-gen` 生成各侧代码，`make sqlc-gen` 从迁移和查询生成 Go 数据访问代码。旧项目里的模型代码（`video_depth_anything/`、`LSTM/`、`face_alignment/`、`DSLFIQA/`、`net.py`）原样复制进对应 Python 服务，只改导入路径和权重路径，不改算法。模型权重不进仓库，沿用旧项目的做法通过配置指向本地路径，并在 `docs/` 下保留权重清单。

invisiscan-ai 不对外提供业务接口。论文演示和前端需要的事件、轨迹、识别日志和回放查询由 invisiscan-main 提供，它读云端库，清单见 `ADMIN_API.md`。`internal/api` 只保留基础设施端点，全部只监听本机或 WireGuard 地址：`POST /internal/recordings`（MediaMTX 回调，只监听 127.0.0.1，校验共享 token）、`/healthz`、`/readyz`、`/metrics`。

## 10. 分阶段实施

实施顺序、逐步验收、依赖、难点和待决问题见 [分阶段实施计划](IMPLEMENTATION_STAGES.md)。旧项目在重构期间保持原样、可运行，作为阶段二到四的回归基线。
