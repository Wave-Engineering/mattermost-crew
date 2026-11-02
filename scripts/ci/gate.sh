#!/usr/bin/env bash
# The decision behind the `ci-gate` job (#213): green only if every job it was handed finished
# `success`. Usage: gate.sh <name>=<result> ...   e.g.  gate.sh unit=success install=success ci=failure
#
# `ci-gate` is the one check branch protection requires on `main`. The jobs it covers are not named
# there, because a required check that stops reporting blocks every PR, and a matrix leg is renamed
# whenever its image is bumped. A job that was skipped or cancelled did not pass, so only `success`
# does. No jobs at all fails too: a gate that checks nothing would pass everything.
set -euo pipefail

if (($# == 0)); then
  echo "gate: no job results given; refusing to pass" >&2
  exit 1
fi

failed=0
for arg in "$@"; do
  name="${arg%%=*}"
  result="${arg#*=}"
  if [[ "$arg" != *=* || -z "$name" ]]; then
    echo "gate: malformed argument '$arg' (want <name>=<result>)" >&2
    failed=1
  elif [[ "$result" == "success" ]]; then
    echo "gate: $name: success"
  else
    echo "gate: $name: ${result:-no result} (not success)" >&2
    failed=1
  fi
done

if ((failed)); then
  echo "gate: FAIL" >&2
  exit 1
fi
echo "gate: every job passed"
