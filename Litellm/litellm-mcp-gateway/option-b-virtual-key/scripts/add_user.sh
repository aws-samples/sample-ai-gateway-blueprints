#!/usr/bin/env bash
# Create a LiteLLM user in a team and issue that user's own virtual key.
# The key identifies the person: the gateway stores their Atlassian token under this user, so
# never share a key between people. Use an immutable user ID (employee ID or IdP subject), not a
# name that could be reused by someone else later.
# The key is written to <user-id>.key with mode 0600 and is not printed. Hand it over through your
# normal secret distribution channel, then delete the file. Keys expire (default 30d, and the
# config caps every key at 30 days). To issue a new key for an existing user, see the README.
#
# Usage: add_user.sh <user-id> <email> <team-id> [key-ttl, default 30d]
# Env:   LITELLM_PROXY_URL, LITELLM_ADMIN_KEY
set -euo pipefail
USAGE="usage: add_user.sh <user-id> <email> <team-id> [key-ttl, default 30d]"
: "${LITELLM_PROXY_URL:?set LITELLM_PROXY_URL}"
: "${LITELLM_ADMIN_KEY:?set LITELLM_ADMIN_KEY}"
USER_ID="${1:?$USAGE}"
EMAIL="${2:?$USAGE}"
TEAM="${3:?$USAGE}"
TTL="${4:-30d}"
OUT="${USER_ID}.key"
[ -e "$OUT" ] && { echo "$OUT exists; move it away first" >&2; exit 1; }

HEADERS=$(mktemp)
trap 'rm -f "$HEADERS"' EXIT
chmod 600 "$HEADERS"
printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$LITELLM_ADMIN_KEY" > "$HEADERS"

body() { python3 - "$@" <<'PY'
import json, sys
kind, user_id, email, team, ttl = sys.argv[1:6]
if kind == "user":
    print(json.dumps({"user_id": user_id, "user_email": email, "user_role": "internal_user", "auto_create_key": False}))
elif kind == "member":
    print(json.dumps({"team_id": team, "member": {"role": "user", "user_id": user_id}}))
else:
    print(json.dumps({"user_id": user_id, "team_id": team, "key_alias": f"{user_id}-{ttl}-1", "duration": ttl}))
PY
}

curl -fsS -X POST "$LITELLM_PROXY_URL/user/new" -H @"$HEADERS" -d "$(body user "$USER_ID" "$EMAIL" "$TEAM" "$TTL")" >/dev/null
curl -fsS -X POST "$LITELLM_PROXY_URL/team/member_add" -H @"$HEADERS" -d "$(body member "$USER_ID" "$EMAIL" "$TEAM" "$TTL")" >/dev/null

umask 077
curl -fsS -X POST "$LITELLM_PROXY_URL/key/generate" -H @"$HEADERS" -d "$(body key "$USER_ID" "$EMAIL" "$TEAM" "$TTL")" \
  | python3 -c 'import json,sys; open(sys.argv[1],"w").write(json.load(sys.stdin)["key"]+"\n")' "$OUT"
echo "created user $USER_ID in team $TEAM; key written to $OUT (expires in $TTL)"
