#!/usr/bin/env bash
# Fails if any code path calls the Mattermost user-groups API (R-17, CT-01).
#
# User groups are a paid feature, so crew and team tokens are matched by our own filter
# (crewchat/addressing.py). Every user-groups endpoint lives under `/groups` (`/api/v4/groups`,
# `/users/{id}/groups`, `/channels/{id}/groups`, `/teams/{id}/groups`, `/ldap/groups`), and the
# client libraries name it `get_groups`, `get_group`, `group_members` and the like.
#
# Usage: no-user-groups.sh [dir...]   (default: packages)
set -euo pipefail

cd "$(dirname "$0")/../.."
dirs=("$@")
if [ ${#dirs[@]} -eq 0 ]; then
    dirs=(packages)
fi

pattern='/groups\b|\b(get|search|create|delete|patch|update|link|unlink)_(user_)?groups?\b|\bgroup_members\b|\bget_group_members\b|\busergroups?\b'
if hits=$(grep -rnEi --include='*.py' "$pattern" "${dirs[@]}"); then
    echo "no-user-groups: the Mattermost user-groups API must not be called (R-17, CT-01):" >&2
    echo "$hits" >&2
    exit 1
fi
echo "no-user-groups: no user-groups API calls in ${dirs[*]}"
