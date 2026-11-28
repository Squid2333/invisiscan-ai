# 本地开发环境

需要 Docker、Go 1.23+、Node.js/npm 和 Python 3.10。仓库根目录的 `buf.yaml`、`buf.gen.yaml` 定义 proto 检查与代码生成；本机已安装全局 Buf 时可直接运行 Make 命令，否则默认通过 `npx buf` 调用。

```sh
make proto-lint
make proto-breaking
make dev-up
make dev-replication
make test-store
make dev-pipeline
```

`dev-up` 生成 Go/Python stubs，并启动本机 PostgreSQL 与按帧号返回检测的 mock PerceptionService。PostgreSQL 首次初始化时创建 `invisiscan_a` 和 `invisiscan_cloud` 两个数据库；两边执行基础迁移，云端库额外执行 `0002_seed.sql`。首次启动后运行 `make dev-replication`，它按 `migrations/site/README.md` 配置 publication/subscription 并等待初始同步完成。开发副本角色密码固定为 `invisiscan_repl`，仅用于本机容器。连接地址只绑定到 loopback，PostgreSQL 使用 `127.0.0.1:54329`，gRPC 使用 `127.0.0.1:50051`。开发账号为 `invisiscan`，密码同名。

SQL 查询按表/领域放在 `go/internal/store/query/`，读写方法由 sqlc 生成；事务编排留在 `store`，轨迹点和图片的批量写入使用 pgx `CopyFrom`。修改查询后运行 `make sqlc-gen`。Makefile 通过 `go run` 调用固定版本 sqlc，无需全局安装。

`make dev-pipeline` 向 mock PerceptionService 以 8 FPS 发送 70 帧黑色 BGR 图，约 9 秒后退出。假图尺寸取 A 库中 `mock_camera_id` 对应的已启用相机，默认是云端 seed 复制来的相机 1。帧时间从本次回放起点按 125 毫秒递增，模拟 RTCP 帧时间。首次完整回放写入一条 `closed` 事件、两条 `ended` 轨迹和八个轨迹点。提前按 Ctrl-C 会刷盘并以 `shutdown` 结束正在进行的事件。

每个相机的回放标识默认为 `default`。重启时，同一回放标识下已经落库的事件会跳过，确认事务尚未提交的事件可以在回放时重新确认。要用同样的固定帧创建一批新的样例数据，运行 `INVISISCAN_MOCK_REPLAY_ID=demo-2 make dev-pipeline`。

启动时先尝试在 A 库取得相机的会话级 advisory lock。另一实例持有该相机时，本次启动报错退出。恢复与实时写入使用持锁的同一条连接。恢复事务按相机结束残留的 `confirmed` 轨迹、关闭 `open` 事件，结束时间取已落库的最后一帧。恢复后的 `ended` 轨迹留在数据库中等待分类。

在仓库根目录运行以下命令，检查最新一次回放。默认事件持续 875 毫秒，关闭原因为 `absence`，两条轨迹各有 5 点和 3 点。

```sh
docker compose -f deploy/dev/compose.yaml exec -T postgres psql -U invisiscan -d invisiscan_a <<'SQL'
WITH latest AS (
  SELECT * FROM events WHERE camera_id = 1 ORDER BY id DESC LIMIT 1
)
SELECT e.id, e.status, e.closed_reason, e.ended_at - e.started_at AS duration,
       t.tracker_id, t.status AS track_status, t.frame_count,
       p.seq, p.frame_time, p.bbox_x, p.bbox_y, p.bbox_w, p.bbox_h, p.depth
FROM latest e JOIN tracks t ON t.event_id = e.id
JOIN track_points p ON p.track_id = t.id
ORDER BY t.tracker_id, p.seq;
SQL
```

启动开发容器并初始化复制后，可以用独立测试相机逐帧核对落库和查询结果。

```sh
INVISISCAN_TEST_DSN='postgres://invisiscan:invisiscan@127.0.0.1:54329/invisiscan_a?sslmode=disable' \
INVISISCAN_CLOUD_TEST_DSN='postgres://invisiscan:invisiscan@127.0.0.1:54329/invisiscan_cloud?sslmode=disable' \
INVISISCAN_TEST_PERCEPTION_TARGET='127.0.0.1:50051' \
go test ./go/internal/pipeline -run '^TestMockRealtimePostgres$' -count=1 -v
```

`make test-recovery` 使用 Testcontainers 启动 PostgreSQL 和 Python mock，无需先运行 `make dev-up`。测试在确认写点、追加写点、关闭事件时终止 pipeline 子进程，随后重启两次并核对持锁范围、事务回滚、待分类轨迹和数据去重。同一命令还用两个独立 PostgreSQL 容器执行站点迁移和双向复制测试。

`make dev-down` 停止容器并保留数据库卷。容器重启会继续使用已初始化的数据库。

`go test ./go/internal/store` 默认跳过数据库测试；设置 `INVISISCAN_TEST_DSN`（A）和 `INVISISCAN_CLOUD_TEST_DSN` 可运行真实 PostgreSQL 数据层读写测试。另设置 `INVISISCAN_REPLICATION_TEST=1` 和 `INVISISCAN_A_TEST_DSN` 可运行双库写入方、奇偶 ID、`origin=none` 和断连追平测试。开发容器 DSN 分别为 `postgres://invisiscan:invisiscan@127.0.0.1:54329/invisiscan_a` 与 `postgres://invisiscan:invisiscan@127.0.0.1:54329/invisiscan_cloud`。

站点复制只在全新开发数据库、且 pipeline 尚未写入业务数据前初始化一次。持久卷已初始化后，不能重复运行 `make dev-replication`；需要清空本地开发卷后重新初始化。
