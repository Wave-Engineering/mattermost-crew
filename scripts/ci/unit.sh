#!/usr/bin/env bash
# The unprivileged half of `make ci` (#79): lint and the unit tests, with no root, no OS users,
# no systemd and no Mattermost. It runs on the org's self-hosted runner (vars.OAW_RUNNER), which
# is persistent, so it touches nothing outside the workspace and the uv cache. The integration
# and installed tests stay in the `ci` matrix on the ephemeral self-hosted pool, a fresh VM per
# job (#83).
set -euo pipefail

# Nothing here may reach for a host install, even if the runner's environment names one.
unset MM_URL MM_ADMIN_TOKEN CREWCHAT_SOCKET CREWCHAT_BIN CREWKIOSK_SOCKET CREWBROKER_INSTANCES

uv sync --frozen
make lint
mkdir -p reports
uv run pytest tests/unit -q --junitxml=reports/junit-unit.xml \
  --cov=packages --cov-report=xml:reports/coverage-unit.xml
