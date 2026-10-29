#!/usr/bin/env bash
# CI ONLY: write stdin to DEST with MODE and OWNER:GROUP, over an existing file or a new one.
#
#   printf '%s\n' "$content" | scripts/ci/put-file.sh 0644 root:root /etc/crewchat/servers.toml
#
# Two steps, as the runbook writes files (#123): `install` from /dev/null sets the mode and owner
# (and empties the file), then `tee` writes the content. Never `install … /dev/stdin`: Ubuntu's
# uutils `install` fails on a pipe whenever DEST already exists. It needs root or sudo.
set -euo pipefail

[[ $# -eq 3 && "$2" == *:* ]] || { echo "usage: put-file.sh MODE OWNER:GROUP DEST <content" >&2; exit 2; }
mode="$1" owner="${2%%:*}" group="${2#*:}" dest="$3"

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"

$SUDO install -m "$mode" -o "$owner" -g "$group" /dev/null "$dest"
$SUDO tee "$dest" >/dev/null
