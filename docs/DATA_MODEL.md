# 数据模型设计

目标库：PostgreSQL 16 + pgvector 0.7+。完整 DDL 在 `migrations/0001_init.sql`，同一套 DDL 在主机 A 本地库和云端主库都执行；按站点执行的脚本在 `migrations/site/`。本文说明设计取舍与字段语义。

## 设计原则

一个时刻只用一个 `timestamptz` 列表达，不再拆 `date` / `time`（旧 `logs` 表三列冗余）。所有业务时间带时区，应用层按 UTC 处理。

身份与素材分离。旧模型里 `pictures` 既存人脸图又兼作临时身份表，导致展示名要 `COALESCE(users.name, pictures.name)` 回退。新模型拆成 `subjects`（身份，含"未知人员"占位身份）和 `face_templates`（特征模板），名字只有一个来源。

派生判断不固化。"迟到"不再写进 `note` 列，而由查询时按 `attendance_rules` 配置或视图计算，阈值调整后历史数据自动一致。

对象存储只存键不存路径。所有图像和视频列存 `object_key`，不存绝对路径，换挂载点或迁移存储不影响历史数据。

状态显式建模。事件和轨迹各有 `status` 列加约束，崩溃恢复靠扫描非终态记录而不是靠外部队列。

每张表只有一个写入方。两个库之间用逻辑复制同步，同一行只能由一方更新，见"双库与写入方"一节。

## 实体关系

```
locations ──┬─< cameras ──┬─< events ──< tracks ──< track_points
            │             │                │
            │             └─< recordings    ├──< track_images
            │                               │
            │                               └──< recognition_logs >── subjects
            └───────────────────────────────────┘                        │
                                   face_embeddings >── face_templates >───┤
                                                      body_templates >───┘
                                                        (ReID 预留)

domain_events、pipeline_heartbeats：独立的追加表，不与上面的表建外键
```

`subjects` 是身份主体，`face_templates` / `body_templates` 是它的生物特征模板（一个人可有多张模板），`face_embeddings` 是人脸模板在某个模型下的特征。`recognition_logs` 既引用 `track`（这次识别基于哪条轨迹）也引用 `subject`（识别成了谁），`subject_id` 为 NULL 表示未识别。

## 双库与写入方

主机 A 本地运行一个 PostgreSQL，作为实时链路的状态库：pipeline、post-track worker、reconcile、相机锁、任务认领只访问这个本地库。云端 PostgreSQL 是业务主库，invisiscan-main 和 invisiscan-agent 只访问云端库。两边执行同一套迁移，表结构相同，用 PostgreSQL 原生逻辑复制同步，订阅端都通过 WireGuard 隧道连接发布端。下表引自 `IMPLEMENTATION_PLAN.md` 4.2 节：

| 表 | 写入方 | 复制方向 | 说明 |
|---|---|---|---|
| `events`、`tracks`、`track_points`、`track_images`、`recognition_logs`、`recordings` | A | A → 云 | 发布时设 `publish = 'insert, update'`，不复制 DELETE。A 本地只保留短期数据，按自己的保留期删除；云端保留长期数据，由云端的清理任务管理 |
| `domain_events`、`pipeline_heartbeats` | A | A → 云 | 同样不复制 DELETE。断网期间 A 删掉的旧心跳，插入记录仍在复制槽保留的 WAL 里，恢复后照常同步到云端 |
| `face_embeddings` | A | A → 云 | |
| `locations`、`cameras`、`attendance_rules` | main | 云 → A | |
| `subjects`、`face_templates` | 两边 | 双向 | 插入两边都可以，更新只归一方，见下 |
| `ops.*`、`main.*` | 云端 | 不复制 | 日志、审计、main 自己的表，不在本项目的迁移里 |

`subjects` 和 `face_templates` 的分工：A 只插入（自动注册陌生人时，`subjects` + `face_templates` + `face_embeddings` 在同一个事务里）；invisiscan-main 插入（新建人员、上传模板图）并负责全部更新（改名、确认陌生人、停用、合并、软删除）。A 计算模板特征只插入 `face_embeddings`，不更新模板行。两张表的自增 ID 错开（A 奇数、云端偶数），两个订阅都设 `origin = none`，从对方复制来的行不会再发回去。

`body_templates` 当前不写入，不在任何发布里，启用 ReID 时再按写入方加入。`0002_seed.sql` 只在云端执行，A 通过订阅的初始同步拿到种子数据。建立复制的顺序见 `migrations/site/README.md`。

## 维度表

旧链路把 `location_id` 和 `camera_id` 作为环境变量硬编码进每个进程（`MYSQL_CAMERA_ID=1`），表本身可能并不存在。新模型把它们建成真表，因为摄像头的分辨率、旋转角、RTSP 地址、深度门限这些参数本来就是按设备变化的配置，放在库里比散在 env 里更可管。

`locations`：`id`、`name`、`description`。

`cameras`：`id`、`location_id`、`name`、`stream_url`、`publish_path`（MediaMTX 路径）、`width`、`height`、`fps`、`rotation`（0/90/180/270，替代旧的 `ROTATE_CCW_90` 布尔开关）、`params jsonb`（深度门限、ROI 分位数、NMS 参数等算法配置，按相机覆盖全局默认值）、`enabled`、`created_at`、`updated_at`。

`cameras` 由云端的 invisiscan-main 写入，复制到 A。`updated_at` 由云端的 `BEFORE UPDATE` 触发器维护，A 每 5 秒轮询这一列发现配置变化，重建对应相机的推理会话。复制写入时订阅端不触发普通触发器，所以 A 上的值就是云端写入时的值。

把算法参数放 `jsonb` 而不是展开成列，是因为这些参数会随调参频繁增删，且只被 Go 侧读取不参与查询条件。

## 事件与轨迹

`events` 对应旧 `events` 表：`id`（bigint 自增）、`uuid`、`camera_id`、`started_at`、`ended_at`、`status`（`open` / `closed`）、`closed_reason`（`absence` 正常结束 / `recovered` 崩溃恢复补齐 / `shutdown` 优雅退出）、`track_count`、`created_at`。

`started_at` 的语义沿用旧链路：取事件内**已确认轨迹中最早的起始时间**，而不是"首次检测到人的帧时间"。事件在第一条轨迹确认时落库，之后若有更早开始的轨迹确认，`started_at` 会被往前改，这是唯一允许改写 `started_at` 的路径。未确认的短轨迹不定义事件起点。`ended_at` 对称地取确认轨迹的最晚出现时间。事件本身的开关仍按检测框数判定（见架构文档第 5 节），所以 `ended_at` 与"最后一帧有检测框"之间最多相差 20 帧加跟踪器的丢失缓冲。`closed_reason` 是新增列，用来区分正常关闭和恢复补齐，避免旧链路那种无法分辨"真的还在进行"和"进程崩了"的悬空记录。

`tracks` 对应旧 `person_tracks`：`id`、`uuid`、`event_id`、`camera_id`、`tracker_id`（跟踪器内的整数 ID，仅用于调试追溯，不唯一）、`started_at`、`ended_at`、`frame_count`、`status`、`end_reason`（`lost` 跟踪器正常移除 / `recovered` 崩溃恢复截断 / `shutdown` 优雅退出时强制结束）、`clock_source`（`rtcp` / `fallback_local`，取首帧的时间来源）、`category`、`category_confidence`、`category_probs jsonb`、`last_error`、`attempts`、`next_attempt_at`、`locked_by`、`locked_until`、`created_at`、`updated_at`。

`next_attempt_at`、`locked_by`、`locked_until` 用于 post-track 任务认领：worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 取 `next_attempt_at <= now()` 且没有被锁（`locked_until` 为空或已过期）的轨迹，写上自己的 `locked_by` 和租约到期时间。失败时 `attempts` 加一，`next_attempt_at` 按指数退避推后，到上限置 `failed`。任务状态和轨迹状态在同一个事务里更新，这是任务队列放在 A 本地库的原因。

`status` 取值 `confirmed` / `ended` / `discarded` / `classified` / `pending_reid` / `identified` / `done` / `failed`。`identified` 是 indoor 轨迹识别完成，`done` 是 pass 轨迹的终态，`failed` 表示 post-track 处理重试 `attempts` 次后仍失败，错误写在 `last_error`，不再自动重投。旧链路的 `tentative` 阶段不落库（见架构文档第 5 节），因此表里不会出现该状态，也就不需要旧链路的"插入后再删除短轨迹"路径。

`category` 取值 `indoor` / `outdoor` / `pass`，与旧 LSTM 的输出标签一致。`category_probs` 存各类概率，旧链路只打日志不入库，存下来便于事后分析误分类。

`event_id` 为 NOT NULL。旧链路有"轨迹先插入、事件后创建、再补写外键"的时序（`update_person_track_event_id`），新设计在同一事务里先建事件再建轨迹，不存在没有事件的轨迹。一条轨迹的生命期跨过事件关闭时，它仍归属原事件，`ended_at` 被截到事件的 `ended_at`。

`track_points` 替代旧的轨迹 txt 文件：`track_id`、`seq`、`frame_time`、`bbox_x` / `bbox_y` / `bbox_w` / `bbox_h`、`depth`、`det_score`。主键 `(track_id, seq)`。

字段语义与旧 txt 逐列对齐，LSTM 的输入才能保持一致：框是跟踪器输出的卡尔曼状态框（左上角 + 宽高），已从推理坐标系换算回原分辨率并取整；`depth` 是该轨迹的深度 EMA（α = 0.8），从未取到有效深度时为 0，不是 NULL，因为旧 txt 写的就是 0，LSTM 训练数据也是这样。`det_score` 是跟踪器输出的分数，新增列，LSTM 不使用。

旧 txt 每行末尾还带 `orig_h,orig_w`（每行重复同一个画面尺寸），这是行级冗余，新模型把画面尺寸放 `cameras` 表，`track_points` 不再重复存储。坐标系约定为旋转后的输出分辨率（1440×2560），写在 DDL 注释里。

数据量：一条轨迹通常几十到几百个点，实验室门口场景每天几千条轨迹以内，日增十万行量级。PostgreSQL 单表在这个规模下几年内都不需要分区，所以不做分区。以后真要分区，主键需要改成包含 `frame_time`，到时再迁移。

`track_images` 登记轨迹产出的图像：`track_id`、`kind`（`body` / `seg` / `face`）、`seq`（与 `track_points.seq` 同一编号，可以按 seq 关联到这张图对应的框和时间）、`object_key`、`quality`（人脸图的 FIQA 分，只有走过第二轮的才有，其他为 NULL）、`selected`（布尔，标记被选为预览图的 body 图，每条轨迹最多 10 张）、`preview_key`（压缩预览图在云端对象存储中的键 `previews/tracks/{track_uuid}/{seq:05d}.jpg`）、`uploaded_at`（A 上传成功后写入）、`created_at`。主键 `(track_id, kind, seq)`。

预览图在轨迹 `started_at` 到 `ended_at` 之间按时间等分取最多 10 张，替代旧链路"去掉前 3 张、后 2 张再均匀采样 6 张"的规则（选图规则见架构文档第 7 节）。只有压缩图上传：长边缩到 512 像素、JPEG 质量 75。`object_key` 指向 A 本地的原图（质量 95），不上传。约束保证只有 `selected` 的 body 图才有 `preview_key`，有 `uploaded_at` 必须先有 `preview_key`。invisiscan-main 只展示 `uploaded_at` 不为空的图。

## 身份与特征底库

这是改动最大的一块。旧模型 `pictures(user_id, name, face_image BLOB, score)` 把人脸 JPG 存在 MySQL 里，每次启动全量读出、逐张 GPU 前向、存成内存列表线性比对。新模型拆为身份 `subjects`、模板 `face_templates`、特征 `face_embeddings`，外加为 ReID 预留的 `body_templates`。

`subjects`：`id`、`uuid`、`name`、`kind`（`known` 已登记人员 / `unknown` 系统自动创建的陌生人）、`external_id`（关联外部人事系统的可选字段）、`enrolled_at`、`active`、`merged_into`、`deleted_at`、`created_at`、`updated_at`。

`merged_into` 和 `deleted_at` 是软标记，由 invisiscan-main 设置。合并时，invisiscan-main 把被合并人的模板改到目标人名下，并在被合并人上设 `merged_into` 指向目标人；合并链在合并时压平，`merged_into` 总是指向一个没有被合并的人。识别日志由 A 写入，invisiscan-main 不修改，查询时按 `COALESCE(merged_into, subject_id)` 归并。删除只设 `deleted_at`，不执行 DELETE；物理删除由两边的清理任务各自执行，发布不同步 DELETE。

旧链路给陌生人生成 `陌生人<uuid>` 这样的名字塞进 `pictures.name`。新模型里陌生人是一个 `kind = 'unknown'` 的正常 `subject`，`name` 可为 NULL（展示时用 `uuid` 前缀代替），将来人工确认身份只需改 `name` 和 `kind`，不需要动特征数据。

`face_templates`：`id`、`uuid`、`subject_id`、`quality`（入库时的 FIQA 分，对应旧 `pictures.score`，迁移来的已登记人员模板可能为 NULL）、`object_key`（模板原图在云端对象存储中的键）、`source`（`legacy_import` / `auto_enroll` / `manual`）、`source_track_id`（可空，自动注册的模板来自哪条轨迹）、`legacy_picture_id`（迁移来源的旧 `pictures.id`，用于核对）、`active`、`deleted_at`、`created_at`。

`active` 只表示"启用"，由 invisiscan-main 控制，和有没有特征无关。原来的约束 `face_templates_active_has_embedding`（active 的模板必须有特征）随特征拆表删掉。invisiscan-main 上传模板时直接写 `active = true`，A 算好特征后只插入 `face_embeddings`，不回头更新模板行，这样模板行的更新只归 invisiscan-main 一方。识别只关联 `status = 'ready'` 的特征，所以特征算好之前，模板不会参与识别。

`face_embeddings`：`template_id`、`model_name`（如 `adaface_ir50_ms1mv2`）、`status`（`pending` / `ready` / `failed`）、`embedding vector(512)`、`norm`（归一化前的特征模长）、`attempts`、`error`（失败原因）、`created_at`、`updated_at`。主键 `(template_id, model_name)`。这张表只由 A 写入。

维度 512 对应 AdaFace ir_50 的输出。`model_name` 在主键里，一个模板可以有多个模型的特征，换模型升级时新旧特征共存：识别时按当前配置的模型名过滤，换模型不需要清库。`embedding` 只在 `ready` 时有值。A 每 5 秒轮询"没有当前模型 `face_embeddings` 行、或行仍是 `pending` 的模板"；计算出错时行保持 `pending`、`attempts` 加一、`error` 记原因，超过上限才置 `failed`。`failed` 的行必须写入，否则轮询会把失败的模板反复拿出来重算。

存的特征是 AdaFace `forward` 返回的单位向量（`net.py` 里已经做了 `x / ||x||`），所以两张模板的内积就是旧代码里的相似度。识别主路径在 Go 里算内积（见架构文档第 7 节），分数方向与旧链路相同，越大越像，阈值 0.35 可以直接沿用。只有在 SQL 里用 pgvector 运算符时才需要注意方向：`<=>` 返回余弦距离 `1 - sim`，`<#>` 返回负内积 `-sim`，两者都是越小越像。

`body_templates`：结构与 `face_templates` 平行，`embedding vector(768)` 待 ReID 模型确定后调整，当前为 ReID 预留，不写入数据。

取消的机制：启动时全量重算特征、`COUNT(*) + MAX(updated_at)` 版本号轮询、6 小时定时全量重载、无 `updated_at` 列时的 CRC32 回退分支。新链路每次识别读一次当前底库，底库变更立即可见，不存在缓存一致性问题。

`subjects` 和 `face_templates` 两边都会插入（A 自动注册陌生人，invisiscan-main 新建人员和上传模板），所以两边的自增 ID 错开：A 用奇数，云端用偶数，由 `migrations/site/{a,cloud}/10_id_sequences.sql` 设置。对外一律用 `uuid`。

## 识别日志

`recognition_logs` 对应旧 `logs` 表：`id`、`uuid`、`track_id`、`event_id`、`camera_id`、`location_id`、`subject_id`、`matched_template_id`（得分最高的那张模板，对应旧 `picture_id`）、`modality`（`face` / `body`，为 ReID 共用此表预留）、`occurred_at`、`action`（`enter` / `exit`）、`score`、`rounds`（1 或 2，是否走过 FIQA 第二轮）、`decision`、`created_at`。`UNIQUE (track_id, modality)` 保证重复投递只产生一条日志。

`decision` 的四个取值和 `subject_id` 的对应关系：

| decision | 含义 | subject_id | score |
|---|---|---|---|
| `matched` | 某轮得分超过阈值 | 命中的人 | 命中那轮的得分 |
| `enrolled` | 两轮都未命中，自动注册为陌生人 | 新建的 unknown subject | 第二轮得分 |
| `unmatched` | 两轮都未命中，最佳人脸质量 < 0.15 不注册 | NULL | 第二轮得分 |
| `no_face` | MTCNN 一张人脸都没对齐出来 | NULL | 0 |

`enrolled` 与旧链路有一处有意的差异：旧链路这时日志记 `unknown`、`picture_id` 为空，新建的陌生人和这次进门记录之间没有关联。新链路让日志直接指向新建的 subject，以后人工给这个陌生人命名，他的第一次进门记录也会跟着显示名字。`matched_template_id` 在 `enrolled` 时指向新建的模板。

与旧表的差异：

`date` 和 `time` 两列删除。旧表同时存 `date`、`time`、`occurred_at` 三个字段表示同一时刻，按日查询改用 `occurred_at` 上的索引配 `date_trunc`，或建生成列 `occurred_date date GENERATED ALWAYS AS ((occurred_at AT TIME ZONE 'Asia/Shanghai')::date) STORED` 并索引，兼顾查询便利与单一数据源。

`username` 列删除。旧表把名字快照进日志，和 `pictures.name` / `users.name` 构成三处来源。新模型通过 `subject_id` 关联取名。代价是改名会影响历史日志的展示——如果业务上要求日志反映"当时的名字"，再补一个 `subject_name_snapshot` 列，但这应该是明确的需求而不是默认行为。

`note` 列删除。旧链路在写入时判断 `occurred_at.time() <= 08:40:00` 决定"正常"还是"迟到"并固化进此列。新模型不存这个判断，改为查询时计算。配套加一张 `attendance_rules` 表（`id`、`location_id`、`late_after time`、`effective_from date`、`effective_to date`），迟到判定写成视图：

```sql
CREATE VIEW v_attendance AS
SELECT l.*, 
       (l.occurred_at AT TIME ZONE 'Asia/Shanghai')::time > r.late_after AS is_late
FROM recognition_logs l
JOIN attendance_rules r ON r.location_id = l.location_id
 AND (l.occurred_at AT TIME ZONE 'Asia/Shanghai')::date 
     BETWEEN r.effective_from AND COALESCE(r.effective_to, 'infinity'::date);
```

这样阈值调整、按地点差异化、历史规则变更都能表达，而旧模型改阈值会让新旧数据口径不一致。

`path` 列删除。旧表存识别时的人脸图目录绝对路径，新模型通过 `track_id` 关联 `track_images` 取图。

`action` 从中文字符串 `'进入'` 改为枚举值 `enter`。旧链路硬编码只写 `'进入'`，`exit` 为 ReID 出门场景预留。

`score` 语义需统一：旧链路存的是相似度（越大越像），迁移后继续存相似度而非距离，避免同一列前后语义不一致。`decision` 列显式记录判定结果，取代旧链路用 `username = 'unknown'` 隐式表达未识别。

## 视频归档

录像只存在 A 本地磁盘，不上云，保留 4 天（MediaMTX `recordDeleteAfter: 96h`）。`recordings` 行由 A 写入并复制到云端，云端的行可以保留得比文件久，invisiscan-main 据此显示"录像已过期"。

`recordings` 对应旧 `videos` 表：`id`、`camera_id`、`object_key`（唯一）、`started_at`、`ended_at`、`duration_seconds`、`size_bytes`、`deleted_at`、`created_at`。`location_id` 不再冗余存储，经 `cameras` 取得。旧 `videos` 不迁移，所以不需要区分来源的列。

旧表的 `duration` 直接写死环境变量 `VIDEO_DURATION_SECONDS`（3600），而不是实际时长，整点前启动或崩溃产生的短切片仍记 3600 秒，回放定位会偏。新模型的 `started_at` 从 MediaMTX 切片文件名解析，`duration_seconds` 取 hook 提供的 `MTX_SEGMENT_DURATION`，`ended_at = started_at + duration`，并加约束 `ended_at > started_at`。MediaMTX 按 `recordDeleteAfter`（4 天）删除旧切片后，Go 的启动扫描和定时扫描把找不到文件的行标 `deleted_at`，不删行，这样历史事件还能查到"当时有录像但已过期"。

`file_name` 和 `file_path` 两列合并为 `object_key`。旧表同时存文件名和绝对路径，前者可从后者推出。

回放定位某次事件：

```sql
SELECT r.object_key,
       EXTRACT(EPOCH FROM (e.started_at - r.started_at)) AS offset_seconds
FROM events e
JOIN recordings r ON r.camera_id = e.camera_id
 AND e.started_at < r.ended_at AND e.ended_at > r.started_at
WHERE e.id = $1
ORDER BY r.started_at;
```

事件跨切片时返回多行，前端按 `offset_seconds` 依次播放。`ON r.camera_id = e.camera_id` 加上时间区间重叠条件，建 `(camera_id, started_at, ended_at)` 索引支撑。

## 领域事件与心跳

这两张表只由 A 写入，复制到云端，供 invisiscan-main 和 invisiscan-agent 读取。

`domain_events`：`id`（bigint 自增）、`type`（如 `recognition.created`、`event.closed`、`subject.auto_enrolled`、`track.failed`、`reconcile.recovered`、`retention.purged`）、`aggregate_uuid`（相关事件、轨迹或人员的 uuid）、`payload jsonb`、`occurred_at`。业务操作在同一个事务里插入领域事件。消费方都在云端，按 `id` 维护游标，所以 id 小的事件必须先提交：写入方插入前先取 `pg_advisory_xact_lock(<固定键>)`，事务提交才释放，id 顺序就等于提交顺序。云端订阅不开并行应用，按提交顺序写入，云端看到的 id 顺序同样可靠。A 本地保留 7 天，云端保留期由 invisiscan-main 决定。

`pipeline_heartbeats`：`id`、`instance_id`、`version`、`camera_ids`（本实例负责的相机）、`camera_status jsonb`（每个相机的拉流状态、最后一帧时间、最后一次 RTCP SR 时间）、`services jsonb`（各推理服务是否连通）、`pending_tasks`、`pending_uploads`、`disk_used_ratio`、`reported_at`。pipeline 每 10 秒插入一行，不做 upsert：逻辑复制对反复更新同一行不友好，只插入还能保留在线历史。当前状态取每个 `instance_id` 最新的一行。云端看到的心跳时间包含复制延迟，判断离线的阈值要留余量（例如 60 秒）。A 和云端各自保留 24 小时后删除。

`ops_commands` / `ops_command_results` 只预留，现在不建表：以后 invisiscan-agent 要在 A 上执行操作（例如重启某个服务）时，由云端写入 `ops_commands` 并复制到 A，A 上的执行器轮询并只执行白名单里的动作，结果写回 `ops_command_results`，再复制到云端。表结构在阶段五定。

## 旧表到新表的映射

| 旧 | 新 | 关键变化 | 是否迁移数据 |
|---|---|---|---|
| `events` | `events` | 加 `status` / `closed_reason` / `uuid`，起止时间语义不变 | 否 |
| `person_tracks` | `tracks` | 加状态机；`track_key` → `uuid`；`image_path` → `track_images` 关联 | 否 |
| `person_tracks.category` | `tracks.category` + `category_probs` | 标签不变，概率新增入库 | 否 |
| 轨迹 `.txt` 文件 | `track_points` | 文件行变表行，去掉逐行冗余的 `orig_h,orig_w` | 否 |
| `pictures` | `subjects` + `face_templates` + `face_embeddings` | 身份、模板、特征拆分；BLOB → `vector` + 对象存储 | **是** |
| `pictures.name`（陌生人名）| `subjects.name` + `kind='unknown'` | 不再用名字字段承载身份状态 | **是** |
| `users` | `subjects`（`kind='known'`）| 合并进统一身份表 | **是**（只迁有人脸图的） |
| `logs` | `recognition_logs` + `attendance_rules` 视图 | 去三时间列冗余、去 `note` 固化判断、去路径列 | 否 |
| `videos` | `recordings` | `duration` 改为实测、路径合并为 `object_key` | 否 |
| （无）| `locations` / `cameras` | env 硬编码的维度升为真表 | 种子数据 |
| Redis `outdoor_master` | `tracks.status='pending_reid'` | 队列状态化，见 ReID 预留 | — |

旧 MySQL `laboratory` 库在切换后保留为只读，旧的事件、轨迹、日志和视频记录需要查时直接查旧库。新库从切换时刻开始记录。

## 人脸底库迁移

迁移脚本放在 `python/tools/migrate_faces.py`，在 A 上用 FaceService 的环境运行。必须在 A 上运行，因为 `face_embeddings` 只能由 A 写入；脚本写 A 本地库，`subjects`、`face_templates` 用 A 的奇数 ID，三张表都经逻辑复制同步到云端。脚本直接 import FaceService 内部的对齐和特征函数，保证迁移出的特征与线上识别用的是同一套代码、同一份权重（`adaface_ir50_ms1mv2.ckpt`）。脚本逻辑复刻旧 `load_faces`：

1. 读取 `SELECT p.id, p.user_id, p.name, u.name, p.face_image, p.score FROM pictures p LEFT JOIN users u ON p.user_id = u.id`。旧代码用的就是这个连接，只是把 `COALESCE` 拆开，以便分别知道两个名字。
2. 身份映射：`user_id` 非空的，每个 `users.id` 建一个 `kind = 'known'` 的 subject，`name = users.name`，`external_id = users.id`。`user_id` 为空的，每张图建一个 `kind = 'unknown'` 的 subject；`pictures.name` 形如 `陌生人<uuid>` 时，把其中的 uuid 作为 subject 的 `uuid`，`name` 置 NULL，这样旧日志里的陌生人名还能对回新 subject；其他名字原样保留，`kind` 仍为 `unknown`，留给人工确认。
3. 对每张 BLOB 先做 MTCNN 对齐，再算 AdaFace 特征，与旧 `load_faces` 的 `align.get_aligned_face` → `model(to_input(...))` 一致。对齐成功的写一条 `status = 'ready'` 的 `face_embeddings` 行。对齐失败的图旧链路会跳过（打印"无人脸"），所以它们实际上从来不在旧底库里。新库照样写入模板行（`active = true`），再写一条 `status = 'failed'`、`error` 记录原因的 `face_embeddings` 行，保留原图，方便人工处理。识别只用 `ready` 的特征，所以这些模板不参与识别。
4. 原图写入云端对象存储 `subjects/{subject_uuid}/{template_uuid}.jpg`，`source = 'legacy_import'`，`legacy_picture_id = pictures.id`，`quality` 取 `pictures.score`（能解析为数字时）。
5. 整个迁移在一个事务里完成，可重复执行：按 `legacy_picture_id` 唯一约束做 upsert。

迁移后的校验：随机取 20 张旧图，用旧 `AdaFace-master` 代码算一次特征，和新库 `face_embeddings` 里对应的 `embedding` 求余弦相似度，要求全部大于 0.9999。另外挑 5 条旧链路识别成功的轨迹，用新识别代码重算，要求命中同一个人、分数差小于 1e-4。

## 典型查询

**按时间段查事件及人员**

```sql
SELECT e.id, e.started_at, e.ended_at, e.track_count,
       t.uuid AS track_uuid, t.category, 
       s.name AS subject_name, rl.score, rl.decision
FROM events e
JOIN tracks t ON t.event_id = e.id
LEFT JOIN recognition_logs rl ON rl.track_id = t.id
LEFT JOIN subjects s ON s.id = rl.subject_id
WHERE e.camera_id = 1
  AND e.started_at >= '2026-11-01' AND e.started_at < '2026-12-01'
ORDER BY e.started_at, t.started_at;
```

**读取识别底库**

```sql
SELECT ft.id, ft.subject_id, fe.embedding
FROM face_templates ft
JOIN subjects s ON s.id = ft.subject_id
 AND s.active AND s.deleted_at IS NULL AND s.merged_into IS NULL
JOIN face_embeddings fe ON fe.template_id = ft.id
 AND fe.model_name = $1 AND fe.status = 'ready'
WHERE ft.active AND ft.deleted_at IS NULL;
```

被合并人的模板在合并时已经改到目标人名下，所以 `s.merged_into IS NULL` 不会漏掉模板，只是防止合并过程中的中间状态。

Go 侧拿到全部模板后按旧 `compute_user` 口径打分：查询特征矩阵 `Q`（n×512）乘模板矩阵转置，按列求均值，取最大列。

**同口径的数据库内算法（用于校验 Go 实现）**

```sql
-- $2 是 n 张查询特征的均值向量（未归一化）
SELECT ft.id, ft.subject_id, -(fe.embedding <#> $2) AS score
FROM face_templates ft
JOIN subjects s ON s.id = ft.subject_id
 AND s.active AND s.deleted_at IS NULL AND s.merged_into IS NULL
JOIN face_embeddings fe ON fe.template_id = ft.id
 AND fe.model_name = $1 AND fe.status = 'ready'
WHERE ft.active AND ft.deleted_at IS NULL
ORDER BY fe.embedding <#> $2
LIMIT 1;
```

由于 `mean_i(q_i · t) = (mean_i q_i) · t`，这条查询的结果应与 Go 侧完全一致。`<#>` 返回负内积，所以取反得到分数。

**某日考勤汇总**

```sql
SELECT s.uuid, s.name,
       MIN(rl.occurred_at) AS first_seen,
       (MIN(rl.occurred_at) AT TIME ZONE 'Asia/Shanghai')::time > r.late_after AS is_late
FROM recognition_logs rl
JOIN subjects orig ON orig.id = rl.subject_id
JOIN subjects s ON s.id = COALESCE(orig.merged_into, orig.id)   -- 合并后归到目标人
JOIN attendance_rules r ON r.location_id = rl.location_id
 AND (rl.occurred_at AT TIME ZONE 'Asia/Shanghai')::date 
     BETWEEN r.effective_from AND COALESCE(r.effective_to, 'infinity'::date)
WHERE rl.decision = 'matched'
  AND rl.modality = 'face'
  AND rl.occurred_date = '2026-11-26'
  AND s.deleted_at IS NULL
GROUP BY s.id, s.uuid, s.name, r.late_after
ORDER BY first_seen;
```

识别日志里的 `subject_id` 是识别当时的人，invisiscan-main 合并人员后不改日志，所以汇总前先按 `COALESCE(merged_into, subject_id)` 归并。合并链在合并时压平，一层 `COALESCE` 就够。

**恢复未处理轨迹（启动时 reconcile）**

```sql
-- $owned：本进程用 pg_try_advisory_lock(camera_id) 拿到的相机
-- 1. 截断上次运行残留的进行中轨迹
UPDATE tracks t
SET status = 'ended', end_reason = 'recovered',
    ended_at = (SELECT max(frame_time) FROM track_points p WHERE p.track_id = t.id),
    locked_by = NULL, locked_until = NULL, next_attempt_at = now(),
    updated_at = now()
WHERE status = 'confirmed'
  AND camera_id = ANY($owned);

-- 2. 关闭残留的进行中事件
UPDATE events e
SET status = 'closed', closed_reason = 'recovered',
    ended_at = (SELECT max(t.ended_at) FROM tracks t WHERE t.event_id = e.id)
WHERE status = 'open'
  AND camera_id = ANY($owned);

-- 3. 需要重新投递的轨迹
SELECT id, status, category FROM tracks
WHERE camera_id = ANY($owned)
  AND (    (status = 'ended')
        OR (status = 'classified' AND category = 'indoor')
        OR (status = 'pending_reid' AND $reid_enabled::bool))
ORDER BY ended_at;
```

三条 SQL 都限定在本进程拥有的相机上。只要出现第二个 pipeline 进程（第二台算力主机，或 A 上按相机拆分进程），不加这个条件，任何一个进程重启都会关掉另一个进程正在进行的事件。第 3 步只决定"要不要重投"，真正执行时 worker 仍按 `tracks` 上的任务认领列取任务，所以租约过期的任务也会被别的 worker 接手。

第 1、2 步在同一个事务里执行，先轨迹后事件，事件的 `ended_at` 才能用到刚补齐的轨迹时间。第 3 步的结果就是恢复队列的全部来源，取代了旧的 Redis `missing_tasks` / `adaface_master` / `outdoor_master` 三个队列。`classified` 且 `pass` 的轨迹不会出现，因为分类时 pass 直接写 `done`；`classified` 且 `outdoor` 也不会出现，分类时直接写 `pending_reid`。
