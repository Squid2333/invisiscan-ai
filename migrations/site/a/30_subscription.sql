-- 仅在 A 执行，用来订阅云端的 pub_cloud。
--
-- 以下步骤开始前，两站点均已执行 0001_init.sql、10_id_sequences.sql
-- 和 20_publication.sql，云端还执行过 0002_seed.sql。
-- 1. 云端执行 site/cloud/30_subscription.sql（copy_data = false）。
-- 它会在 A 写入业务数据前创建复制槽，开始记录变更。
-- 2. A 执行本文件（copy_data = true），初始同步会复制云端种子数据
-- 以及 invisiscan-main 已经写入的记录。
-- 3. 等所有表完成同步后再启动 pipeline。下列查询的 srsubstate 应全为 r。
--      SELECT srrelid::regclass, srsubstate FROM pg_subscription_rel;
--
-- 第 2 步复制到 A 的记录不会发回云端。
-- 云端订阅跳过已有数据，origin = none 也会跳过
-- A 从云端收到的变更。
--
-- CREATE SUBSCRIPTION 可能警告 copy_data 与 origin = none 一起使用时
-- 会复制来自其他站点的数据，因为云端的 subjects 和 face_templates
-- 也由 sub_a 写入。按上述顺序执行时，A 尚未写入业务数据，
-- 云端因此没有来自 A 的记录。
--
-- 通过 psql 变量传入连接字符串。
-- 密码写在 A 上 postgres 用户的 ~/.pgpass 中。
--   psql -d invisiscan -v cloud_conninfo='host=10.8.0.1 port=5432 dbname=invisiscan user=invisiscan_repl' \
--        -f 30_subscription.sql

CREATE SUBSCRIPTION sub_cloud
    CONNECTION :'cloud_conninfo'
    PUBLICATION pub_cloud
    WITH (
        origin    = none,  -- 忽略云端从 A 收到的变更
        copy_data = true,
        streaming = on     -- 按提交顺序应用记录，避免使用 parallel
    );
