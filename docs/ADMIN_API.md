# invisiscan-main：API 清单与跨项目契约（草案）

本文件的API无需 invisiscan-ai 实现，仅在架构上提供参考

本文件是给 invisiscan-main（云端业务项目）的设计输入。这里列出的 API 全部由 invisiscan-main 实现，invisiscan-ai 不实现。表结构依据 invisiscan-ai 的 `migrations/0001_init.sql`，标"新增"的表由 invisiscan-ai 后续的迁移创建。编写日期 2026-11-26，同日按双库部署修订。

部署与数据同步的完整方案见 `IMPLEMENTATION_PLAN.md` 第 4 节，这里只列 invisiscan-main 需要知道的部分。

## 1. 分工

| 项目                     | 位置                               | 职责                                         |
| ------------------------ | ---------------------------------- | -------------------------------------------- |
| invisiscan-ai            | 主机 A（本地 GPU 主机，无公网 IP） | 拉流、推理、跟踪、识别、录像。不对外提供接口 |
| invisiscan-main          | 云服务器                           | 业务 API、用户认证、审计、前端               |
| invisiscan-agent（后期） | 云服务器                           | 自动运维，读数据库                           |

A 主动用 WireGuard 连接云服务器。两边各有一个 PostgreSQL，用逻辑复制同步，invisiscan-main 只访问云端库，不直连 A 的库。

依赖方向是 invisiscan-main 依赖 invisiscan-ai 的数据。invisiscan-main 没部署或宕机时，A 上的拉流、跟踪、识别和录像照常运行，数据先存在 A 本地库，网络恢复后自动同步。反过来，A 离线时 invisiscan-main 仍然可以查询已经同步过来的历史数据，但实时画面和录像播放不可用。

## 2. 数据从哪来

| 数据                               | 来源                                                       | invisiscan-main 怎么读                                                                     |
| ---------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 事件、轨迹、识别日志、录像元数据   | A 写入本地库                                               | 逻辑复制到云端，只读                                                                       |
| 人员、人脸模板                     | 两边都写                                                   | 见第 3 节的写入规则                                                                        |
| 人脸特征 `face_embeddings`（新增） | A 写入                                                     | 逻辑复制到云端，只读                                                                       |
| 领域事件 `domain_events`（新增）   | A 写入                                                     | 逻辑复制到云端，按 id 游标消费                                                             |
| 心跳 `pipeline_heartbeats`（新增） | A 每 10 秒插入一行                                         | 逻辑复制到云端，按实例取最新一行                                                           |
| 运行日志 `ops.logs`                | A 上的 Vector 经隧道直接写云端库                           | 不经过复制，见 5.1 节                                                                      |
| 指标                               | A 上的 Prometheus 本地保留 7 天，同时 remote_write 到云端  | 查云端 Prometheus。断网超过约 2 小时，云端会缺一段指标，这段要通过隧道查 A 上的 Prometheus |
| 图片                               | A 异步上传到云端对象存储，每条轨迹最多 10 张压缩过的人体图 | 只展示 `track_images.uploaded_at` 不为空的图片，见 5.4 节                                  |
| 录像和实时画面                     | 只存在 A 本地，原码流，保留 4 天                           | 通过隧道反向代理 A 上的 MediaMTX，见 5.4 节                                                |

复制有几秒到几十秒的延迟，断网期间延迟会拉长到断网时长。系统按最多断网 5 天设计，5 天内恢复，数据会自动补齐；录像只保留 4 天，断网超过 4 天时，最早那部分录像在恢复前就已删除。界面上要能看出"数据更新到什么时间"，依据是复制状态和心跳时间。

## 3. 写入规则

逻辑复制要求每张表的更新只来自一方，否则两边同时改同一行会冲突。所以 invisiscan-main 的写权限是受限的。

| 表                                                                                                                           | invisiscan-main 的权限 | 规则                                         |
| ---------------------------------------------------------------------------------------------------------------------------- | ---------------------- | -------------------------------------------- |
| `locations`、`cameras`、`attendance_rules`                                                                                   | 读写                   | 只有 invisiscan-main 写                      |
| `subjects`、`face_templates`                                                                                                 | 插入和更新             | 只做软删除（设 `deleted_at`），不执行 DELETE |
| `face_embeddings`                                                                                                            | 只读                   | 特征由 invisiscan-ai 计算后写入              |
| `events`、`tracks`、`track_points`、`track_images`、`recognition_logs`、`recordings`、`domain_events`、`pipeline_heartbeats` | 只读                   | 只由 invisiscan-ai 写                        |
| `ops.*`、`main.*`                                                                                                            | 独占                   | 日志、审计、消费游标、webhook，只存在云端    |

合并人员时，invisiscan-main 把被合并人的模板改到目标人名下，并设置 `subjects.merged_into`。识别日志属于 invisiscan-ai 的表，不能改，查询时按 `COALESCE(merged_into, subject_id)` 归并。

`subjects` 和 `face_templates` 的自增 ID 在两边错开（A 用奇数，云端用偶数），对外一律用 uuid。

## 4. API 清单

所有接口都在 `/api/v1` 下，共用以下约定：

- **ID：** 表里有 `uuid` 列的资源，对外用 uuid，不暴露自增 id。
- **时间和分页：** 时间统一用带时区的 RFC 3339 格式，列表统一用游标分页。
- **错误：** 错误返回 RFC 9457 的 `application/problem+json` 格式。

### 4.1 只读查询

| 接口                                                               | 说明                                                                                                |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `GET /locations`、`GET /cameras`、`GET /cameras/{id}`              | 地点和相机信息                                                                                      |
| `GET /events?camera_id=&from=&to=`                                 | 事件列表                                                                                            |
| `GET /events/{uuid}`                                               | 事件详情，带轨迹列表和识别结果                                                                      |
| `GET /events/{uuid}/playback`                                      | 事件对应的录像片段和时间偏移，见 5.4 节                                                             |
| `GET /tracks/{uuid}`                                               | 轨迹状态、分类结果和各类概率                                                                        |
| `GET /tracks/{uuid}/points`                                        | 轨迹点序列                                                                                          |
| `GET /tracks/{uuid}/images`                                        | 轨迹的预览图，最多 10 张，按时间排序，只返回已上传的。分割图和人脸裁剪图只留在 A，不提供，见 5.4 节 |
| `GET /recognition-logs?location_id=&date=&subject_uuid=&decision=` | 识别日志                                                                                            |
| `GET /attendance/daily?location_id=&date=`                         | 某天的考勤汇总，基于 `v_attendance` 视图                                                            |
| `GET /recordings?camera_id=&from=&to=`                             | 录像切片列表。A 离线时仍可查询元数据，但无法播放                                                    |

### 4.2 管理写入

| 接口                                       | 说明                                                                         |
| ------------------------------------------ | ---------------------------------------------------------------------------- |
| `GET /subjects?kind=`                      | 人员列表。`kind=unknown` 就是"待确认陌生人"队列                              |
| `POST /subjects`、`PATCH /subjects/{uuid}` | 新建、改名。陌生人确认身份时把 `kind` 改成 `known` 并填上名字                |
| `DELETE /subjects/{uuid}`                  | 软删除：设 `deleted_at`，模板一起软删除                                      |
| `POST /subjects/{uuid}/merge`              | 合并到已登记的人名下，见第 3 节                                              |
| `GET/POST /subjects/{uuid}/templates`      | 查看、上传模板图。上传是异步的，返回 202，见 5.2 节                          |
| `GET /templates/{uuid}`                    | 查看模板处理状态：`pending` / `ready` / `failed`，状态来自 `face_embeddings` |
| `PATCH/DELETE /templates/{uuid}`           | 停用或软删除单张模板                                                         |
| `POST/PATCH /cameras/{id}`                 | 新增相机、修改配置，包括 ROI 和 `params`，见 5.3 节                          |
| `GET/POST /attendance-rules`               | 考勤规则。只新增带生效日期的规则、不修改旧规则，所以历史数据的判定不会变     |

### 4.3 实时推送和集成

| 接口                                                                                 | 说明                                                                                                         |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| `GET /stream`（SSE）                                                                 | 推送事件开始和结束、轨迹确认和分类、新的识别结果、相机上下线。数据来自 `domain_events`，推送延迟包含复制延迟 |
| `GET /cameras/{id}/live`                                                             | 实时画面地址，见 5.4 节                                                                                      |
| `WS /cameras/{id}/detections`                                                        | 每帧检测框，后期再做，见 5.4 节                                                                              |
| `GET/POST/DELETE /webhook-subscriptions`、`GET .../deliveries`、`POST .../redeliver` | webhook 订阅管理、投递记录、手动重发。数据来自 `domain_events`                                               |

### 4.4 运维和审计

| 接口                                             | 说明                                                                                    |
| ------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `GET /system/status`                             | 各相机拉流状态、推理服务是否连通、待处理任务数、数据同步延迟、A 的磁盘使用率，见 5.1 节 |
| `GET /logs?service=&level=&camera_id=&from=&to=` | 运行日志，查 `ops.logs`                                                                 |
| `GET /audit-logs`                                | 审计日志，包括用户操作和系统操作，见 5.1 节                                             |

## 5. 跨项目的实现细节

### 5.1 日志、状态与审计

- **运行日志。** invisiscan-ai 只向 stdout 输出 JSON，A 上的 Vector 经隧道直接写入云端 `ops.logs`，不经过逻辑复制。`ops.logs` 按天分区，默认保留 30 天，由 invisiscan-main 的迁移创建和清理。断网期间日志存在 A 的磁盘缓冲里，恢复后补发，所以日志可能比业务数据晚到。
- **系统状态。** `GET /system/status` 查 `pipeline_heartbeats` 的最新一行。心跳从 A 复制过来，时间里包含复制延迟，判断离线的阈值要留余量（例如 60 秒）。趋势数据查 Prometheus。
- **审计。** 分两类：
  - 用户操作（改名、合并、上传或删除模板、修改相机配置、查看人脸原图、导出）由 invisiscan-main 在执行操作的同一个事务里写 `main.audit_logs`。
  - 系统操作（自动注册陌生人、保留期清理、reconcile 强制关闭、轨迹置为 `failed`）由 invisiscan-ai 写进 `domain_events`，invisiscan-main 消费后写入审计表，操作者记为 `system`。

`domain_events` 按 `id` 消费，已消费的最大 `id` 存在 `main.consumer_offsets`。id 顺序等于提交顺序，这个保证由 invisiscan-ai 负责（见 `IMPLEMENTATION_PLAN.md` 4.6 节）。消费至少一次，按 `id` 去重。

### 5.2 上传人脸模板

invisiscan-main 不调用 invisiscan-ai 的推理服务。流程：

1. invisiscan-main 把原图写入云端对象存储 `subjects/{subject_uuid}/{template_uuid}.jpg`。
2. 插入 `face_templates` 行：`active = true`、`source = 'manual'`（特征算好之前，模板因为没有 `ready` 的特征而不参与识别）。接口返回 202 和模板 uuid。
3. 这行复制到 A 后，invisiscan-ai 从云端对象存储下载原图，调用 `AlignFaces` 和 `EmbedFaces`，把结果插入 `face_embeddings`：成功写 `status = 'ready'` 和特征，失败写 `failed` 和原因。
4. `face_embeddings` 复制回云端，invisiscan-main 通过 `GET /templates/{uuid}` 返回状态，也可以监听 `domain_events` 里的 `template.ready` / `template.failed`。

从上传到状态可见，中间经过两次复制和一次推理，正常情况下是几秒到几十秒。

### 5.3 修改相机配置

invisiscan-main 更新 `cameras` 后，这行复制到 A，invisiscan-ai 轮询 `cameras.updated_at` 发现变化，重建这个相机的推理会话。生效延迟是复制延迟加最多 5 秒。不需要调用 invisiscan-ai 的任何接口。

### 5.4 图片、录像与实时画面

- **对象存储。** 使用云厂商的对象存储，和云服务器同一厂商、同一地域，通过 S3 兼容接口访问。invisiscan-main 走同地域内网地址；bucket 私有，前端只拿预签名地址。`previews/` 由生命周期规则按保留期删除。
- **轨迹预览图。** 每条非 `discarded` 的轨迹，invisiscan-ai 从首帧到末帧按时间等分选出最多 10 张人体图，长边缩到 512 像素、JPEG 质量 75，异步上传到云端对象存储 `previews/tracks/{track_uuid}/{seq:05d}.jpg`。对象键在 `track_images.preview_key`，`selected = true` 且 `uploaded_at` 不为空才返回。invisiscan-main 签发有时效的对象存储地址，不直接暴露存储地址。
- **陌生人模板图。** 自动注册的陌生人，注册用的人脸图（112×112）上传到 `subjects/{subject_uuid}/{template_uuid}.jpg`，供"待确认陌生人"界面显示。
- **录像和实时画面。** 录像只存在 A 本地，直接录推理用的那路流（1440×2560、约 4 Mbps），保留 4 天。invisiscan-main 通过 WireGuard 隧道反向代理 A 上 MediaMTX 的 HLS 和 playback 端口，签发有时效的地址给前端。每个观看者占用 A 约 4 Mbps 上行带宽。录像和事件时间完全对齐。`recordings.deleted_at` 不为空表示录像已过期，返回 410；A 离线时返回 503，并说明主机离线。录像元数据查询不受影响。
- **每帧检测框（后期）。** 检测框每帧都有，不适合走数据库。到需要时由 invisiscan-ai 在隧道内暴露一个 gRPC 服务端流 `SubscribeDetections`，invisiscan-main 订阅后转成 WebSocket。在这之前实时画面只显示视频。

### 5.5 Redis

Redis 部署在云端，供 invisiscan-main 使用：多个 API 实例之间广播 SSE 推送、缓存、限流、会话存储。Redis 不参与和 invisiscan-ai 的数据同步。

## 6. invisiscan-ai 为此提供的内容

- 迁移（两边都执行）：`face_embeddings`、`domain_events`、`pipeline_heartbeats`、软删除列、`track_images.preview_key` 和 `uploaded_at`。
- 数据：按第 2 节的方向复制到云端。
- 隧道内的访问：MediaMTX 的 HLS 和 playback 端口，只监听 WireGuard 地址并开启认证。
- 后期：`SubscribeDetections` gRPC 流。

invisiscan-main 自己负责的迁移：`ops.logs`、`main.audit_logs`、`main.consumer_offsets`、webhook 相关表。
