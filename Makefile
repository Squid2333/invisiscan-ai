.PHONY: contract-check migrate-check proto-lint proto-breaking proto-gen sqlc-gen dev-up dev-down dev-pipeline

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
