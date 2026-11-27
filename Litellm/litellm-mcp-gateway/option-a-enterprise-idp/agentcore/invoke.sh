#!/usr/bin/env bash
# Invoke the agent as a signed-in user: HTTPS InvokeAgentRuntime with the user's IdP access token
# as a bearer token (a runtime with a JWT authorizer rejects SigV4). The token is read from a file
# and passed to curl through a temporary 0600 header file, never on the command line or stdout.
# Usage: RUNTIME_ARN=arn:aws:bedrock-agentcore:... ./invoke.sh <token-file> "<prompt>"
set -euo pipefail
TOKEN_FILE=$1
PROMPT=$2
REGION=${AWS_REGION:-eu-west-1}
: "${RUNTIME_ARN:?set RUNTIME_ARN to the agent runtime ARN}"

ESCAPED_ARN=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$RUNTIME_ARN")
URL="https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ESCAPED_ARN}/invocations?qualifier=DEFAULT"
# The runtime session ID must be at least 33 characters.
SESSION_ID="session-$(openssl rand -hex 16)"

HEADERS=$(umask 077; mktemp)
trap 'rm -f "$HEADERS"' EXIT
printf 'Authorization: Bearer %s\nContent-Type: application/json\nX-Amzn-Bedrock-AgentCore-Runtime-Session-Id: %s\n' \
  "$(tr -d '\r\n' < "$TOKEN_FILE")" "$SESSION_ID" > "$HEADERS"
BODY=$(python3 -c 'import json, sys; print(json.dumps({"prompt": sys.argv[1]}))' "$PROMPT")
curl -sS -X POST "$URL" -H @"$HEADERS" -d "$BODY" -w '\n[HTTP %{http_code}]\n'
