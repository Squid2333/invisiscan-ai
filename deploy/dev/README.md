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

`dev-up` 生成 Go/Python stubs，并启动本机 PostgreSQL 与固定响应的 PerceptionService。PostgreSQL 首次初始化时创建 `invisiscan_a` 和 `invisiscan_cloud` 两个数据库；两边执行基础迁移，云端库额外执行 `0002_seed.sql`。首次启动后运行 `make dev-replication`，它按 `migrations/site/README.md` 配置 publication/subscription 并等待初始同步完成。开发副本角色密码固定为 `invisiscan_repl`，仅用于本机容器。连接地址只绑定到 loopback：PostgreSQL `127.0.0.1:54329`，gRPC `127.0.0.1:50051`。开发账号为 `invisiscan`，密码同名。

SQL 查询按表/领域放在 `go/internal/store/query/`，读写方法由 sqlc 生成；事务编排留在 `store`，轨迹点和图片的批量写入使用 pgx `CopyFrom`。修改查询后运行 `make sqlc-gen`。Makefile 通过 `go run` 调用固定版本 sqlc，无需全局安装。

运行 Go pipeline 骨架时，它会连接 A 库、打开 `Perceive` 流、发送一帧 1×1 假图，并校验收到固定 `SessionReady` 与 `FrameResult`。按 Ctrl-C 退出。`make dev-down` 停止容器但保留数据库卷；容器重启不会重复执行初始化迁移。

`go test ./go/internal/store` 默认跳过数据库测试；设置 `INVISISCAN_TEST_DSN`（A）和 `INVISISCAN_CLOUD_TEST_DSN` 可运行真实 PostgreSQL 数据层读写测试。另设置 `INVISISCAN_REPLICATION_TEST=1` 和 `INVISISCAN_A_TEST_DSN` 可运行双库写入方、奇偶 ID、`origin=none` 和断连追平测试。开发容器 DSN 分别为 `postgres://invisiscan:invisiscan@127.0.0.1:54329/invisiscan_a` 与 `postgres://invisiscan:invisiscan@127.0.0.1:54329/invisiscan_cloud`。

站点复制只在全新开发数据库、且 pipeline 尚未写入业务数据前初始化一次。持久卷已初始化后，不能重复运行 `make dev-replication`；需要清空本地开发卷后重新初始化。
