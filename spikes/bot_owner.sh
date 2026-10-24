#!/usr/bin/env bash
# Spike 1.2 (#8): Dev Spec §5.N questions 6, 8 and 9, by experiment against a throwaway server.
#
#   source .mattermost.env        # from scripts/ci/mattermost-up.sh: MM_URL, MM_ADMIN_TOKEN
#   spikes/bot_owner.sh 2>&1 | tee out.txt
#
# It never runs against a real server: it creates users, patches the system_user_access_token
# role and mints tokens (CP-03, CP-06). Each step prints the request, the HTTP status and the
# relevant fields of the response; tokens are never printed. The last block is a summary of
# PASS/FAIL lines, one per check, that docs/spikes/2026-10-spike-1.2.md quotes.
set -uo pipefail  # no -e: a refused call is a result to record, not a reason to stop

: "${MM_URL:?source the env file from scripts/ci/mattermost-up.sh}"
: "${MM_ADMIN_TOKEN:?source the env file from scripts/ci/mattermost-up.sh}"
API="${MM_URL}/api/v4"
RUN="$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
PW="Spike-pw-${RUN}-Aa1!"
SUMMARY=()

say() { printf '\n## %s\n' "$*"; }
check() { # check NAME CONDITION-RESULT(0/1) DETAIL
  local verdict=FAIL
  [[ "$2" == 0 ]] && verdict=PASS
  SUMMARY+=("$verdict  $1  ($3)")
}

# call TOKEN METHOD PATH [JSON] -> sets STATUS and BODY; prints a one-line record.
call() {
  local token="$1" method="$2" path="$3" body="${4:-}" out
  local args=(-sS -o - -w '\n%{http_code}' -X "$method" -H "Authorization: Bearer ${token}"
    -H 'Content-Type: application/json')
  [[ -n "$body" ]] && args+=(--data "$body")
  out="$(curl "${args[@]}" "${API}${path}")"
  STATUS="${out##*$'\n'}"
  BODY="${out%$'\n'*}"
  printf '%s %s -> %s\n' "$method" "$path" "$STATUS" >&2
}
show() { jq -c "${1:-.}" <<<"$BODY" 2>/dev/null || printf '%s\n' "$BODY"; }

login() { # login USERNAME -> echoes a session token
  curl -sS -D - -o /dev/null -H 'Content-Type: application/json' \
    --data "$(jq -nc --arg u "$1" --arg p "$PW" '{login_id:$u,password:$p}')" \
    "${API}/users/login" | tr -d '\r' | awk -F': ' 'tolower($1)=="token"{print $2}'
}

ADMIN="$MM_ADMIN_TOKEN"
say "server"
call "$ADMIN" GET /system/ping
curl -sS -D - -o /dev/null "${API}/system/ping" | tr -d '\r' | grep -i '^x-version-id' || true

say "setup: users broker, observer, newowner and a team (as admin)"
mkuser() {
  call "$ADMIN" POST /users "$(jq -nc --arg u "$1-$RUN" --arg p "$PW" \
    '{username:$u,email:($u+"@example.com"),password:$p}')"
  jq -r .id <<<"$BODY"
}
BROKER_ID="$(mkuser broker)"
OBSERVER_ID="$(mkuser observer)"
NEWOWNER_ID="$(mkuser newowner)"
echo "broker=$BROKER_ID observer=$OBSERVER_ID newowner=$NEWOWNER_ID"
call "$ADMIN" POST /teams "$(jq -nc --arg n "spike-$RUN" '{name:$n,display_name:$n,type:"O"}')"
TEAM_ID="$(jq -r .id <<<"$BODY")"
for u in "$BROKER_ID" "$OBSERVER_ID" "$NEWOWNER_ID"; do
  call "$ADMIN" POST "/teams/${TEAM_ID}/members" "$(jq -nc --arg t "$TEAM_ID" --arg u "$u" '{team_id:$t,user_id:$u}')"
done

say "role patch: add create_bot, read_bots, manage_bots to system_user_access_token"
call "$ADMIN" GET /roles/name/system_user_access_token
ROLE_ID="$(jq -r .id <<<"$BODY")"
show '.permissions'
PERMS="$(jq -c '.permissions + ["create_bot","read_bots","manage_bots"] | unique' <<<"$BODY")"
call "$ADMIN" PUT "/roles/${ROLE_ID}/patch" "$(jq -nc --argjson p "$PERMS" '{permissions:$p}')"
show '.permissions'
[[ "$STATUS" == 200 ]] && jq -e '.permissions | index("create_bot") and index("manage_bots") and index("read_bots")' <<<"$BODY" >/dev/null
check "role patch applied" $? "PUT /roles/{id}/patch -> $STATUS"
call "$ADMIN" PUT "/users/${BROKER_ID}/roles" '{"roles":"system_user system_user_access_token"}'
check "role assigned to broker" "$([[ $STATUS == 200 ]]; echo $?)" "PUT /users/{id}/roles -> $STATUS"

BROKER="$(login "broker-$RUN")"
OBSERVER="$(login "observer-$RUN")"
call "$BROKER" GET /users/me
show '{id,roles}'

say "Q8: the broker creates a bot account"
call "$BROKER" POST /bots "$(jq -nc --arg u "agent-a-$RUN" '{username:$u,display_name:"agent a"}')"
show '{user_id,username,owner_id}'
BOT_A="$(jq -r .user_id <<<"$BODY")"
[[ "$STATUS" == 201 && "$(jq -r .owner_id <<<"$BODY")" == "$BROKER_ID" ]]
check "owner_id set to the creator" $? "POST /bots -> $STATUS, owner_id=$(jq -r .owner_id <<<"$BODY")"
call "$BROKER" POST /bots "$(jq -nc --arg u "agent-b-$RUN" '{username:$u,display_name:"agent b"}')"
BOT_B="$(jq -r .user_id <<<"$BODY")"
for b in "$BOT_A" "$BOT_B"; do
  call "$ADMIN" POST "/teams/${TEAM_ID}/members" "$(jq -nc --arg t "$TEAM_ID" --arg u "$b" '{team_id:$t,user_id:$u}')"
done

say "Q8: read the bot as a second user (observer, a plain system_user)"
call "$OBSERVER" GET "/bots/${BOT_A}"
show '{user_id,owner_id,status_code,id}'
OBS_BOTS_STATUS="$STATUS"
call "$OBSERVER" GET "/users/${BOT_A}"
show '{id,is_bot,bot_description,roles}'
echo "user object keys: $(jq -c 'keys' <<<"$BODY")"

say "Q8: mint the bot's own token (broker), then read as the bot itself"
call "$BROKER" POST "/users/${BOT_A}/tokens" '{"description":"spike bot a"}'
show 'del(.token)'
BOT_A_TOKEN="$(jq -r .token <<<"$BODY")"
BOT_A_TOKEN_ID="$(jq -r .id <<<"$BODY")"
call "$BROKER" POST "/users/${BOT_B}/tokens" '{"description":"spike bot b"}'
BOT_B_TOKEN="$(jq -r .token <<<"$BODY")"
call "$BOT_A_TOKEN" GET "/bots/${BOT_B}"
show '{user_id,owner_id,status_code,id}'
BOT_READS_STATUS="$STATUS"; BOT_READS_OWNER="$(jq -r '.owner_id // empty' <<<"$BODY")"
call "$BOT_A_TOKEN" GET "/bots/${BOT_A}"
show '{user_id,owner_id,status_code,id}'

say "Q8: who CAN read owner_id: the owner (broker, read_bots) and an admin (read_others_bots)"
call "$BROKER" GET "/bots/${BOT_A}"
show '{user_id,owner_id}'
BROKER_READS="$STATUS"
call "$BROKER" GET "/bots?per_page=200"
show '[.[] | {user_id,owner_id}]'
call "$ADMIN" GET "/bots/${BOT_A}"
show '{user_id,owner_id}'
check "owner_id readable by its owner (broker)" "$([[ $BROKER_READS == 200 ]]; echo $?)" "broker GET /bots/{id} -> $BROKER_READS"

if [[ "$OBS_BOTS_STATUS" == 200 ]]; then
  check "owner_id visible to another user" 0 "observer GET /bots/{id} -> 200"
elif [[ "$BOT_READS_STATUS" == 200 && "$BOT_READS_OWNER" == "$BROKER_ID" ]]; then
  check "owner_id visible to another user" 1 "observer GET /bots/{id} -> $OBS_BOTS_STATUS; only callers with read_bots see it (bot -> $BOT_READS_STATUS)"
else
  check "owner_id visible to another user" 1 "observer GET /bots/{id} -> $OBS_BOTS_STATUS, bot GET /bots/{id} -> $BOT_READS_STATUS"
fi

say "Q8: the bot tries to change its own owner"
call "$BOT_A_TOKEN" POST "/bots/${BOT_A}/assign/${OBSERVER_ID}"
show '{owner_id,status_code,id}'
A1="$STATUS"
call "$BOT_A_TOKEN" POST "/bots/${BOT_A}/assign/${BOT_A}"
show '{owner_id,status_code,id}'
A2="$STATUS"
call "$BOT_A_TOKEN" PUT "/bots/${BOT_A}" "$(jq -nc --arg u "agent-a-$RUN" --arg o "$OBSERVER_ID" '{username:$u,owner_id:$o}')"
show '{owner_id,status_code,id}'
A3="$STATUS"; A3_OWNER="$(jq -r '.owner_id // empty' <<<"$BODY")"
call "$ADMIN" GET "/bots/${BOT_A}"
show '{owner_id}'
AFTER="$(jq -r .owner_id <<<"$BODY")"
[[ "$AFTER" == "$BROKER_ID" && "$A1" =~ ^40[134]$ && "$A2" =~ ^40[0-9]$ ]]
check "bot cannot change its owner" $? "assign->observer $A1, assign->self $A2, PUT /bots owner_id $A3 (owner_id in reply: ${A3_OWNER:-none}); owner after: $AFTER"

say "Q8: the owner (broker) reassigns the bot"
call "$BROKER" POST "/bots/${BOT_A}/assign/${NEWOWNER_ID}"
show '{owner_id,status_code,id}'
R1="$STATUS"
call "$ADMIN" GET "/bots/${BOT_A}"
REASSIGNED="$(jq -r .owner_id <<<"$BODY")"
[[ "$R1" == 200 && "$REASSIGNED" == "$NEWOWNER_ID" ]]
check "owner can reassign the bot" $? "POST /bots/{id}/assign/{user} as broker -> $R1; owner now $REASSIGNED"
call "$BROKER" GET "/bots/${BOT_A}"
show '{owner_id,status_code,id}'
check "former owner no longer sees the bot" "$([[ $STATUS == 404 || $STATUS == 403 ]]; echo $?)" "broker GET /bots/{id} after reassignment -> $STATUS"
call "$BROKER" POST "/bots/${BOT_A}/assign/${BROKER_ID}"
show '{owner_id,status_code,id}'
echo "(broker re-assigning back after losing ownership -> $STATUS)"
call "$ADMIN" POST "/bots/${BOT_A}/assign/${BROKER_ID}"
show '{owner_id}'

say "Q6: a bot-to-bot DM"
call "$BOT_A_TOKEN" POST /channels/direct "$(jq -nc --arg a "$BOT_A" --arg b "$BOT_B" '[$a,$b]')"
show '{id,type,status_code,id}'
DM_STATUS="$STATUS"; DM_ID="$(jq -r '.id // empty' <<<"$BODY")"
POST_STATUS=none; READ_STATUS=none
if [[ "$DM_STATUS" =~ ^20[01]$ ]]; then
  call "$BOT_A_TOKEN" POST /posts "$(jq -nc --arg c "$DM_ID" '{channel_id:$c,message:"bot a to bot b",props:{crew_proto:1}}')"
  show '{id,user_id,props,status_code}'
  POST_STATUS="$STATUS"
  call "$BOT_B_TOKEN" GET "/channels/${DM_ID}/posts?per_page=5"
  show '[.posts[] | {user_id,message,props}]'
  READ_STATUS="$STATUS"
fi
[[ "$DM_STATUS" =~ ^20[01]$ && "$POST_STATUS" == 201 && "$READ_STATUS" == 200 ]]
check "bot-to-bot DM supported" $? "create DM $DM_STATUS, post $POST_STATUS, other bot reads $READ_STATUS"

say "Q6: bots in @ autocomplete, and their avatars"
call "$OBSERVER" GET "/users/autocomplete?in_team=${TEAM_ID}&name=agent-a"
show '[.users[] | {id,username,is_bot,last_picture_update}]'
jq -e --arg b "$BOT_A" '[.users[].id] | index($b)' <<<"$BODY" >/dev/null
check "bots appear in @ autocomplete" $? "GET /users/autocomplete?in_team&name -> $STATUS"
# Set a distinct avatar on bot a: first as the bot itself, then as its owner (the broker),
# through both the user-image and the bot-icon endpoints.
PNG="$(mktemp --suffix=.png)"
SVG="$(mktemp --suffix=.svg)"
python3 - "$PNG" <<'PY'
import struct, sys, zlib
w = h = 16
raw = b"".join(b"\x00" + b"\xd0\x40\x20" * w for _ in range(h))
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
PY
printf '%s' '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="#d04020"/></svg>' >"$SVG"
upload() { # upload TOKEN PATH FILE -> http status
  curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $1" -F "image=@$3" "${API}$2"
}
AV_SELF="$(upload "$BOT_A_TOKEN" "/users/${BOT_A}/image" "$PNG")"
echo "POST /users/{bot}/image as the bot -> $AV_SELF"
AV_OWNER="$(upload "$BROKER" "/users/${BOT_A}/image" "$PNG")"
echo "POST /users/{bot}/image as the owner (broker) -> $AV_OWNER"
AV_ICON="$(upload "$BROKER" "/bots/${BOT_A}/icon" "$SVG")"
echo "POST /bots/{bot}/icon (SVG) as the owner (broker) -> $AV_ICON"
rm -f "$PNG" "$SVG"
call "$OBSERVER" GET "/users/autocomplete?in_team=${TEAM_ID}&name=agent-"
show '[.users[] | {username,is_bot,last_picture_update}]'
IMG_A="$(curl -sS -H "Authorization: Bearer ${OBSERVER}" "${API}/users/${BOT_A}/image" | sha256sum | cut -c1-16)"
IMG_B="$(curl -sS -H "Authorization: Bearer ${OBSERVER}" "${API}/users/${BOT_B}/image" | sha256sum | cut -c1-16)"
echo "avatar sha256 as seen by observer: bot a $IMG_A, bot b (default) $IMG_B"
[[ ( "$AV_OWNER" == 200 || "$AV_ICON" == 200 ) && "$IMG_A" != "$IMG_B" ]]
check "bots carry their own avatars (set by the owner)" $? "as bot $AV_SELF, as owner $AV_OWNER, icon $AV_ICON; hashes a=$IMG_A b=$IMG_B"

say "Q9/R-07: token create, native expiry detection, revoke"
call "$BROKER" GET "/users/tokens/${BOT_A_TOKEN_ID}"
show '.'
if jq -e 'has("expires_at")' <<<"$BODY" >/dev/null; then EXPIRY=present; else EXPIRY=absent; fi
echo "native token expiry: $EXPIRY (detected by the expires_at key on the token object)"
check "native expiry detected" 0 "expires_at key $EXPIRY"
if [[ "$EXPIRY" == present ]]; then
  EXP="$(( ($(date +%s) + 86400) * 1000 ))"
  call "$BROKER" POST "/users/${BOT_B}/tokens" "$(jq -nc --argjson e "$EXP" '{description:"spike expiring",expires_at:$e}')"
  show 'del(.token)'
  check "token created with native expiry" "$([[ $STATUS == 200 && $(jq -r .expires_at <<<"$BODY") == "$EXP" ]]; echo $?)" "POST /users/{id}/tokens expires_at -> $STATUS"
fi
call "$BOT_A_TOKEN" GET /users/me
BEFORE="$STATUS"
call "$BROKER" POST /users/tokens/revoke "$(jq -nc --arg t "$BOT_A_TOKEN_ID" '{token_id:$t}')"
REV="$STATUS"
call "$BOT_A_TOKEN" GET /users/me
AFTER_REV="$STATUS"
[[ "$BEFORE" == 200 && "$REV" == 200 && "$AFTER_REV" == 401 ]]
check "token create/revoke" $? "bot token works $BEFORE, revoke $REV, after revoke $AFTER_REV"

say "summary"
printf '%s\n' "${SUMMARY[@]}"
