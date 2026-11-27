#!/usr/bin/env bash
set -euo pipefail

# The development container uses one PostgreSQL server with two databases. This
# is intentionally the same publication/subscription order used by two hosts;
# only the connection addresses differ. The script runs once on a fresh volume.
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

# Site migrations deliberately keep credentials out of source control. The
# development-only role has a fixed password so the two databases can connect
# over the container's loopback address.
"${psql_admin[@]}" --dbname=invisiscan_cloud \
	--command="ALTER ROLE invisiscan_repl PASSWORD 'invisiscan_repl';"
"${psql_admin[@]}" --dbname=invisiscan_a \
	--command="ALTER ROLE invisiscan_repl PASSWORD 'invisiscan_repl';"

# With both databases in one PostgreSQL cluster, CREATE SUBSCRIPTION cannot
# create its publisher slot in the same command: slot creation waits for the
# CREATE SUBSCRIPTION transaction itself. Pre-create the slots while no
# business writes have started, then reference them with create_slot=false.
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
