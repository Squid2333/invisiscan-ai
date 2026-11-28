-- 仅在云端执行，用来订阅 A 的 pub_a。
-- 先执行本文件，再执行 site/a/30_subscription.sql。
-- 完整顺序见 A 的订阅脚本。
--
-- copy_data = false。趁 A 尚无业务数据时执行，不复制存量数据。
-- 在 A 上创建的复制槽会记录之后的每次变更。
-- 如果复制存量数据，A 初始同步时从云端收到的记录可能被发回，
-- 导致主键冲突。
--
-- 通过 psql 变量传入连接字符串。
-- 密码写在云端 postgres 用户的 ~/.pgpass 中。
--   psql -d invisiscan -v a_conninfo='host=10.8.0.2 port=5432 dbname=invisiscan user=invisiscan_repl' \
--        -f 30_subscription.sql

CREATE SUBSCRIPTION sub_a
    CONNECTION :'a_conninfo'
    PUBLICATION pub_a
    WITH (
        origin    = none,  -- 忽略 A 从云端收到的变更
        copy_data = false,
        streaming = on     -- 按提交顺序应用记录，保持 domain_events 游标有效
    );
