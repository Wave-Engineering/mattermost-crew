#!/usr/bin/env bash
# Verify an installed crewchat host (Dev Spec §5.B, "Verify the install").
#
# It starts empty. Story 1.3 adds the broker's checks and Story 1.4 the daemon's: the socket
# permissions, `health`, and that the agent's user can read no credential, checked per known
# file name with `test -e <file> && ! sudo -u agent test -r <file>`, never a glob (R-08).
set -euo pipefail

echo "verify-install: no checks yet"
