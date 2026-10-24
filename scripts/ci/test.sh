#!/usr/bin/env bash
# The test suite. Integration tests skip unless MM_URL and MM_ADMIN_TOKEN are set
# (scripts/ci/mattermost-up.sh sets them).
set -euo pipefail

uv sync --frozen
make test
