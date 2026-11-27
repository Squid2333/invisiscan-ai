# AGENT.md

本文件写给在本仓库里写代码的人和 AI 助手。先读设计文档，再动代码。

## 先读什么

| 文档 | 内容 |
|---|---|
| `docs/ARCHITECTURE.md` | 拓扑、帧时间、状态机、存储、链路时序、仓库结构 |
| `docs/DATA_MODEL.md` | 每张表的字段语义、双库与写入方、典型查询 |
| `docs/IMPLEMENTATION_STAGES.md` | 分阶段实施步骤、验收、难点和待决问题 |
| `docs/IMPLEMENTATION_PLAN.md` | 技术栈、A 与云端的部署和同步、运维 |
| `docs/ADMIN_API.md` | invisiscan-main 的接口和跨项目契约。本项目不实现这些接口 |
| `migrations/` | DDL 是 schema 的唯一来源，sqlc 从这里生成代码 |
| `proto/invisiscan/v1/` | Go 和 Python 之间唯一的共享物 |

代码和文档不一致时，先确认哪边对，再把另一边改过来，不要让两者长期分叉。

## 写代码的原则

**代码简洁，直接表达意图。** 一个函数做一件事，读起来不需要跳很多层。三行重复好过一个只用一次的抽象。只在文档已经定义的地方建接口（`BlobStore`、`TaskQueue`、gRPC 服务），不为"以后可能换实现"预先加接口、配置项或插件点。

**相信上下游。** 系统内部的数据由我们自己的代码产生，并且已经有一层负责保证它的正确性，下游就不再重复检查：

- 数据库约束保证的事（状态取值、非空、`ready` 的特征一定有 embedding），读出来后不再判断。
- PerceptionService 和 FaceService 返回的结果按 proto 契约直接用，不检查维度、范围或是否归一化。
- 状态机的条件更新（`UPDATE ... WHERE status = 'ended'`）和唯一约束负责幂等，调用方不先查一遍再写。
- 配置在启动时由 `internal/config` 校验一次，之后各处直接读取。

只在信任边界校验输入：RTSP 流和帧时间、MediaMTX 回调（校验共享 token）、从对象存储下载的图片、配置文件。

**不写过度的防御性代码。** 具体是：

- 不为不可能发生的情况写分支。例如 `tracks.event_id` 是 NOT NULL，就不要处理它为空。
- 错误向上返回，不吞掉、不改成默认值继续跑。Go 用 `fmt.Errorf("...: %w", err)` 在边界处补一次上下文；Python 不在模型调用外面包 `try/except`，异常让 gRPC 变成错误状态返回。
- 进程级的意外直接退出。崩溃恢复由启动时的 reconcile 负责（DATA_MODEL "恢复未处理轨迹"），不在每个调用点自己补偿。
- 重试只放在设计里规定的地方：post-track 任务认领的指数退避、`face_embeddings.attempts`、上传任务。其他地方不加重试循环。
- 不加"以防万一"的空值检查、类型断言、日志刷屏。

**易于维护。**

- 命名用文档里的术语（event、track、subject、template、embedding、reconcile），一个概念只用一个词。
- 注释写"为什么"，尤其是与旧链路对齐的决定（例如 `frame_rate = 15` 而不是 8），不复述代码做了什么。
- 阈值和算法参数进配置，并在配置定义处注明旧链路的出处。
- 改动保持小而聚焦，不顺手重构无关代码。

## 必须守住的约束

- **每张表只有一个更新方。** A 只写 `events`、`tracks`、`track_points`、`track_images`、`recognition_logs`、`recordings`、`face_embeddings`、`domain_events`、`pipeline_heartbeats`，以及插入（不更新）自动注册的 `subjects` 和 `face_templates`。不要在 A 上更新 `cameras`、`subjects`、`face_templates`。完整表格见 DATA_MODEL "双库与写入方"。
- **只做软删除。** `subjects`、`face_templates` 用 `deleted_at`，不执行 DELETE。发布不同步 DELETE。
- **迁移在两边都执行，只前进。** 按"先扩展、后收缩"写：先加列或加表，再上线代码，删列放到后续版本。新增复制表要同时改 `migrations/site/` 下的 publication。`0002_seed.sql` 只在云端执行。
- **时间统一用 `timestamptz`、UTC 处理。** 帧时间来自 RTCP SR，不从画面读，不用墙钟替代（`fallback_local` 除外，见 ARCHITECTURE §4）。
- **对象键不携带语义，一次写入后不移动。** 数据库只存对象键，不存绝对路径。
- **本项目不对外提供业务接口。** `internal/api` 只有基础设施端点（ARCHITECTURE §9）。
- **阶段二到四以与旧链路一致为验收标准。** 移植时保留旧行为，包括看起来奇怪的行为（文档里都标了出处）。想改算法，单独提出，放到阶段七。

## 仓库约定

- `legacy/` 是旧链路代码，重构期间保持原样、可运行，用来生成回归比对的黄金数据。不要修改，不要删除。
- 模型权重和 `testdata/` 不进 Git。
- Go：pgx v5 + sqlc，`log/slog` 输出 JSON 到 stdout，不直接写日志表。
- Python：3.10，每个服务一个容器，依赖用 uv 锁文件，FaceService 写死 torch 版本。旧项目的模型代码原样复制，只改导入路径和权重路径。
- proto：改动要通过 `buf lint` 和 `buf breaking`。

## 测试

- 测试覆盖行为和与旧链路的一致性，不为每个私有函数写测试。
- 跟踪器用旧 `RGBsort.py` 录制的 JSONL 黄金数据做逐帧回放（IMPLEMENTATION_STAGES 阶段三），纯数学部分进 CI。
- 数据库相关的测试用 testcontainers 起真实的 PostgreSQL，不 mock 数据库。双库复制有一个集成测试。
