#!/usr/bin/env bash
set -euo pipefail

createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" invisiscan_a
createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" invisiscan_cloud

for database in invisiscan_a invisiscan_cloud; do
	psql --username="$POSTGRES_USER" --dbname="$database" --set=ON_ERROR_STOP=1 \
		--file=/migrations/0001_init.sql
	if [[ "$database" == invisiscan_cloud ]]; then
		psql --username="$POSTGRES_USER" --dbname="$database" --set=ON_ERROR_STOP=1 \
			--file=/migrations/0002_seed.sql
	fi
	psql --username="$POSTGRES_USER" --dbname="$database" --set=ON_ERROR_STOP=1 \
		--file=/migrations/0003_contract_checks.sql
done
