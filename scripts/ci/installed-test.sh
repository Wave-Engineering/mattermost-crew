#!/usr/bin/env bash
# The tests that need the installed daemon and broker (IT-01), run after scripts/ci/broker-up.sh
# and scripts/ci/daemon-up.sh. Here a missing install fails the run instead of skipping it.
set -euo pipefail

export CREWCHAT_INSTALLED_REQUIRED=1
uv run pytest tests/integration/test_daemon_installed.py -q -p no:cacheprovider \
  --junitxml=reports/junit-installed.xml
