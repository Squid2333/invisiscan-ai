# invisiscan-ai 企业级实现方案（基于现有 docs）

依据：`docs/ARCHITECTURE.md`、`docs/DATA_MODEL.md`、`migrations/0001_init.sql`、`migrations/0002_seed.sql`、`proto/invisiscan/v1/*.proto` 和 `legacy/` 旧链路代码。编写日期 2026-11-26，同日按部署拓扑修订。

## 0. 定位

本方案不推翻现有设计。`ARCHITECTURE.md` 的拓扑、帧时间方案、状态机、存储模型和六个实施阶段全部作为主线保留。本文做三件事：把技术栈落到具体的库和版本策略；补上企业级系统需要、但现有 docs 没覆盖的部分；指出现有 docs 和 DDL 需要修改的具体位置。项目不商用，许可证和法规合规不在本方案范围内。

系统分三个项目：

| 项目 | 运行位置 | 职责 |
|---|---|---|
| invisiscan-ai（本项目） | 主机 A：本地 GPU 主机，没有公网 IP | 拉流、推理、跟踪、事件与轨迹状态机、分类、人脸识别、录像归档。不对外提供接口 |
| invisiscan-main | 云服务器 | 业务 API、用户认证、审计、前端。清单和契约见 `ADMIN_API.md` |
| invisiscan-agent（后期） | 云服务器 | 自动运维，读取数据库里的日志、心跳和系统事件 |

docs 的隐含前提是"单台主机、数据库在本机"。现在业务数据库放在云上，A 与云之间是一条可能中断的广域网链路，这是相对 docs 最大的变化，见第 4 节。其余增量是多实例下的 reconcile 与任务认领（第 3 节）和生产运维（第 5 节）。

## 1. 沿用的 docs 决策

以下决策直接沿用，实现时以 docs 为准：帧时间走 RTCP SR 而不是像素 OCR（ARCHITECTURE §4）；事件与轨迹状态机落库、启动时 reconcile（§5）；`recognition_logs UNIQUE (track_id, modality)` 加条件更新实现幂等（§5）；对象键不携带语义、`BlobStore` 抽象（§6）；Go 承担全部有状态逻辑、Python 只做推理（§3）；PerceptionService 与 FaceService 按依赖环境拆分（§3）；识别打分沿用 `compute_user` 口径、Go 内存矩阵乘（§7）；ReID 只留契约（§8）；阶段一到阶段六的顺序与验收方式（§10）。

有一处 docs 结论按新拓扑重新评估：ARCHITECTURE §3 认为"不再需要 Redis"。在 A 上这个结论仍然成立，因为 post-track 任务状态必须和轨迹状态在同一个事务里更新，用 A 本地的 PostgreSQL 做任务队列最简单。但云端有适合用 Redis 的场景，见 4.9 节。

## 2. 技术栈

docs 已经定下语言和中间件，这里补到库和工具一级。版本策略统一为"大版本写进文档，小版本由锁文件固定"。

| 层 | 选型 | 说明 |
|---|---|---|
| 采集与归档 | MediaMTX + ffmpeg，运行在 A | 沿用 ARCHITECTURE §3 的单流录制。录像存 A 本地磁盘，保留 4 天，不上云 |
| pipeline | Go，`go.mod` 锁定 | 沿用 §3 |
| RTSP 与解码 | gortsplib + cgo libavcodec | 沿用 §3 |
| 数据库访问 | pgx v5 + sqlc | sqlc 以迁移文件为 schema 来源，生成类型安全代码 |
| 迁移 | goose | 同一套迁移同时执行在 A 和云端，只前进不回滚，见 4.3 节 |
| proto | buf | `buf lint` + `buf breaking` 进 CI |
| HTTP 端点 | Go 标准库 `net/http` | 只有本机和隧道内的基础设施端点，见 4.8 节 |
| 配置 | koanf | 全局默认值在配置文件，按相机覆盖值在 `cameras.params` |
| 日志 | log/slog 输出 JSON 到 stdout，由 A 上的 Vector 采集 | Vector 开磁盘缓冲，写入云端 `ops.logs`，见 4.7 节 |
| 指标 | Prometheus 客户端；A 上运行完整的 Prometheus（不用 agent 模式），本地保留 7 天，同时 remote_write 到云端 | remote_write 只能补发约 2 小时内的断网数据，更长的断网靠 A 本地保留，见 4.7 节 |
| 推理服务 | Python 3.10 + grpcio，每服务一个容器 | 依赖用 uv 生成带哈希的锁文件，取代 Conda 环境的手工维护 |
| 推理加速（后期） | ONNX Runtime / TensorRT | 等价基线验收通过后再做（第 6 节阶段七） |
| 数据库 | A 和云端各一个 PostgreSQL（同一大版本，16 或 17）+ pgvector 0.8 | A 是实时链路的状态库，云端是业务主库，两边用逻辑复制同步，见 4.2、4.3 节 |
| 网络 | WireGuard | A 主动连接云服务器，见 4.1 节 |
| 文件存储 | A：本地文件系统；云端：云厂商的对象存储，通过 S3 兼容接口访问 | `BlobStore` 两种实现都要有，S3 实现用 aws-sdk-go-v2，见 4.5 节 |
| 时钟 | chrony | 帧时间来自 A 上 MediaMTX 的系统时钟（ARCHITECTURE §4），A 必须做时间同步 |
| 部署 | A：docker compose，由 systemd 托管 | 云端的部署归 invisiscan-main 项目 |
| CI | GitHub Actions | golangci-lint、ruff、mypy、`go test`（testcontainers 起真实 Postgres，含一个双库复制的集成测试）、pytest、buf、镜像扫描 |

有一处 docs 的依据需要更正：ARCHITECTURE §3 拆分两个 Python 服务的理由是 AdaFace 要求 `torch<=1.13.1`，但 `legacy/AdaFace-master/requirements-runtime.txt` 对 torch 没有版本约束。建议仍然保持拆分，理由换成：FaceService 无状态、可以单独扩副本，人脸模型也可以单独升级。FaceService 的锁文件必须写死 torch 版本。

## 3. 主机 A 内部：对现有设计的修改

### 3.1 相机归属与 reconcile

DATA_MODEL "恢复未处理轨迹" 的 reconcile SQL 是全表操作。现在 A 上只有一个 pipeline 进程，这没有问题。但只要以后出现第二个进程（第二台算力主机，或者 A 上按相机拆分进程），任何一个进程重启都会关掉另一个进程正在进行的事件。

修改方式：每个相机用 A 本地库上的会话级 advisory lock（`pg_try_advisory_lock(camera_id)`）确定归属，reconcile 的三条 SQL 都加上 `AND camera_id = ANY($owned)`。这个改动很小，建议在阶段三实现。

### 3.2 post-track 任务的认领

`TaskQueue` 用 A 本地库实现：`tracks` 加 `next_attempt_at`、`locked_by`、`locked_until` 三列，worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 认领任务，失败按指数退避，`attempts` 到上限置 `failed`。channel 只做唤醒信号。任务状态和轨迹状态在同一个事务里更新，这是任务队列放在 A 本地库而不用 Redis 的原因。

### 3.3 帧传输

pipeline 和 PerceptionService 都在 A 上。gRPC 同时支持 TCP 和 Unix domain socket，由配置里的地址决定走哪种：
- **Go 客户端：** gRPC-Go 原生支持两种地址写法，`unix:///run/invisiscan/perception.sock` 和 `127.0.0.1:50051`，代码里不需要区分。
- **Python 服务端：** 同一个服务可以同时调用两次 `add_insecure_port`，一次监听 `unix:/run/invisiscan/perception.sock`，一次监听 TCP 端口，两种连接同时可用。

默认部署走 socket，省掉 TCP 开销；测试时改成 TCP 地址，就可以用 grpcurl 调试、在宿主机直接运行 Python 服务，或者让另一台开发机连过来。TCP 端口只监听 127.0.0.1 或 WireGuard 地址，不在公网开放。阶段二测吞吐时两种方式都测一遍，记录每路相机的帧延迟差异。

以后如果加第二台算力主机，每台主机各跑一套 pipeline + PerceptionService，不跨机器传原始帧（每路约 90 MB/s）。

### 3.4 识别底库

沿用 ARCHITECTURE §7：每次识别从 A 本地库读出全部 active 模板的特征，在 Go 内存里做矩阵乘。系统面向 50 到 100 人，底库最多千级，每个特征 512 维约 2 KB，一千张约 2 MB，本地库单次读取只需要毫秒级，不需要缓存，也不需要近似检索。

这样还有一个好处：main 修改底库（停用、合并、删除）后，变更复制到 A，下一次识别就能看到，不存在缓存失效的问题。

### 3.5 数据量

单相机每天约十万行 `track_points`（DATA_MODEL 的估算），几路相机的规模在 A 和云端都不需要分区。`ops.logs` 是例外，它在云端按天分区，见 4.7 节。

## 4. 主机 A 与云端

### 4.1 网络

A 没有公网 IP，所有连接都由 A 主动发起。A 运行 WireGuard 客户端，连接云服务器上的 WireGuard 服务端，设置 `PersistentKeepalive = 25` 保持 NAT 映射。隧道建立后，云端可以通过 A 的隧道地址访问 A，A 仍然不在公网上开放任何端口。

隧道内需要的流量：
- 逻辑复制：两个方向都走隧道（4.2 节）。
- 日志：A 上的 Vector 写云端 `ops.logs`（4.7 节）。
- 指标：A 上的 Prometheus remote_write 到云端；云端也可以通过隧道直接查询 A 上的 Prometheus（4.7 节）。
- 录像与实时画面：invisiscan-main 通过隧道反向代理 A 上 MediaMTX 的 HLS 和 playback 端口，每个观看者约 4 Mbps（4.5 节）。

图片上传不走隧道：A 通过 HTTPS 直接访问云厂商对象存储的公网地址（4.5 节）。

两边的 PostgreSQL 都只监听 WireGuard 网卡地址，`pg_hba.conf` 只放行对端的隧道 IP，复制用专门的低权限账号。

### 4.2 双库与逻辑复制

A 本地运行一个 PostgreSQL，作为实时链路的状态库。pipeline、post-track worker、reconcile、相机锁、任务认领全部只访问这个本地库，延迟和可用性不受广域网影响。云端 PostgreSQL 是业务主库，invisiscan-main 和 invisiscan-agent 只访问云端库。两边用 PostgreSQL 原生逻辑复制同步，订阅端都通过隧道连接发布端。

两边执行同一套迁移，表结构相同。表按写入方划分：

| 表 | 写入方 | 复制方向 | 说明 |
|---|---|---|---|
| `events`、`tracks`、`track_points`、`track_images`、`recognition_logs`、`recordings` | A | A → 云 | 发布时设 `publish = 'insert, update'`，不复制 DELETE。A 本地只保留短期数据，按自己的保留期删除；云端保留长期数据，由云端的清理任务管理 |
| `domain_events`、`pipeline_heartbeats` | A | A → 云 | 同样不复制 DELETE。断网期间 A 删掉的旧心跳，插入记录仍在复制槽保留的 WAL 里，恢复后照常同步到云端 |
| `face_embeddings`（新增，见 4.4 节） | A | A → 云 | |
| `locations`、`cameras`、`attendance_rules` | main | 云 → A | |
| `subjects`、`face_templates` | 两边 | 双向 | 规则见 4.4 节 |
| `ops.*`、`main.*` | 云端 | 不复制 | 日志、审计、main 自己的表 |

### 4.3 复制的实现细节

- **两个订阅。** 云端订阅 A 的发布 `pub_a`，A 订阅云端的发布 `pub_cloud`。两个订阅都设 `origin = none`（PostgreSQL 16 起支持），这样从对方复制过来的行不会再被发回去，双向表不会循环复制。
- **主键冲突。** 只由一方插入的表，主键序列只在写入方使用，不会冲突。`subjects` 和 `face_templates` 两边都会插入，所以两边的自增 ID 要错开：A 用奇数（`START WITH 1 INCREMENT BY 2`），云端用偶数（`START WITH 2 INCREMENT BY 2`）。这是一个按站点执行的迁移步骤。两张表本来就有 `uuid`，对外一律用 uuid。
- **生成列。** `recognition_logs.occurred_date` 是生成列，PostgreSQL 16/17 不复制生成列，由订阅端自己计算，结果相同。
- **外键与触发器。** 复制写入时订阅端默认不触发普通触发器，外键也不检查。所以同一个业务事务里的行必须放在同一个发布里，才能保证按事务顺序到达。目前没有需要在订阅端响应复制写入的触发器；以后如果加，要设成 `ENABLE ALWAYS`。订阅不开并行应用（`streaming` 设为 `off` 或 `on`，不用 `parallel`），保证订阅端按发布端的提交顺序写入，4.6 节的消费游标依赖这一点。
- **心跳表的冲突。** `pipeline_heartbeats` 按实例 id 做 upsert，A 每 10 秒更新同一行。逻辑复制对这类更新不友好，所以心跳表改为只插入：每次心跳插入一行，云端按实例取最新一行，A 和云端各自保留 24 小时后删除。这样心跳历史也保留下来了，agent 可以看 A 的在线记录。
- **NOTIFY 不跨库。** ADMIN_API.md 上一版用 `NOTIFY` 通知 A 配置变更，这在双库下不成立，因为云端的 NOTIFY 到不了 A。改为 A 每 5 秒轮询：相机配置看 `cameras.updated_at`，新上传的模板看"没有对应 `face_embeddings` 行的模板"。两个查询都走索引，开销很小。
- **种子数据。** `0002_seed.sql` 只在云端执行，A 通过订阅的初始同步拿到数据。
- **schema 变更。** 逻辑复制不同步 DDL。同一套迁移在两边都执行，按"先扩展、后收缩"的方式写：先在两边加列或加表，再上线写新列的代码，删列放到后续版本。新增复制表后要 `ALTER PUBLICATION ... ADD TABLE`，并在对端执行 `ALTER SUBSCRIPTION ... REFRESH PUBLICATION`。迁移工具在 A 和云端各跑一次，CI 里用双库集成测试验证。
- **断网容忍度：5 天。** 断网期间，复制槽会保留 WAL，直到对端追上进度。复制槽保留的是整个库的 WAL，不只是被复制的表，所以要按实测量来定上限：
  - **A 侧：** 阶段一跑通写库链路后，用 `pg_current_wal_lsn()` 测一天的 WAL 增长量 `W`，设 `max_slot_wal_keep_size = 5 × W × 2`。在实测之前先设 50 GB。按单相机每天约十万行轨迹点估算，`W` 应该在 1 GB 以内，这个初始值是宽松的。
  - **云端：** 断网期间 A 也连不上云端的发布，云端为 A 保留的复制槽同样会积压 WAL，按同样的方法设上限。
  - **超过上限：** 复制槽会失效，需要重建订阅，并用脚本按时间段补齐缺失的数据。所以复制延迟和复制槽保留的 WAL 大小是最重要的两个告警指标，断网超过 3 天就要告警，留出处理时间。
  - **网络恢复后：** 订阅自动重连并追赶进度，不需要人工操作。

### 4.4 人员与人脸模板的双向写入

两张表都由两边写入，冲突靠"插入两边都可以，更新只归一方"来避免：

| 操作 | 谁做 | 写哪里 |
|---|---|---|
| 自动注册陌生人 | A | 插入 `subjects` + `face_templates` + `face_embeddings`，同一个事务 |
| 新建人员、上传模板图 | main | 插入 `subjects`、`face_templates` |
| 计算模板特征 | A | 插入 `face_embeddings`，成功写 `status = 'ready'` 和特征，失败写 `failed` 和原因 |
| 改名、确认陌生人、停用、合并、删除 | main | 只更新 `subjects`、`face_templates` |

为此 DDL 要改两处：

- **特征拆表。** 特征从 `face_templates` 拆到新表 `face_embeddings(template_id, model_name, status, embedding vector(512), norm, error, created_at)`，主键是 `(template_id, model_name)`。现有 `0001_init.sql` 在 `face_templates.embedding` 上建了 HNSW 索引 `face_templates_embedding_hnsw`，ARCHITECTURE §7 保留它的理由是"以后的单向量检索和底库查重"，识别主路径不依赖它。底库是千级，查重直接全表扫描也只要毫秒级，所以拆表时不迁移这个索引；以后确实需要再在 `face_embeddings` 上补建。这样 main 上传的模板行，A 不需要回头更新，A 只插入自己拥有的特征行。DATA_MODEL 原本就希望"换模型时新旧特征共存"，拆表后一个模板可以有多个模型的特征，正好满足这一点。`embedding` 允许为空：`status = 'failed'` 的行也要写进去，否则 A 的轮询会把失败的模板反复拿出来重算。重试次数存在这张表上，超过上限才置 `failed`。
- **合并和删除只做软标记。** `subjects` 加 `merged_into`、`deleted_at`，`face_templates` 加 `deleted_at`：
  - 合并时，main 把被合并人的模板改到目标人名下，并设置 `merged_into`。
  - 识别日志属于 A 写入的表，main 不修改。查询时按 `COALESCE(merged_into, subject_id)` 归并。
  - 物理删除由两边的清理任务各自执行，对应的发布只同步 insert 和 update。

A 的识别底库查询改为：`face_templates` 未删除且 active、`subjects` 未删除、未合并且 active，再关联当前模型的 `face_embeddings`。

### 4.5 图片与录像

**轨迹图片：每条轨迹最多上传 10 张压缩图。**

- **范围。** 只上传 `body`（人体裁剪图），indoor、outdoor、pass 三类轨迹都传，`discarded` 不传。`seg` 和 `face` 是算法中间产物，只留在 A。
- **选图。** 在轨迹的 `started_at` 到 `ended_at` 之间按时间等分，取 10 个时间点 `t_k = started_at + k × (ended_at − started_at) / 9`（k = 0…9，含首尾）。每个时间点取 `frame_time` 最接近的 body 图，并去重。body 图不足 10 张就全部上传。按时间等分而不是按序号等分，是因为推理跟不上时会丢帧（ARCHITECTURE §3 的最新帧槽），序号和时间不一定均匀对应。
- **替代旧规则。** 被选中的图在 `track_images.selected` 上打标记。这条规则替代 ARCHITECTURE §7"去掉前 3 张、后 2 张，再均匀采样 6 张"的规则。旧规则去掉首尾，是因为轨迹刚出现和快消失时常常只拍到半个人。如果首尾图质量不好，可以改成去掉首尾几张后再等分，只需要改选图函数。
- **压缩。** 长边缩到 512 像素，用 Go 标准库编码成 JPEG，质量 75，单张几十 KB。A 本地保留质量 95 的原图，供分割、人脸对齐和回归测试使用，压缩图只用于上传。想再压小一些可以用 WebP，但 Go 标准库没有 WebP 编码器，需要通过 cgo 调用 libwebp。
- **上传。** 轨迹结束、确认不是 `discarded` 后，由上传任务写到云端对象存储 `previews/tracks/{track_uuid}/{seq:05d}.jpg`。对象键存进新增列 `track_images.preview_key`，上传成功后 A 写 `uploaded_at`，随复制同步到云端。断网时任务积压，恢复后补传。
- **陌生人模板图。** A 自动注册陌生人时，把注册用的人脸图（112×112，几 KB）原样上传到 `subjects/{subject_uuid}/{template_uuid}.jpg`。main 的"待确认陌生人"界面要显示它。
- **本地清理。** A 本地图片按 A 的保留期删除，被选中的图要确认已经上传才能删。

**main 上传的模板原图。** main 写到云端对象存储，A 计算特征时从对象存储下载。

**云端对象存储：使用云厂商的服务。**

- **选型：** 和云服务器选同一家厂商、同一个地域。国内主流厂商的对象存储（阿里云 OSS、腾讯云 COS、华为云 OBS 等）都提供 S3 兼容接口，`BlobStore` 的 S3 实现用 aws-sdk-go-v2，换厂商只改 endpoint 和凭证。如果所选厂商的 S3 兼容层缺某个功能，就在 `BlobStore` 后面换成厂商原生 SDK，调用方不受影响。
- **访问路径：** A 通过 HTTPS 访问对象存储的公网地址，不走隧道，因为对象存储不在云服务器上。invisiscan-main 用同地域的内网地址访问，不产生公网流量费。
- **权限：** bucket 设为私有。A 用一个专门的子账号，只能写 `previews/` 和 `subjects/` 前缀、读 `subjects/` 前缀，凭证放在 A 的环境变量或密钥文件里，不进代码和镜像。前端看图用 main 签发的有时效的预签名地址。
- **保留与版本：** `previews/` 用生命周期规则按云端的保留期自动删除。bucket 开启版本控制，旧版本 30 天后过期，防止误删。

**录像：沿用 ARCHITECTURE §3 的单流方案，保留 4 天。**

- **录制方式：** MediaMTX 直接录制推理用的那路流（1440×2560、约 4 Mbps），只换封装，不解码也不重新编码，录像和推理帧的时间完全一致。`recordFormat: fmp4`、`recordSegmentDuration: 1h` 不变。
- **保留期：** `recordDeleteAfter: 96h`。MediaMTX 默认只保留 1 天，必须显式设置。
- **存储量：** 按 4 Mbps 算，每路相机每天约 43 GB，4 天约 173 GB。实际码率在阶段二实测后再核算。
- **元数据：** `recordings` 表照常由 A 写入并复制到云端，所以 main 能查到"某个事件对应哪段录像"。切片被 MediaMTX 删除后，A 在启动扫描和定时扫描时写 `recordings.deleted_at`，随复制同步到云端。云端的 `recordings` 行可以保留得比文件久，main 据此显示"录像已过期"。
- **播放：** main 通过隧道反向代理 A 上 MediaMTX 的 playback 和 HLS 端口。A 离线时录像和实时画面不可用，元数据仍然可以查询。
- **上行带宽：** 实时画面和录像回放都是原码流，每个观看者占用 A 约 4 Mbps 的上行带宽。观看的人多或者上行带宽不够时，可以在 MediaMTX 上按需转一路低码率的预览流，只在有人观看时运行。这个现在不做。

**A 的磁盘。** 录像（每路 4 天约 173 GB）、复制槽 WAL、本地原图、待上传的压缩图、Vector 磁盘缓冲都在 A 上，磁盘使用率要纳入告警。

### 4.6 领域事件

新增 `domain_events` 表：`id`（bigint 自增）、`type`（如 `recognition.created`、`event.closed`、`subject.auto_enrolled`、`track.failed`、`reconcile.recovered`、`retention.purged`）、`aggregate_uuid`、`payload jsonb`、`occurred_at`。业务操作在同一个事务里插入领域事件。

消费方按 `id` 维护游标，必须保证 id 小的事件先提交，否则游标会跳过晚提交的事件。领域事件的写入量很小（每分钟几条到几十条），所以直接串行化：插入前先取 `pg_advisory_xact_lock(<固定键>)`，事务提交才释放。这样 id 顺序就等于提交顺序。云端订阅按提交顺序应用，所以云端看到的 id 顺序同样可靠。

消费方都在云端：
- **invisiscan-main：** 用于实时推送、webhook 和系统操作审计。
- **invisiscan-agent：** 读取 `track.failed`、`reconcile.recovered` 等系统事件。

Fay 通知先取消。ARCHITECTURE §5 和 §7 里"事务提交后调用 Fay 的 `GET /face/hello`"那一步不实现，`internal/recognize/` 里也不写 Fay 客户端。以后要恢复时，在 A 上加一个消费 `recognition.created` 的通知器，游标存在 A 本地，门口问候就不依赖云端。

A 本地的 `domain_events` 保留 7 天；云端保留期由 main 决定。

### 4.7 日志与运维数据

日志存进云端数据库，供 invisiscan-main 展示、invisiscan-agent 分析。invisiscan-ai 的代码不直接写日志表，只用 slog 向 stdout 输出 JSON，字段固定为 `ts`、`level`、`service`、`host`、`camera_id`、`track_uuid`、`trace_id`、`msg`、`attrs`。

A 上运行 Vector，从 Docker 日志采集各容器的 stdout，通过 [PostgreSQL sink](https://vector.dev/docs/reference/configuration/sinks/postgres/) 经隧道写入云端 `ops.logs`。sink 开启[磁盘缓冲](https://vector.dev/docs/architecture/buffering-model/)，断网期间日志存在 A 的磁盘上，恢复后补发。缓冲写满时的行为设为 `when_full = "drop_newest"`：丢弃新到的日志，已缓冲的旧日志保留，业务进程不受影响。Vector 的缓冲不支持"丢弃最旧的"，所以缓冲上限 `max_size` 按 5 天的日志量留足：阶段五实测每天的日志量 `L`，设 `max_size = 5 × L × 2`，实测之前先设 10 GB。这样日志走的是独立通道，不经过逻辑复制，也不占用 A 本地库。

`ops.logs` 在云端按天分区，默认保留 30 天，到期整块删除。表结构和分区由云端迁移管理，归 invisiscan-main 项目或单独的 ops 迁移，A 不执行这部分迁移。

invisiscan-agent 能读到的运维数据：
- `ops.logs`：运行日志。
- `pipeline_heartbeats`：pipeline 每 10 秒插入一行，内容是实例 id、版本、负责的相机，以及每个相机的拉流状态、最后一帧时间、最后一次 RTCP SR 时间、推理服务是否连通、待处理任务数、A 的磁盘使用率、待上传图片数。这张表从 A 复制到云端，所以云端看到的心跳时间包含复制延迟，判断离线的阈值要留余量（例如 60 秒）。
- `domain_events`：系统事件。
- Prometheus：A 上运行完整的 Prometheus，抓取本机各服务的 `/metrics`，本地保留 7 天（`--storage.tsdb.retention.time=7d`），同时 remote_write 到云端的 Prometheus 或兼容存储。不用 agent 模式，原因是 remote_write 只能补发约 2 小时内的数据，断网更久，WAL 被压缩后这部分数据就发不出去了（[Prometheus 文档](https://prometheus.io/docs/practices/remote_write/)），而 agent 模式没有本地存储，也不能查询（[Agent 模式](https://prometheus.io/docs/prometheus/3.14/prometheus_agent/)）。用完整的 Prometheus，断网超过 2 小时那段时间的指标云端会缺一段，但 A 本地还保留着，云端可以通过隧道把 A 的 Prometheus 当作第二个数据源来查。
- 复制状态：云端 `pg_stat_subscription` 看订阅延迟；A 的复制槽 WAL 大小通过 A 的指标上报。

agent 以后如果要在 A 上执行操作（例如重启某个服务），云端不直接调用 A。预留一张 `ops_commands` 表，由云端写入、复制到 A；A 上的执行器轮询这张表，只执行白名单里的动作，结果写回 `ops_command_results`，再复制到云端。这一部分现在不实现，只在阶段五预留表结构。

### 4.8 本项目保留的端点

invisiscan-ai 不对外提供业务接口。只保留基础设施端点，全部只监听本机或 WireGuard 地址：
- `POST /internal/recordings`：MediaMTX 的 `runOnRecordSegmentComplete` 回调（ARCHITECTURE §3），只监听 127.0.0.1，校验共享 token。
- `/healthz`、`/readyz`：给 docker compose 和 systemd 做健康检查。
- `/metrics`：给 A 上的 Prometheus 抓取。
- A 上 Prometheus 的查询端口：只监听 WireGuard 地址，供云端查询断网期间的指标（4.7 节）。
- MediaMTX 的 HLS 和 playback 端口：只监听 WireGuard 地址，供 invisiscan-main 反向代理（4.5 节），开启 MediaMTX 自带的认证。

### 4.9 Redis 的位置

Redis 可以用，但放在云端，用在 invisiscan-main 里：多实例之间广播实时推送、缓存、限流、会话存储。

A 到云端的数据通道不用 Redis。这条链路最重要的是断网期间不丢数据：Redis 放在云端，A 断网时同样写不进去；放在 A 上，作用和 A 本地的 PostgreSQL 重复，还要额外处理持久化。A 内部的任务队列也不用 Redis，原因见 3.2 节。

## 5. 生产运维

### 5.1 数据保留

保留期按存储位置分别设置：
- **A 本地：** 录像（MediaMTX `recordDeleteAfter: 96h`，即 4 天）、轨迹图片（已上传的）、本地库里的轨迹数据、`domain_events`（7 天）。
- **云端：** 轨迹数据、识别日志、`ops.logs`（默认 30 天）由云端的清理任务执行；图片由对象存储的生命周期规则删除。

录像只保留 4 天，比断网容忍时间短。断网超过 4 天时，断网第一天的录像在网络恢复前就被删掉了，云端只能看到这些录像的元数据和 `deleted_at`。这是保留 4 天的直接结果，需要接受。

清理任务按分区或按时间范围批量删除，每次清理写一条 `retention.purged` 领域事件。

### 5.2 可观测性

指标在 ARCHITECTURE §10 阶段五的基础上增加：按相机的拉流状态、RTCP SR 最近到达时间、`clock_source = fallback_local` 的帧占比、GPU 利用率和显存、底库规模、待处理任务数、`failed` 轨迹数、待上传图片数、复制延迟、复制槽 WAL 大小、Vector 磁盘缓冲使用量、A 的磁盘使用率。

告警放在云端，由 invisiscan-agent 或 Alertmanager 负责。A 离线时云端收不到 A 的指标，所以 A 离线本身要作为一条告警，依据是心跳超时和复制中断。离线超过 3 天再升级告警一次。复制槽上限按 5 天用量的两倍设置（4.3 节），但实测之前用的是估算值，3 天告警是为了在接近 5 天之前留出处理时间。断网期间 A 本地的磁盘告警发不出去，所以 A 的复制槽、Vector 缓冲和磁盘上限都按 5 天预先留足，而不是依赖告警来处理。

### 5.3 备份与发布

- **备份：** A 本地库只是云端的上游，不单独备份；云端用 pgBackRest 做每日全量加 WAL 归档，支持按时间点恢复。对象存储开启版本化。每季度做一次云端恢复演练。
- **A 的恢复：** A 的磁盘损坏时，本地库可以从云端重新初始化：建空库、执行迁移、重新建立订阅，由逻辑复制的初始同步补齐数据。丢失的只有还没复制到云端的那几秒到几分钟数据，以及还没上传的图片和还没发出的日志。录像没有上云，A 的磁盘损坏会丢失录像，这一点需要接受。
- **发布：** 镜像用 cosign 签名，生成 SBOM，用 Trivy 扫描。迁移按 4.3 节的顺序执行。
- **模型：** 权重放在 A 本地，按 sha256 校验。`SessionReady.detector_model` 等字段返回模型标识，写进轨迹记录，随复制到云端。

## 6. 实施计划

阶段一到阶段六与 ARCHITECTURE §10 一致，本表只列追加内容。

| 阶段 | docs 原定内容 | 追加内容 |
|---|---|---|
| 一 | 骨架与数据层 | 按 4.4 节改 DDL：`face_embeddings`、`domain_events`、`pipeline_heartbeats`、任务认领三列、软删除列、`track_images.preview_key` 和 `uploaded_at`；按站点错开的 ID 序列；双库复制的集成测试 |
| 二 | 采集、帧时间、感知服务 | WireGuard 隧道；A 的时间同步；gRPC 的 TCP 和 socket 两种地址；录像保留 4 天（`recordDeleteAfter: 96h`），实测推流的实际码率；实测单 GPU 能带几路相机 |
| 三 | 跟踪器移植 | 3.1 节相机锁与限定相机的 reconcile |
| 四 | 分类与人脸识别 | 3.2 节任务认领；4.6 节领域事件；模板特征计算 worker；不做 Fay 通知 |
| 五 | 归档、运维（docs 原定的查询 API 移到 invisiscan-main） | 4.5 节选图、压缩上传与 MediaMTX 反向代理；4.7 节 Vector 日志、心跳、指标；4.8 节端点；第 5 节备份与告警；预留 `ops_commands` |
| 六 | ReID（可选） | 不变 |
| 七（可选） | 无 | 算法迭代与加速：验收标准从"与旧链路逐像素一致"改为评测集指标（检测 mAP、跟踪 IDF1/HOTA、人脸 TAR@FAR、LSTM 分类准确率）；然后做模型升级、ONNX/TensorRT 加速 |

阶段七的前提是阶段二到四已经建好黄金数据和回归工具，这样架构迁移和算法改动可以分开验证。

## 7. 已确认的参数

| 参数 | 取值 | 由此确定的配置 |
|---|---|---|
| 系统规模 | 50 到 100 人，底库最多千级 | 不做底库缓存，不建 HNSW 索引（3.4 节） |
| 录像 | 单流原码流，保留 4 天 | `recordDeleteAfter: 96h`，每路约 173 GB（4.5 节） |
| 云端对象存储 | 云厂商的服务 | S3 兼容接口，A 走公网 HTTPS，main 走内网（4.5 节） |
| 断网容忍时间 | 5 天 | 两边复制槽 `max_slot_wal_keep_size = 5 × 日 WAL 量 × 2`（4.3 节）；Vector 缓冲 `max_size = 5 × 日志量 × 2`（4.7 节）；A 上 Prometheus 本地保留 7 天（4.7 节）；断网超过 3 天告警 |

带"实测"的上限在阶段一和阶段五测出实际增长量后再定，测之前用文中给的初始值。

