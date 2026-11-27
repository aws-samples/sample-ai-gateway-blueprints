#!/usr/bin/env python3
"""Register the gateway as an OAuth client of the Atlassian MCP server v2. Run once per gateway.

The gateway, not the agent, is the OAuth client: Atlassian redirects each user's consent
back to <PROXY_BASE_URL>/callback, and the gateway exchanges and stores the token.

Steps:
  1. Read the v2 protected resource metadata (RFC 9728) to find the authorization server.
     Do not use https://mcp.atlassian.com/.well-known/oauth-authorization-server: that
     document describes the v1 authorization server, whose tokens fail on /v2/mcp.
  2. Read the authorization server metadata (RFC 8414) and register a confidential client
     with dynamic client registration (RFC 7591).
  3. Write the result (client_id, client_secret and the endpoints) to a file with mode 0600.
     Nothing secret is printed.

Standard library only.
Usage (from the litellm-mcp-gateway folder):
  python3 scripts/register_atlassian_client.py \\
    --redirect-uri "$(terraform -chdir=infra/terraform output -raw atlassian_redirect_uri)" [--out atlassian-oauth-client.json]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

RESOURCE_METADATA_URL = "https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp"


def fetch_json(url, body=None):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        sys.exit(f"HTTP {err.code} from {url}: {err.read().decode(errors='replace')[:300]}")


def fetch_metadata(issuer):
    """RFC 8414 section 3.1: the well-known segment goes between the host and the issuer path.
    Fall back to appending it, which some servers (Atlassian included) also serve."""
    parts = urllib.parse.urlparse(issuer)
    path = parts.path.rstrip("/")
    candidates = [f"{parts.scheme}://{parts.netloc}/.well-known/oauth-authorization-server{path}"]
    if path:
        candidates.append(issuer.rstrip("/") + "/.well-known/oauth-authorization-server")
    for url in candidates:
        try:
            metadata = fetch_json(url)
        except SystemExit:
            continue
        if metadata.get("issuer", "").rstrip("/") == issuer.rstrip("/"):
            return metadata
    sys.exit(f"No authorization server metadata with issuer {issuer}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--redirect-uri", required=True,
                        help="The gateway callback, exactly <PROXY_BASE_URL>/callback")
    parser.add_argument("--client-name", default="litellm-mcp-gateway")
    parser.add_argument("--out", default="atlassian-oauth-client.json")
    args = parser.parse_args()

    redirect = urllib.parse.urlparse(args.redirect_uri)
    if redirect.path.rstrip("/") != "/callback" or redirect.query or redirect.fragment:
        sys.exit("--redirect-uri must be <PROXY_BASE_URL>/callback with no query string")
    if redirect.scheme != "https" and redirect.hostname not in ("localhost", "127.0.0.1"):
        sys.exit("--redirect-uri must use https (http is accepted only for localhost testing)")
    if os.path.exists(args.out):
        sys.exit(f"{args.out} already exists; move it away first so a client secret is not overwritten")

    resource = fetch_json(RESOURCE_METADATA_URL)
    issuer = resource["authorization_servers"][0]
    metadata = fetch_metadata(issuer)
    if "S256" not in metadata.get("code_challenge_methods_supported", []):
        sys.exit("Authorization server does not advertise PKCE S256")

    registration = fetch_json(metadata["registration_endpoint"], {
        "client_name": args.client_name,
        "redirect_uris": [args.redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    })
    if not registration.get("client_secret"):
        sys.exit("Registration returned no client_secret; the gateway needs a confidential client")

    result = {
        "client_id": registration["client_id"],
        "client_secret": registration["client_secret"],
        "client_secret_expires_at": registration.get("client_secret_expires_at", 0),
        "redirect_uri": args.redirect_uri,
        "resource": resource["resource"],
        "issuer": metadata["issuer"],
        "authorization_url": metadata["authorization_endpoint"],
        "token_url": metadata["token_endpoint"],
        "revocation_url": metadata.get("revocation_endpoint"),
    }
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(result, handle, indent=2)

    print(f"Registered client_id {result['client_id']}; credentials written to {args.out} (mode 0600)")
    print(f"authorization_url {result['authorization_url']}")
    print(f"token_url         {result['token_url']}")
    print(f"resource          {result['resource']}")


if __name__ == "__main__":
    main()
