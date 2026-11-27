# Option A: sign in with your enterprise identity provider (recommended)

Users and their agents call the gateway with an access token (JWT) from your company identity provider (IdP). LiteLLM validates the token, creates the user on first sign-in, and maps the user's IdP groups to LiteLLM teams. Nobody needs a LiteLLM key, and moving a person to another IdP group changes which Confluence tools their agent can use, with no change in LiteLLM.

This option needs a LiteLLM Enterprise license: [JWT auth is an Enterprise feature](https://docs.litellm.ai/docs/proxy/token_auth). The Atlassian side is the same as in [option B](../option-b-virtual-key/).

## How it works

| Step | What happens |
|---|---|
| Sign-in | The agent sends `Authorization: Bearer <IdP access token>` on model and MCP calls. LiteLLM checks the signature against the IdP's JWKS, and checks `iss`, `aud` and expiry. |
| User | `sub` becomes the LiteLLM user ID (`user_id_upsert: true` creates the user on the first call). The gateway stores the user's Atlassian token under this ID. |
| Team | Each value in the `groups` claim that matches a LiteLLM `team_id` makes the user a member of that team. `sync_user_role_and_teams: true` removes memberships that are no longer in the token. The team's `mcp_tool_permissions` cap the tools. |
| Upstream | LiteLLM calls the Atlassian Rovo MCP server with the token it holds for this user. The IdP token never leaves the gateway. |

The configuration is in [`proxy/config.yaml`](proxy/config.yaml). The IdP connection comes from three environment variables. The [Terraform](../infra/terraform/) sets them from `jwt_public_key_url`, `jwt_issuer` and `jwt_audience`:

| Variable | Where to find it | Example |
|---|---|---|
| `JWT_PUBLIC_KEY_URL` | `jwks_uri` in the IdP's OpenID configuration (`<issuer>/.well-known/openid-configuration`) | `https://idp.example.com/oauth2/v1/keys` |
| `JWT_ISSUER` | `issuer` in the same document; must equal the token's `iss` exactly | `https://idp.example.com` |
| `JWT_AUDIENCE` | The audience you configure for the gateway application in the IdP; must appear in the token's `aud` | `litellm` |

## Identity provider requirements

- An OIDC application (or API audience) for the gateway, with a fixed audience value.
- Access tokens that carry `sub` (stable per person), `email` and `groups`.
- The `groups` claim lists group values exactly as you use them for LiteLLM `team_id`. Use immutable group IDs where the IdP offers them, not display names that someone could rename or recreate. Send only the gateway groups in the token if the IdP supports group filters, and never let users create groups whose value could match a team.
- A short access token lifetime, for example 1 hour. Disabling a person in the IdP stops new tokens; tokens already issued work until they expire. See offboarding in the [main README](../README.md#security-notes) for the stored Atlassian token.
- Only administrators may receive the `litellm_proxy_admin` scope (`admin_jwt_scope` in `config.yaml`): any token that carries it is a proxy admin.

## Teams

**One gateway group per person.** LiteLLM serves each request as one team: the first of the user's groups, in sorted order, that exists as a LiteLLM team. Tools are not combined across teams (tested on LiteLLM 1.109.1). If someone must be in two gateway groups, the agent can choose one per request with the `x-litellm-team-id` header; LiteLLM rejects a team that is not in the user's token with HTTP 403.

**A user removed from every gateway group loses access.** With `sync_user_role_and_teams: true`, the next request drops the old memberships and the user lists no tools. Without it, the user keeps the last team's tools (tested).

## Set up

Deploy the infrastructure with `edition = "enterprise"`, store the license and the Atlassian client, and start the gateway, as described in [`infra/terraform/README.md`](../infra/terraform/README.md#deploy). Run the commands below from this folder (`option-a-enterprise-idp/`).

1. **Create one team per IdP group.** Use an administrator key or an SSO admin session so changes are attributed; the master key is for the first setup only.
   ```bash
   export LITELLM_PROXY_URL=https://litellm.internal.example.com
   export LITELLM_ADMIN_KEY="$(aws secretsmanager get-secret-value \
     --secret-id "$(terraform -chdir=../infra/terraform output -raw master_key_secret_arn)" \
     --query SecretString --output text)"
   ../scripts/create_team.sh 00g1confluenceread
   ../scripts/create_team.sh 00g2confluencebrowse listConfluenceSpaces listConfluenceContent atlassianUserInfo
   unset LITELLM_ADMIN_KEY
   ```
   The team ID is the group value in the token (here, example immutable IDs). With no tool list, the team gets the five read tools of the server. A team can never use a tool that is not in the server's `allowed_tools`, so add `searchConfluence` to `config.yaml` first if a team needs it. LiteLLM never creates teams from the `groups` claim: a group without a matching team gives no access. The script also sets request and token rate limits per team (`TEAM_RPM_LIMIT`, `TEAM_TPM_LIMIT`) and an optional budget (`TEAM_MAX_BUDGET`).
2. **Each user connects Atlassian once.** The user gets an access token for the gateway audience from the IdP (for example with the IdP's CLI or the OAuth device authorization flow), saves it without the `Bearer ` prefix, and runs the shared helpers:
   ```bash
   mkdir -p ~/.config/litellm
   (umask 077; your-idp-cli get-token --audience litellm > ~/.config/litellm/credential)   # example command
   export GATEWAY_URL=https://litellm.internal.example.com
   export GATEWAY_CREDENTIAL_FILE=~/.config/litellm/credential
   python3 ../scripts/connect_atlassian.py
   python3 ../scripts/check_access.py
   ```
   `connect_atlassian.py` opens the Atlassian consent page (`--no-browser` prints the URL instead), asks for the address the browser lands on after approval, then shows the Atlassian account and sites that were connected and asks the user to confirm them. `check_access.py` lists the tools of the user's team and calls `atlassianUserInfo`, which must return the user's own Atlassian account. Access tokens expire, so fetch a new one when the helpers return HTTP 401; the stored Atlassian token stays valid.
3. **Run the sample agent.**
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r client/requirements.txt
   export LITELLM_PROXY_URL=https://litellm.internal.example.com
   export USER_ACCESS_TOKEN="$(cat ~/.config/litellm/credential)"
   python3 client/agent.py
   ```

## Run the agent on Amazon Bedrock AgentCore Runtime

- Configure the runtime with an [inbound JWT authorizer](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html) for the same IdP, so AgentCore accepts only signed-in users.
- Add `Authorization` to the runtime's [request header allowlist](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-header-allowlist.html). AgentCore passes the `Authorization` header to your code only when a custom JWT authorizer is configured.
- For each request, strip the `Bearer ` prefix and pass the token to `ask()` in [`client/agent.py`](client/agent.py). Never cache it across users, and never fall back to a shared credential.
- Run the runtime in VPC mode in subnets that can reach the internal load balancer, and attach the `gateway_clients_security_group_id` security group from the Terraform outputs.
- The audience in the token must be accepted by both the AgentCore authorizer and LiteLLM (`JWT_AUDIENCE`).

## What to test

| Check | Expected |
|---|---|
| User in one gateway group lists tools | The team's tools only, prefixed `atlassian_confluence-` |
| User who never ran `connect_atlassian.py` | `auth_required` for the server, no tools |
| Tampered or expired JWT | HTTP 401 |
| Call to a tool outside the team's list | Rejected by the gateway |
| User moved to another gateway group | Next token, new tool set, no change in LiteLLM |
| User removed from every gateway group | No tools |
| User tries to create a LiteLLM key (`POST /key/generate`) | Rejected: only administrators issue keys |
| `atlassianUserInfo` for two users | Each returns their own Atlassian account |
| LiteLLM spend logs | Each MCP call, allowed or denied, with the user's `sub` and team |
