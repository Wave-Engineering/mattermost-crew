#!/usr/bin/env bash
# Fetch the kit's `sqtk-doorbell.sh`, and the `sqtk-answer.jq` it installs beside it, at the
# commit pinned in `scripts/ci/kit-ref` (#156), so `test_squawkie`'s fixture checks run on CI
# instead of skipping. Pinned, so an upstream edit can't turn unrelated PRs red: bumping
# `kit-ref` is how a kit change gets checked.
#
# Prints `export SQTK_DOORBELL=<path>` on stdout, for `eval`, and appends it to `$GITHUB_ENV`
# when that is set.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ref="$(tr -d '[:space:]' <"${ROOT}/scripts/ci/kit-ref")"
if [[ ! "$ref" =~ ^[0-9a-f]{40}$ ]]; then
  echo "fetch-kit: scripts/ci/kit-ref is not a full commit SHA: '${ref}'" >&2
  exit 1
fi

base="https://raw.githubusercontent.com/Wave-Engineering/claudecode-workflow/${ref}/scripts"
dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/crewchat-kit"
mkdir -p "${dir}/hooks/workflow"
curl -fsS --retry 3 -o "${dir}/sqtk-doorbell.sh" "${base}/sqtk-doorbell.sh"
curl -fsS --retry 3 -o "${dir}/hooks/workflow/sqtk-answer.jq" \
  "${base}/hooks/workflow/sqtk-answer.jq"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "SQTK_DOORBELL=${dir}/sqtk-doorbell.sh" >>"$GITHUB_ENV"
fi
echo "export SQTK_DOORBELL=${dir}/sqtk-doorbell.sh"
