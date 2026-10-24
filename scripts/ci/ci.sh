#!/usr/bin/env bash
# The CI job's build: install exactly what uv.lock pins (hash-checked), then `make ci`.
set -euo pipefail

uv sync --frozen
make ci
