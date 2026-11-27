.PHONY: contract-check migrate-check proto-lint proto-breaking proto-gen sqlc-gen dev-up dev-down dev-pipeline test-store dev-replication dev-replication-check

BUF_BREAKING_AGAINST ?= .git\#branch=main,subdir=proto
BUF ?= npx buf
SQLC ?= go run github.com/sqlc-dev/sqlc/cmd/sqlc@v1.29.0

contract-check: migrate-check

migrate-check:
	python3 tools/check_migrations.py

proto-lint:
	@$(BUF) lint

proto-breaking:
	@$(BUF) breaking --against '$(BUF_BREAKING_AGAINST)'

proto-gen:
	@$(BUF) generate

sqlc-gen:
	$(SQLC) generate

dev-up: proto-gen
	docker compose -f deploy/dev/compose.yaml up -d --build postgres perception-mock

dev-down:
	docker compose -f deploy/dev/compose.yaml down

dev-pipeline:
	go run ./go/cmd/pipeline -config config/dev.yaml

test-store:
	go test ./go/internal/store -count=1

dev-replication:
	docker compose -f deploy/dev/compose.yaml exec -T postgres bash /dev-init/02-init-replication.sh

dev-replication-check:
	docker compose -f deploy/dev/compose.yaml exec -T postgres psql -U invisiscan -d invisiscan_a -c "SELECT subname, subenabled, suborigin FROM pg_subscription;"
	docker compose -f deploy/dev/compose.yaml exec -T postgres psql -U invisiscan -d invisiscan_cloud -c "SELECT subname, subenabled, suborigin FROM pg_subscription;"
