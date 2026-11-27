#!/usr/bin/env bash
# Create one LiteLLM team with a ceiling on the Confluence tools its members can use.
#   Option A: run once per IdP group. The team_id must equal the group value in the JWT "groups"
#             claim. LiteLLM never creates teams from that claim, so a group without a team gets
#             no access. Use immutable group IDs where your IdP offers them, and one gateway group
#             per person: a user in several gateway groups is served by one team only.
#   Option B: run once per team, then add users with option-b-virtual-key/scripts/add_user.sh.
# A team can only use tools that are also in the server's allowed_tools in proxy/config.yaml.
#
# Usage: create_team.sh <team-id> [tool ...]
# Env:   LITELLM_PROXY_URL, LITELLM_ADMIN_KEY (an administrator key; use the master key only for
#        first-time setup), optional TEAM_RPM_LIMIT (default 120), TEAM_TPM_LIMIT (default 400000),
#        TEAM_MAX_BUDGET (USD per TEAM_BUDGET_DURATION, default unset) and TEAM_BUDGET_DURATION (30d).
set -euo pipefail
: "${LITELLM_PROXY_URL:?set LITELLM_PROXY_URL}"
: "${LITELLM_ADMIN_KEY:?set LITELLM_ADMIN_KEY}"
TEAM="${1:?usage: create_team.sh <team-id> [tool ...]}"
shift
SERVER_ID="${MCP_SERVER_ID:-atlassian-confluence}"
if [ "$#" -gt 0 ]; then
  TOOLS=("$@")
else
  TOOLS=(listConfluenceSpaces listConfluenceContent getConfluenceContent
         getAccessibleAtlassianResources atlassianUserInfo)
fi

# Pass the admin key to curl in a file, not on the command line (visible in the process list).
HEADERS=$(mktemp)
trap 'rm -f "$HEADERS"' EXIT
chmod 600 "$HEADERS"
printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$LITELLM_ADMIN_KEY" > "$HEADERS"

payload=$(python3 - "$TEAM" "$SERVER_ID" "${TOOLS[@]}" <<'PY'
import json, os, sys
team, server_id, tools = sys.argv[1], sys.argv[2], sys.argv[3:]
body = {
    "team_id": team,
    "team_alias": team,
    "models": ["claude-sonnet"],
    "rpm_limit": int(os.environ.get("TEAM_RPM_LIMIT", "120")),
    "tpm_limit": int(os.environ.get("TEAM_TPM_LIMIT", "400000")),
    "object_permission": {
        "mcp_servers": [server_id],
        "mcp_tool_permissions": {server_id: tools},
    },
}
if os.environ.get("TEAM_MAX_BUDGET"):
    body["max_budget"] = float(os.environ["TEAM_MAX_BUDGET"])
    body["budget_duration"] = os.environ.get("TEAM_BUDGET_DURATION", "30d")
print(json.dumps(body))
PY
)

curl -fsS -X POST "$LITELLM_PROXY_URL/team/new" -H @"$HEADERS" -d "$payload" \
  | python3 -c 'import json,sys; t=json.load(sys.stdin); print("created team", t.get("team_id"))'
