.PHONY: format lint test integration_tests build help

test:
	uv run pytest

integration_tests:
	uv run pytest tests/integration_tests

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --select I --fix .

build:
	uv build

help:
	@echo "test              Run all tests"
	@echo "integration_tests Run graph integration tests"
	@echo "lint              Run Ruff"
	@echo "format            Format and sort imports"
	@echo "build             Build source and wheel distributions"
