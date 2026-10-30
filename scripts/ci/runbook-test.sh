#!/usr/bin/env bash
# IT-20: run docs/runbook.md's install and verify blocks on this fresh host (Story 3.7).
#
#   scripts/ci/mattermost-up.sh       # first: MM_URL and MM_ADMIN_TOKEN
#   scripts/ci/runbook-test.sh
#
# tests/integration/test_runbook.py runs scripts/ci/runbook-configure.sh (the runbook's Configure
# step, with CI's values), then every fenced bash block under "Install …" and "Verify the
# install", in document order. The blocks create the service users, install into
# /opt/mattermost-crew and start the systemd units, so this runs only on the ephemeral pool, a
# fresh VM per job (#83), never after two-user-setup.sh or on a workstation.
set -euo pipefail

"$(dirname "$0")/host-prereqs.sh"  # the runbook's Prerequisites, as an operator provides them

export CREWCHAT_RUNBOOK_IT=1
export CREWBROKER_INSTANCES="${CREWBROKER_INSTANCES:-ci:local}"
# "Install kiosk research" for one server (#142): the broker, the builder and its units.
export CREWKIOSK_RESEARCH_SERVERS="${CREWKIOSK_RESEARCH_SERVERS:-local}"

uv sync --frozen
mkdir -p reports
uv run pytest tests/integration/test_runbook.py -q -p no:cacheprovider \
  --junitxml=reports/junit-runbook.xml
