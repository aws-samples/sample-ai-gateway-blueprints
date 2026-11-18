#!/usr/bin/env bash
# Registers the read-only Confluence MCP server in the gateway database
# (no restart needed), then creates a team key with the confluence_read group.
# Check the endpoint paths against your LiteLLM version's API docs.
set -euo pipefail
: "${LITELLM_PROXY_URL:?set LITELLM_PROXY_URL}"
: "${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY}"

curl -sS -X POST "$LITELLM_PROXY_URL/v1/mcp/server" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "server_name": "atlassian_confluence_read",
    "url": "https://mcp.atlassian.com/v1/mcp/authv2",
    "transport": "http",
    "auth_type": "none",
    "extra_headers": ["Authorization"],
    "allowed_tools": [
      "getConfluenceSpaces", "getConfluencePage", "getPagesInConfluenceSpace",
      "getAccessibleAtlassianResources", "atlassianUserInfo"
    ],
    "mcp_access_groups": ["confluence_read"]
  }'

curl -sS -X POST "$LITELLM_PROXY_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "team-docs-agent",
    "models": ["claude-sonnet"],
    "object_permission": {"mcp_access_groups": ["confluence_read"]}
  }'
