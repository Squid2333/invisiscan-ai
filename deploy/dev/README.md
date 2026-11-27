# 本地开发环境

需要 Docker、Go 1.23+、Node.js/npm 和 Python 3.10。仓库根目录的 `buf.yaml`、`buf.gen.yaml` 定义 proto 检查与代码生成；本机已安装全局 Buf 时可直接运行 Make 命令，否则默认通过 `npx buf` 调用。

```sh
make proto-lint
make proto-breaking
make dev-up
make dev-pipeline
```

`dev-up` 生成 Go/Python stubs，并启动本机 PostgreSQL 与固定响应的 PerceptionService。PostgreSQL 首次初始化时创建 `invisiscan_a` 和 `invisiscan_cloud` 两个数据库；两边执行基础迁移，云端库额外执行 `0002_seed.sql`。连接地址只绑定到 loopback：PostgreSQL `127.0.0.1:54329`，gRPC `127.0.0.1:50051`。开发账号为 `invisiscan`，密码同名。

修改 SQL 查询后运行 `make sqlc-gen`。Makefile 通过 `go run` 调用固定版本 sqlc，无需全局安装。

运行 Go pipeline 骨架时，它会连接 A 库、打开 `Perceive` 流、发送一帧 1×1 假图，并校验收到固定 `SessionReady` 与 `FrameResult`。按 Ctrl-C 退出。`make dev-down` 停止容器但保留数据库卷；容器重启不会重复执行初始化迁移。

当前两个数据库仅用于本地模式验证，尚未创建站点 publication/subscription，也不代表双库复制测试已通过。
