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
#   MM_URL=https://chat.apps.oakai.waveeng.com MM_ADMIN_TOKEN=<a dev-space admin access token> \
#   SMOKE_TARGET=oaw-devspace scripts/smoke-devspace.sh [--cleanup <run> [--yes]]
#
# It prints a markdown report on stdout, ready to post to Plan #3 as a comment, and writes the
# same report to reports/smoke-devspace.md (with the JUnit XML beside it). Progress and pytest's
# own output go to stderr. It exits 0 only when every flow ran and passed: a skipped flow is a
# failed smoke, because the smoke then proved nothing.
#
# What it does to the server: the tests/e2e world creates two crews' partners, an outsider, two
# broker accounts and the bot accounts, each with a per-run suffix, and gives the broker
# accounts the patched system_user_access_token role (scripts/ci/broker-role-patch.sh). The
# OaW dev space needs no yes-prod for that (CP-06). The report names the run's 4-hex suffix and
# its creation record, reports/smoke-<run>-created.json: every account and team the run made.
#
# `--cleanup <run>` deactivates that run's own accounts and archives its team, from its creation
# record: it disables the bots, deactivates the users and archives the team, with Mattermost's
# soft deletes, so each is reversible. It acts on an entry only when its id is in the record,
# its name carries the run's suffix, and the server still has that id under that name, active.
# Nothing outside the record is listed or touched. Without `--yes` it is a dry run that writes
# nothing. That is not a dev-space wipe, which still needs BJ; the same target guard below keeps
# it off any partner's server. On any partner organization's production server the role patch is the
# human partner's yes-prod (CP-03, S-25), so this script runs only with
# SMOKE_TARGET=oaw-devspace and MM_URL exactly the OaW dev space ($DEVSPACE_URL below), or
# with SMOKE_TARGET=local against a localhost throwaway from scripts/ci/mattermost-up.sh.
# Only the dev space lets the role patch reach a non-local server (#99).
set -euo pipefail

DEVSPACE_URL="https://chat.apps.oakai.waveeng.com"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORTS="${SMOKE_REPORTS:-${ROOT}/reports}"
FLOWS=(tests/e2e/test_cross_crew.py tests/e2e/test_outsider_switch.py tests/e2e/test_broadcast.py)

log() { echo "smoke-devspace: $*" >&2; }
die() { log "ERROR: $*"; exit 2; }

cleanup_run=""
yes=0
while (($#)); do
  case "$1" in
    --cleanup)
      (($# >= 2)) || die "--cleanup needs the run suffix the smoke report names"
      cleanup_run="$2"
      shift 2
      ;;
    --yes) yes=1; shift ;;
    *) die "unknown argument: $1 (usage: $0 [--cleanup <run> [--yes]])" ;;
  esac
done
[[ "$yes" == 0 || -n "$cleanup_run" ]] || die "--yes goes only with --cleanup <run>"

: "${MM_URL:?set MM_URL to the OaW dev space URL}"
: "${MM_ADMIN_TOKEN:?set MM_ADMIN_TOKEN to a dev-space admin access token}"
MM_URL="${MM_URL%/}"

allow_remote=0
case "${SMOKE_TARGET:-}" in
  oaw-devspace)
    # The statement alone is not enough: the broker role patch below reaches the server, and
    # on a partner's production server that needs its human partner's yes-prod (CP-03).
    [[ "$MM_URL" == "$DEVSPACE_URL" ]] \
      || die "SMOKE_TARGET=oaw-devspace needs MM_URL=$DEVSPACE_URL, not $MM_URL"
    allow_remote=1
    ;;
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

# One request as the admin: the body goes to $api_body, the HTTP status to stdout ("000" when
# the server could not be reached). Not -f: a 404 is an answer here ("gone"), not a failure.
api_body=""
api() {
  curl -sS -o "$api_body" -w '%{http_code}' -X "$1" \
    -H "Authorization: Bearer ${MM_ADMIN_TOKEN}" "${MM_URL}/api/v4$2" || true
}

# `--cleanup <run> [--yes]`: deactivate what run <run> recorded creating, and nothing else.
cleanup() {
  local run="$1" yes="$2"
  [[ "$run" =~ ^[0-9a-f]{4}$ ]] || die "--cleanup needs a run suffix of 4 lowercase hex digits, not '$run'"
  local record="${REPORTS}/smoke-${run}-created.json"
  [[ -f "$record" ]] || die "no creation record ${record}: with no record, there is nothing --cleanup may touch"
  jq -e --arg run "$run" '.run == $run and (.created | type == "array")' "$record" >/dev/null 2>&1 \
    || die "${record} is not run ${run}'s creation record"
  local user_re="^(partner-(oaw|synth)${run}|outsider-e2e${run}|x\\.oaw${run}|crewbroker-(oaw|synth)${run}-e2e|[a-z0-9_-]+\\.(oaw|synth)${run})$"
  local team_re="^crew-e2e${run}$"
  api_body="$(mktemp)"
  trap 'rm -f "$api_body"' RETURN
  local acted=0 skipped=0 failed=0 kind id name status path reason verb method
  while IFS=$'\t' read -r kind id name; do
    reason=""
    case "$kind" in
      user | bot) [[ "$name" =~ $user_re ]] || reason="not this run's name"; path="/users/${id}" ;;
      team) [[ "$name" =~ $team_re ]] || reason="not this run's name"; path="/teams/${id}" ;;
      *) reason="unknown kind '$kind'" ;;
    esac
    [[ -n "$reason" || "$id" =~ ^[a-z0-9]{26}$ ]] || reason="not a Mattermost id"
    if [[ -z "$reason" ]]; then
      status="$(api GET "$path")"
      if [[ "$status" == 404 ]]; then
        reason="gone"
      elif [[ "$status" != 200 ]]; then
        echo "FAILED  ${kind} ${name} (${id}): lookup answered HTTP ${status}"
        failed=$((failed + 1))
        continue
      elif [[ "$(jq -r 'if has("name") and (has("username") | not) then .name else .username end' "$api_body")" != "$name" ]]; then
        reason="renamed"
      elif [[ "$(jq -r '.delete_at // 0' "$api_body")" != 0 ]]; then
        reason="already deactivated"
      elif [[ "$kind" != team && "$(jq -r '.is_bot // false' "$api_body")" != "$([[ "$kind" == bot ]] && echo true || echo false)" ]]; then
        reason="kind mismatch"
      fi
    fi
    if [[ -n "$reason" ]]; then
      echo "skip    ${kind} ${name} (${id}): ${reason}"
      skipped=$((skipped + 1))
      continue
    fi
    case "$kind" in
      bot) verb="disable"; method=POST; path="/bots/${id}/disable" ;;
      user) verb="deactivate"; method=DELETE; path="/users/${id}" ;;
      team) verb="archive"; method=DELETE; path="/teams/${id}" ;;
    esac
    if [[ "$yes" != 1 ]]; then
      echo "would   ${verb} ${kind} ${name} (${id})"
      acted=$((acted + 1))
      continue
    fi
    status="$(api "$method" "$path")"
    if [[ "$status" == 200 ]]; then
      echo "done    ${verb} ${kind} ${name} (${id})"
      acted=$((acted + 1))
    else
      echo "FAILED  ${verb} ${kind} ${name} (${id}): HTTP ${status}"
      failed=$((failed + 1))
    fi
  done < <(jq -r '.created[] | [.kind, .id, .username] | @tsv' "$record")
  if [[ "$yes" == 1 ]]; then
    echo "cleanup ${run}: ${acted} acted on, ${skipped} skipped, ${failed} failed"
  else
    echo "cleanup ${run} (dry run, nothing written; add --yes): ${acted} to act on, ${skipped} skipped, ${failed} failed"
  fi
  ((failed == 0))
}

if [[ -n "$cleanup_run" ]]; then
  rc=0
  cleanup "$cleanup_run" "$yes" || rc=$?
  exit "$rc"
fi

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

# The run suffix is chosen here and printed, so `--cleanup <run>` can find what the run made.
run="$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
created="${REPORTS}/smoke-${run}-created.json"
[[ ! -e "$created" ]] || die "${created} already exists: a creation record is never overwritten"
export E2E_RUN="$run" E2E_CREATED="$created"

log "running ${#FLOWS[@]} flows against ${MM_URL} (Mattermost ${version}), run suffix ${run}"
log "creation record: ${created} (scripts/smoke-devspace.sh --cleanup ${run} deactivates its accounts)"
uv sync --frozen >&2
export MM_URL MM_ADMIN_TOKEN
# The e2e world's broker accounts get the patched role through broker-role-patch.sh, which
# refuses a non-local server unless told. Only the pinned dev space is told (#99).
if [[ "$allow_remote" == 1 ]]; then
  export BROKER_ROLE_PATCH_ALLOW_REMOTE=1
else
  unset BROKER_ROLE_PATCH_ALLOW_REMOTE
fi
set +e
uv run pytest "${FLOWS[@]}" -v -rA -p no:cacheprovider --junitxml="$junit" >&2
pytest_rc=$?
set -e
finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

uv run python - "$junit" "$report" "$MM_URL" "$version" "$head_sha" "$started" "$finished" \
  "$pytest_rc" "$run" "$created" <<'PY'
import os
import sys
import xml.etree.ElementTree as ET

junit, report, url, version, sha, started, finished, rc, run, created = sys.argv[1:]
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
    f"- **Run suffix:** `{run}` (`scripts/smoke-devspace.sh --cleanup {run}` deactivates its accounts)",
    f"- **Creation record:** `reports/{os.path.basename(created)}` (what `--cleanup {run}` acts on)",
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
