.PHONY: contract-check migrate-check proto-lint proto-breaking

BUF_BREAKING_AGAINST ?= .git\#branch=main,subdir=proto
BUF ?= buf

contract-check: migrate-check

migrate-check:
	python3 tools/check_migrations.py

proto-lint:
	@$(BUF) lint

proto-breaking:
	@$(BUF) breaking --against '$(BUF_BREAKING_AGAINST)'
