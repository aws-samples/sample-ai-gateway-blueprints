#!/usr/bin/env python3
"""Check what one user can do through the gateway: list MCP tools, then call one read tool.

Sends only the user's gateway credential (IdP access token for option A, virtual key for
option B) as "Authorization: Bearer". It never sends an Atlassian token.

Standard library only.
Usage (from the litellm-mcp-gateway folder):
  export GATEWAY_URL=https://litellm.internal.example.com      # or https://<host>:8443 through the bastion
  export GATEWAY_CREDENTIAL_FILE=~/.config/litellm/credential  # IdP access token or virtual key
  python3 scripts/check_access.py [--call atlassian_confluence-atlassianUserInfo] [--arguments '{}']
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"


def read_credential(path):
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            return handle.read().strip()
    value = os.environ.get("GATEWAY_CREDENTIAL", "").strip()
    if not value:
        sys.exit("Set GATEWAY_CREDENTIAL_FILE (or GATEWAY_CREDENTIAL)")
    return value


class McpSession:
    """Minimal MCP streamable HTTP client (JSON-RPC over POST, JSON or SSE responses)."""

    def __init__(self, url, credential):
        self.url = url
        self.headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id = None
        self.protocol_version = None
        self.next_id = 1

    def _post(self, payload):
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
                text = response.read().decode()
        except urllib.error.HTTPError as err:
            sys.exit(f"HTTP {err.code} from the gateway: {err.read().decode(errors='replace')[:300]}")
        wanted = payload.get("id")
        messages = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]
        if not messages and text.strip():
            messages = [json.loads(text)]
        # An SSE stream can carry notifications before the reply: return the reply to this request.
        for message in messages:
            if wanted is None or message.get("id") == wanted:
                return message
        return {}

    def request(self, method, params=None):
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.next_id += 1
        reply = self._post(message)
        if "error" in reply:
            sys.exit(f"{method} failed: {reply['error']}")
        result = reply.get("result", {})
        if method == "initialize":
            self.protocol_version = result.get("protocolVersion", PROTOCOL_VERSION)
        return result

    def open(self, client_name):
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1.0"},
        })
        self.notify("notifications/initialized")

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method})


def main():
    parser = argparse.ArgumentParser(description="List and call gateway MCP tools as one user")
    parser.add_argument("--gateway", default=os.environ.get("GATEWAY_URL"),
                        help="Gateway base URL (default: $GATEWAY_URL)")
    parser.add_argument("--credential-file", default=os.environ.get("GATEWAY_CREDENTIAL_FILE"),
                        help="File with the user's gateway credential (default: $GATEWAY_CREDENTIAL_FILE)")
    parser.add_argument("--call", default="atlassian_confluence-atlassianUserInfo",
                        help="Tool to call after listing (gateway name: <server>-<tool>)")
    parser.add_argument("--arguments", default="{}", help="JSON arguments for --call")
    args = parser.parse_args()
    if not args.gateway:
        sys.exit("Set GATEWAY_URL or pass --gateway")

    session = McpSession(args.gateway.rstrip("/") + "/mcp/", read_credential(args.credential_file))
    session.open("check-access")

    listing = session.request("tools/list")
    outcomes = listing.get("_meta", {}).get("litellm.ai/server_outcomes", {})
    for server, outcome in outcomes.items():
        print(f"server {server}: {outcome.get('status')} ({outcome.get('tool_count', 0)} tools)")
    names = sorted(tool["name"] for tool in listing.get("tools", []))
    if names:
        print("tools:", ", ".join(names))
    elif outcomes:
        print("tools: none (run connect_atlassian.py to connect your Atlassian account)")
    else:
        print("tools: none (your team is not granted any MCP server)")
    if not args.call or args.call not in names:
        return

    result = session.request("tools/call", {"name": args.call, "arguments": json.loads(args.arguments)})
    text = " ".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
    status = "error" if result.get("isError") else "ok"
    print(f"{args.call}: {status}: {text[:400]}")


if __name__ == "__main__":
    main()
