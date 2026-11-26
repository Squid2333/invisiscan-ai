# 按站点执行的迁移

`migrations/` 下的编号迁移在主机 A 和云端都执行，`0002_seed.sql` 除外，它只在云端执行。本目录下的脚本只在对应站点执行一次，用于双库复制的初始化。

| 顺序 | 云端 | 主机 A |
|---|---|---|
| 1 | `0001_init.sql`、`0002_seed.sql` | `0001_init.sql` |
| 2 | `cloud/10_id_sequences.sql`：`subjects`、`face_templates` 用偶数 ID | `a/10_id_sequences.sql`：用奇数 ID |
| 3 | `cloud/20_publication.sql`：建 `pub_cloud` | `a/20_publication.sql`：建 `pub_a` |
| 4 | `cloud/30_subscription.sql`：订阅 `pub_a`，`copy_data = false` | |
| 5 | | `a/30_subscription.sql`：订阅 `pub_cloud`，`copy_data = true` |
| 6 | | 等 `pg_subscription_rel` 全部为 `r`，再启动 pipeline |

第 4 步必须在第 5 步之前，并且这时 A 上还没有写入任何业务数据。原因见 `a/30_subscription.sql` 的注释。

前提：两边的 `postgresql.conf` 已设 `wal_level = logical` 和 `max_slot_wal_keep_size`，并且 `pg_hba.conf` 只对对端的隧道 IP 放行 `invisiscan_repl`（见 `docs/IMPLEMENTATION_PLAN.md` 4.1、4.3 节）。复制账号的密码放在 `~/.pgpass`，不写进脚本。
