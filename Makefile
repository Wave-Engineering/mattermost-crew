.PHONY: lint test ci verify-install

lint:
	uv run ruff check . && uv run ruff format --check . && uv run mypy packages && ./scripts/ci/no-user-groups.sh

test:
	uv run pytest tests -v --junitxml=reports/junit.xml --cov=packages --cov-report=xml:reports/coverage.xml

ci: lint test

verify-install:
	./scripts/verify-install.sh
