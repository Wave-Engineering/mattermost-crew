#!/usr/bin/env bash
# The dev-space smoke (Story 3.9; Dev Spec §6.1 and §7): the end-to-end flows, run by an agent
# against the OaW dev space instead of a throwaway container, with two crews:
#
#   E2E-01  a cross-crew exchange under trusted crews       (R-01, R-09, R-12, R-16, R-20, R-26)
#   E2E-02  kiosk Q&A, a social flip with its kiosk review,
#           and social lapsing back to crew only            (R-21, R-23, R-31, R-33)
#   E2E-03  an `@agents` broadcast to about 30 agents,
#           within the server's rate limit                  (R-16, R-25)
#
# Usage:
#
#   MM_URL=https://<the OaW dev space> MM_ADMIN_TOKEN=<a dev-space admin access token> \
#   SMOKE_TARGET=oaw-devspace scripts/smoke-devspace.sh
#
# It prints a markdown report on stdout, ready to post to Plan #3 as a comment, and writes the
# same report to reports/smoke-devspace.md (with the JUnit XML beside it). Progress and pytest's
# own output go to stderr. It exits 0 only when every flow ran and passed: a skipped flow is a
# failed smoke, because the smoke then proved nothing.
#
# What it does to the server: the tests/e2e world creates two crews' partners, an outsider, two
# broker accounts and the bot accounts, each with a per-run suffix, and gives the broker
# accounts the patched system_user_access_token role (scripts/ci/broker-role-patch.sh). The
# OaW dev space needs no yes-prod for that (CP-06). It never cleans up after itself: wiping the
# dev space needs BJ. On any partner organization's production server the role patch is the
# human partner's yes-prod (CP-03, S-25), so this script runs only with
# SMOKE_TARGET=oaw-devspace, a statement that MM_URL is the OaW dev space, or with
# SMOKE_TARGET=local against a localhost throwaway from scripts/ci/mattermost-up.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORTS="${SMOKE_REPORTS:-${ROOT}/reports}"
FLOWS=(tests/e2e/test_cross_crew.py tests/e2e/test_outsider_switch.py tests/e2e/test_broadcast.py)

log() { echo "smoke-devspace: $*" >&2; }
die() { log "ERROR: $*"; exit 2; }

: "${MM_URL:?set MM_URL to the OaW dev space URL}"
: "${MM_ADMIN_TOKEN:?set MM_ADMIN_TOKEN to a dev-space admin access token}"
MM_URL="${MM_URL%/}"

case "${SMOKE_TARGET:-}" in
  oaw-devspace) ;;
  local)
    case "$MM_URL" in
      http://localhost:* | http://127.0.0.1:*) ;;
      *) die "SMOKE_TARGET=local needs a localhost MM_URL, not $MM_URL" ;;
    esac
    ;;
  *) die "set SMOKE_TARGET=oaw-devspace to confirm MM_URL is the OaW dev space (CP-06); never a partner's production server (CP-03)" ;;
esac

for tool in curl jq uv; do
  command -v "$tool" >/dev/null || die "$tool is not installed"
done

version="$(curl -fsS "${MM_URL}/api/v4/config/client?format=old" | jq -r '.Version // "unknown"')" \
  || die "no answer from ${MM_URL}/api/v4/config/client"
curl -fsS -o /dev/null -H "Authorization: Bearer ${MM_ADMIN_TOKEN}" "${MM_URL}/api/v4/users/me" \
  || die "MM_ADMIN_TOKEN is not accepted by ${MM_URL}"

cd "$ROOT"
mkdir -p "$REPORTS"
junit="${REPORTS}/junit-smoke-devspace.xml"
report="${REPORTS}/smoke-devspace.md"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
head_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

log "running ${#FLOWS[@]} flows against ${MM_URL} (Mattermost ${version})"
uv sync --frozen >&2
export MM_URL MM_ADMIN_TOKEN
# The e2e world's broker accounts get the patched role through broker-role-patch.sh, which
# refuses a non-local server unless told; the SMOKE_TARGET check above is that decision.
export BROKER_ROLE_PATCH_ALLOW_REMOTE=1
set +e
uv run pytest "${FLOWS[@]}" -v -rA -p no:cacheprovider --junitxml="$junit" >&2
pytest_rc=$?
set -e
finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

uv run python - "$junit" "$report" "$MM_URL" "$version" "$head_sha" "$started" "$finished" \
  "$pytest_rc" <<'PY'
import sys
import xml.etree.ElementTree as ET

junit, report, url, version, sha, started, finished, rc = sys.argv[1:]
flows = {
    "test_e2e01_cross_crew_under_trusted": "E2E-01 cross-crew exchange under trusted crews",
    "test_e2e02_outsider_and_switch": "E2E-02 kiosk Q&A, social flip and its review, lapse",
    "test_e2e03_fleet_broadcast_within_rate_limit": "E2E-03 `@agents` broadcast within the rate limit",
}
outcomes = {}
for case in ET.parse(junit).getroot().iter("testcase"):
    result = "pass"
    for child in case:
        if child.tag in ("failure", "error", "skipped"):
            result = {"failure": "FAIL", "error": "ERROR", "skipped": "SKIPPED"}[child.tag]
    outcomes[case.get("name")] = (result, float(case.get("time") or 0))

lines = [
    "## Dev-space smoke (Story 3.9)",
    "",
    f"- **Server:** {url} (Mattermost {version})",
    f"- **Commit:** `{sha}`",
    f"- **Run:** {started} to {finished}, by `scripts/smoke-devspace.sh`",
    "",
    "| Flow | Result | Time |",
    "|------|--------|------|",
]
ok = rc == "0"
for name, label in flows.items():
    result, secs = outcomes.get(name, ("NOT RUN", 0.0))
    ok = ok and result == "pass"
    lines.append(f"| {label} | {result} | {secs:.1f} s |")
lines += ["", f"**Smoke: {'PASS' if ok else 'FAIL'}** (pytest exit {rc})", ""]
text = "\n".join(lines)
with open(report, "w", encoding="utf-8") as fh:
    fh.write(text)
print(text)
sys.exit(0 if ok else 1)
PY
