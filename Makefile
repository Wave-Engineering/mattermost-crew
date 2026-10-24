.PHONY: lint test ci verify-install

lint:
	uv run ruff check . && uv run mypy packages

test:
	uv run pytest tests -q --junitxml=reports/junit.xml --cov=packages --cov-report=xml:reports/coverage.xml

ci: lint test

verify-install:
	./scripts/verify-install.sh
