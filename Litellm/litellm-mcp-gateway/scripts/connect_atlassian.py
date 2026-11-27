#!/usr/bin/env python3
"""Connect one user's Atlassian account to the gateway (one-time consent, OAuth 2.1 with PKCE).

The user signs in to the gateway with their own credential:
  Option A (Enterprise): an access token (JWT) from the company identity provider
  Option B (community):  the user's own LiteLLM virtual key
The gateway becomes the holder of that user's Atlassian token. Agents never see it.

How the flow runs:
  1. This helper calls the gateway's /authorize for the MCP server with the user's credential
     and a PKCE challenge. The gateway answers with the Atlassian consent URL and a one-time
     state cookie (anti-CSRF), which this helper keeps.
  2. The user approves in a browser. Atlassian sends the browser to <gateway>/callback. That
     browser does not hold the state cookie, so the page says the sign-in is incomplete.
     This is expected: the user copies the address of that page and pastes it here.
  3. The helper replays /callback with the state cookie, then exchanges the code at the
     gateway's /token with the user's credential and the PKCE verifier. The gateway stores the
     Atlassian token encrypted, under this user.
  4. The gateway's /token response also contains the raw Atlassian tokens. The helper
     discards them without printing or saving them.
  5. The helper confirms the gateway stored the token, then shows the Atlassian account and
     sites the gateway now reaches as this user. If they are not yours, answer "n": the helper
     disconnects them again.

Treat the callback address like a password: anyone who has it and a gateway credential can
connect your Atlassian account to their gateway user. Never paste it anywhere else.

Standard library only.
Usage (from the litellm-mcp-gateway folder):
  export GATEWAY_URL=https://litellm.internal.example.com      # PROXY_BASE_URL, or https://<host>:8443 through the bastion
  export GATEWAY_CREDENTIAL_FILE=~/.config/litellm/credential  # IdP access token or virtual key
  python3 scripts/connect_atlassian.py [--server-id atlassian-confluence] [--no-browser] [--yes]
"""
import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_access import McpSession  # noqa: E402  (same folder, standard library only)

# Loopback redirect for the gateway's final hop. Nothing needs to listen on it: the helper
# reads the redirect target from the gateway's /callback response instead of following it.
CLIENT_REDIRECT_URI = "http://127.0.0.1:8765/callback"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def printable(text):
    """Drop control characters from text that came from a URL, so it cannot drive the terminal."""
    return "".join(ch for ch in str(text) if ch.isprintable())[:300]


def read_credential(path):
    if path:
        path = os.path.expanduser(path)
        if os.name == "posix" and os.stat(path).st_mode & 0o077:
            print(f"Warning: {path} is readable by other users; run: chmod 600 {path}", file=sys.stderr)
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    else:
        value = os.environ.get("GATEWAY_CREDENTIAL", "").strip()
    if not value:
        sys.exit("Set GATEWAY_CREDENTIAL_FILE (or GATEWAY_CREDENTIAL) to the user's credential")
    return value


def read_long_line(prompt):
    """Read one pasted line. Terminals in canonical mode cut a line at about 1 KB (macOS) and an
    Atlassian callback address is longer, so switch the terminal to non-canonical input for it."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    try:
        import termios
    except ImportError:  # Windows consoles accept long lines through input()
        return input().strip()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    raw = termios.tcgetattr(fd)
    raw[3] &= ~termios.ICANON
    raw[6][termios.VMIN], raw[6][termios.VTIME] = 1, 0
    chunks = []
    try:
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        while True:
            chunk = os.read(fd, 4096).decode(errors="ignore")
            if not chunk:
                break
            chunks.append(chunk)
            if "\n" in chunk or "\r" in chunk:
                break
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)
        print()
    return "".join(chunks).replace("\r", "\n").split("\n")[0].strip()


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def query_of(url):
    return {key: values[0] for key, values in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def main():
    parser = argparse.ArgumentParser(description="Connect your Atlassian account to the LiteLLM MCP gateway")
    parser.add_argument("--gateway", default=os.environ.get("GATEWAY_URL"),
                        help="Gateway base URL: PROXY_BASE_URL, or the same host on a forwarded port (default: $GATEWAY_URL)")
    parser.add_argument("--server-id", default="atlassian-confluence", help="MCP server_id in the gateway")
    parser.add_argument("--credential-file", default=os.environ.get("GATEWAY_CREDENTIAL_FILE"),
                        help="File with the user's gateway credential (default: $GATEWAY_CREDENTIAL_FILE)")
    parser.add_argument("--authorization-host", default="auth.atlassian.com",
                        help="Expected host of the consent page (the Atlassian v2 authorization server)")
    parser.add_argument("--no-browser", action="store_true", help="Print the consent URL instead of opening it")
    parser.add_argument("--yes", action="store_true", help="Do not ask to confirm the connected account")
    args = parser.parse_args()
    if not args.gateway:
        sys.exit("Set GATEWAY_URL or pass --gateway")

    gateway = args.gateway.rstrip("/")
    gateway_origin = urllib.parse.urlparse(gateway)
    if gateway_origin.scheme != "https" and gateway_origin.hostname not in ("localhost", "127.0.0.1"):
        sys.exit("The gateway URL must use https (http is accepted only for localhost testing)")
    credential = read_credential(args.credential_file)
    auth_header = {"Authorization": f"Bearer {credential}"}
    oauth_base = f"{gateway}/v1/mcp/server/oauth/{urllib.parse.quote(args.server_id, safe='')}"

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect)

    def call(request):
        try:
            response = opener.open(request, timeout=60)
            return response.status, response.headers, response.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()

    # 1. Start consent at the gateway; keep the state cookie.
    verifier, challenge = pkce_pair()
    client_state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode({
        "redirect_uri": CLIENT_REDIRECT_URI, "state": client_state, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    status, headers, body = call(urllib.request.Request(f"{oauth_base}/authorize?{params}", headers=auth_header))
    consent_url = headers.get("Location") if status in (302, 303, 307) else None
    if not consent_url:
        sys.exit(f"Gateway /authorize returned HTTP {status}: {body.decode(errors='replace')[:300]}")
    consent_parts = urllib.parse.urlparse(consent_url)
    if consent_parts.scheme != "https" or consent_parts.hostname != args.authorization_host:
        sys.exit(f"The gateway sent an unexpected consent page ({printable(consent_parts.hostname)}); stopping")
    upstream_state = query_of(consent_url).get("state")
    if not upstream_state or not any(c.name.startswith("mcp_oauth_state_") for c in jar):
        sys.exit("Gateway did not return a consent state; check the server is oauth2 authorization_code")

    # 2. User approves in the browser, then pastes the address of the callback page.
    print("\nApprove access in your browser. Atlassian then sends you to a gateway page that says the")
    print("sign-in is incomplete. That is expected. Copy the full address of that page and paste it here.")
    print("You have 10 minutes.\n")
    if args.no_browser or not webbrowser.open(consent_url):
        print(f"Open this URL in your browser:\n{consent_url}\n")
    pasted = read_long_line("Callback address: ")
    pasted_url = urllib.parse.urlparse(pasted)
    returned = query_of(pasted)
    if "error" in returned:
        sys.exit(f"Atlassian returned an error: {printable(returned.get('error'))} "
                 f"{printable(returned.get('error_description', ''))}")
    # Compare scheme and host, not port: through an SSM port forward GATEWAY_URL may be
    # https://<host>:8443 while Atlassian redirects to PROXY_BASE_URL, https://<host>/callback.
    if (pasted_url.scheme, pasted_url.hostname) != (gateway_origin.scheme, gateway_origin.hostname) \
            or not pasted_url.path.endswith("/callback"):
        sys.exit("That address is not this gateway's /callback page")
    if returned.get("state") != upstream_state or "code" not in returned:
        sys.exit("That callback does not belong to this consent attempt; run the helper again")

    # 3. Replay /callback with the state cookie; the gateway answers with a code for our loopback URI.
    callback = f"{gateway}/callback?" + urllib.parse.urlencode({"code": returned["code"], "state": upstream_state})
    status, headers, body = call(urllib.request.Request(callback))
    forwarded = headers.get("Location") or ""
    if not forwarded.startswith(CLIENT_REDIRECT_URI):
        sys.exit(f"Gateway /callback returned HTTP {status} without the expected redirect")
    forwarded_query = query_of(forwarded)
    if forwarded_query.get("state") != client_state or "code" not in forwarded_query:
        sys.exit("State mismatch on the gateway redirect; stopping")

    # 4. Exchange at the gateway as this user. The gateway stores the Atlassian token.
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": forwarded_query["code"],
        "redirect_uri": CLIENT_REDIRECT_URI, "code_verifier": verifier,
    }).encode()
    request = urllib.request.Request(f"{oauth_base}/token", data=form, headers={
        **auth_header, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    status, headers, body = call(request)
    if status != 200:
        sys.exit(f"Gateway /token returned HTTP {status}: {body.decode(errors='replace')[:300]}")
    # The body holds the raw Atlassian tokens. The gateway already stored them; drop this copy.
    granted_scope = json.loads(body).get("scope", "")
    del body

    # 5. The gateway answers 200 even if storing failed, so check, then show what was connected.
    status_url = f"{gateway}/v1/mcp/server/{urllib.parse.quote(args.server_id, safe='')}/oauth-user-credential"
    status, headers, body = call(urllib.request.Request(f"{status_url}/status", headers=auth_header))
    if status != 200 or not json.loads(body).get("has_credential"):
        sys.exit(f"The gateway did not store the Atlassian token (HTTP {status}); check the gateway logs")
    print("The gateway stored your Atlassian token; this helper kept no copy.")
    if granted_scope:
        print(f"Granted scopes: {printable(granted_scope)}")
    if not confirm_account(gateway, credential, args.yes):
        call(urllib.request.Request(status_url, method="DELETE", headers=auth_header))
        sys.exit("Disconnected again. Sign in to Atlassian with your own account and run the helper again.")
    print("Connected.")


def confirm_account(gateway, credential, assume_yes):
    """Show the Atlassian account and sites the gateway now reaches as this user."""
    session = McpSession(gateway + "/mcp/", credential)
    session.open("connect-atlassian")
    names = [tool["name"] for tool in session.request("tools/list").get("tools", [])]

    def run(suffix):
        name = next((n for n in names if n.endswith("-" + suffix)), None)
        if not name:
            return None
        result = session.request("tools/call", {"name": name, "arguments": {}})
        text = "".join(p.get("text", "") for p in result.get("content", []) if p.get("type") == "text")
        try:
            return json.loads(text)
        except ValueError:
            return None

    def data(reply):  # Atlassian v2 wraps results as {"data": {...}}
        return reply.get("data", reply) if isinstance(reply, dict) else {}

    account = data(run("atlassianUserInfo")).get("accountId")
    sites = data(run("getAccessibleAtlassianResources")).get("resources", [])
    print(f"Atlassian account ID: {printable(account or 'unknown')}")
    for site in sites if isinstance(sites, list) else []:
        print(f"  site: {printable(site.get('url', ''))}")
    if assume_yes:
        return True
    return read_long_line("Is this your Atlassian account and the sites you expect? [y/N] ").lower() in ("y", "yes")


if __name__ == "__main__":
    main()
