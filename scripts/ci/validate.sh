#!/usr/bin/env bash
# Static checks: ruff and mypy.
set -euo pipefail

uv sync --frozen
make lint
