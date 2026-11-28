#!/usr/bin/env bash
set -euo pipefail

# 开发容器用一个 PostgreSQL 实例运行两个数据库。
# 发布和订阅的建立顺序与双主机部署一致，只有连接地址不同。
# 此脚本只在新数据卷上运行一次。
psql_admin=(psql --username="$POSTGRES_USER" --set=ON_ERROR_STOP=1)

"${psql_admin[@]}" --dbname=invisiscan_cloud --file=/migrations/site/cloud/10_id_sequences.sql
"${psql_admin[@]}" --dbname=invisiscan_a --file=/migrations/site/a/10_id_sequences.sql
if [[ $("${psql_admin[@]}" --dbname=invisiscan_cloud --tuples-only --no-align \
	--command="SELECT EXISTS(SELECT 1 FROM pg_publication WHERE pubname='pub_cloud');") != "t" ]]; then
	"${psql_admin[@]}" --dbname=invisiscan_cloud --file=/migrations/site/cloud/20_publication.sql
fi
if [[ $("${psql_admin[@]}" --dbname=invisiscan_a --tuples-only --no-align \
	--command="SELECT EXISTS(SELECT 1 FROM pg_publication WHERE pubname='pub_a');") != "t" ]]; then
	"${psql_admin[@]}" --dbname=invisiscan_a --file=/migrations/site/a/20_publication.sql
fi

# 站点迁移不会把连接凭据写入版本库。
# 开发环境使用固定密码，让两个数据库通过容器内的回环地址连接。
# 这个密码仅用于开发容器。
"${psql_admin[@]}" --dbname=invisiscan_cloud \
	--command="ALTER ROLE invisiscan_repl PASSWORD 'invisiscan_repl';"
"${psql_admin[@]}" --dbname=invisiscan_a \
	--command="ALTER ROLE invisiscan_repl PASSWORD 'invisiscan_repl';"

# 两个数据库共用一个 PostgreSQL 集群时，不能让 CREATE SUBSCRIPTION 同时创建发布端的复制槽。
# 创建槽会等待 CREATE SUBSCRIPTION 自己的事务结束。
# 因此在业务数据写入前先创建槽，
# 再通过 create_slot=false 引用已有的槽。
"${psql_admin[@]}" --dbname=invisiscan_a \
	--command="SELECT pg_create_logical_replication_slot('sub_a', 'pgoutput');"
"${psql_admin[@]}" --dbname=invisiscan_cloud \
	--command="SELECT pg_create_logical_replication_slot('sub_cloud', 'pgoutput');"

"${psql_admin[@]}" --dbname=invisiscan_cloud \
	--command="CREATE SUBSCRIPTION sub_a CONNECTION 'host=127.0.0.1 port=5432 dbname=invisiscan_a user=invisiscan_repl password=invisiscan_repl' PUBLICATION pub_a WITH (origin=none, copy_data=false, streaming=on, create_slot=false);"
"${psql_admin[@]}" --dbname=invisiscan_a \
	--command="CREATE SUBSCRIPTION sub_cloud CONNECTION 'host=127.0.0.1 port=5432 dbname=invisiscan_cloud user=invisiscan_repl password=invisiscan_repl' PUBLICATION pub_cloud WITH (origin=none, copy_data=true, streaming=on, create_slot=false);"

for database in invisiscan_a invisiscan_cloud; do
	for _ in $(seq 1 60); do
		ready=$("${psql_admin[@]}" --dbname="$database" --tuples-only --no-align \
			--command="SELECT count(*) > 0 AND bool_and(srsubstate='r') FROM pg_subscription_rel;")
		if [[ "$ready" == "t" ]]; then break; fi
		sleep 1
	done
	if [[ "${ready:-f}" != "t" ]]; then
		echo "logical subscription table sync did not finish for $database" >&2
		exit 1
	fi
done
