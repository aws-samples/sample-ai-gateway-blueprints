# LiteLLM MCP gateway: per-user Confluence access for AgentCore agents

This blueprint shows how to give [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html) agents access to Confluence through a self-hosted [LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy) that acts as an MCP gateway on Amazon ECS. Each person's agent reads Confluence as that person, with that person's Confluence permissions. There is no shared Confluence credential, and the agent never holds an Atlassian token.

Two independent layers decide every request:

| Layer | Direction | Credential | Decides |
|---|---|---|---|
| Governance | Agent to gateway | The user's gateway credential: an identity provider (IdP) access token in option A, the user's own LiteLLM virtual key in option B | Which MCP servers and tools the user's agent can list and call (LiteLLM team) |
| Identity | Gateway to Confluence | The user's Atlassian OAuth token, held and refreshed by the gateway | Which content Confluence returns, and as whom |

An agent gets a result only when both layers allow it. The gateway is the OAuth client of the [Atlassian Rovo MCP server](https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server/). Each user consents once; the gateway stores that user's token encrypted in its database, keyed by the gateway user, and refreshes it without user action. The caller's credential is never forwarded upstream, as the [MCP security best practices](https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices) and the [AWS Prescriptive Guidance on MCP governance](https://docs.aws.amazon.com/prescriptive-guidance/latest/mcp-strategies/mcp-governance-strategy.html) recommend.

## Choose an option

| | [Option A: enterprise identity provider sign-in](option-a-enterprise-idp/) (recommended) | [Option B: per-user virtual key](option-b-virtual-key/) |
|---|---|---|
| How users sign in to the gateway | An access token (JWT) from your company IdP | A LiteLLM virtual key issued to each person |
| Team membership | IdP groups map to LiteLLM teams; change a group in the IdP and the user's tools change | An administrator adds each user to a team |
| Per-person setup in LiteLLM | None: users are created on first sign-in | One user and one key per person ([`add_user.sh`](option-b-virtual-key/scripts/add_user.sh)) |
| Audit log of administrator changes | Yes | No (the community edition writes no audit log) |
| LiteLLM edition | Enterprise license ([JWT auth is an Enterprise feature](https://docs.litellm.ai/docs/proxy/token_auth)) | Community edition |
| Atlassian side (per-user OAuth consent, also called three-legged OAuth or 3LO; per-user token store; tool allowlists) | Same | Same |

Both options use the same MCP server definition, the same scripts and the same infrastructure. Only the inbound credential differs.

## Architecture

![The agent on AgentCore Runtime calls the internal Application Load Balancer with the user's credential. LiteLLM on Amazon ECS checks the user's team, calls the Atlassian Rovo MCP server with the Atlassian token it stores for that user, and calls Amazon Bedrock for models.](images/litellm-mcp-gateway-architecture.png)

Request path: the agent on AgentCore Runtime calls the gateway's internal Application Load Balancer with the user's credential. LiteLLM authenticates the user, checks the team's tool permissions, looks up that user's Atlassian token, and calls `https://mcp.atlassian.com/v2/mcp`. Model calls go to Amazon Bedrock through the same gateway. Everything runs in private subnets; only outbound HTTPS leaves the VPC, through a NAT gateway.

One-time consent per user ([LiteLLM MCP OAuth](https://docs.litellm.ai/docs/mcp_oauth), OAuth 2.1 with PKCE):

1. The user runs [`scripts/connect_atlassian.py`](scripts/connect_atlassian.py) with their gateway credential. The gateway returns the Atlassian consent URL and a one-time state cookie.
2. The user approves in the browser. Atlassian redirects to `<PROXY_BASE_URL>/callback`, and the user pastes that address into the helper.
3. The helper completes the exchange at the gateway, which stores the Atlassian access and refresh tokens encrypted with `LITELLM_SALT_KEY`, under that user and MCP server. The helper then shows the Atlassian account and sites that were connected and asks the user to confirm.

### Names used in this sample

| Name | Meaning |
|---|---|
| `atlassian-confluence` | The MCP server's `server_id`, used in team permissions, API paths and `--server-id` |
| `atlassian_confluence` | The MCP server's key in `config.yaml`, which LiteLLM uses as the tool prefix, for example `atlassian_confluence-getConfluenceContent` |
| `gateway_url` (Terraform output), `PROXY_BASE_URL` (LiteLLM), `GATEWAY_URL` (Python scripts), `LITELLM_PROXY_URL` (shell scripts and `agent.py`) | All hold `https://<gateway_hostname>`. Through the bastion port forward, use `https://<gateway_hostname>:8443` for the last two |

## Contents

```
litellm-mcp-gateway/
├── scripts/                         # Shared by both options
│   ├── register_atlassian_client.py # Register the gateway as an OAuth client of Atlassian (once)
│   ├── create_team.sh               # One LiteLLM team with a tool ceiling (per IdP group in option A)
│   ├── connect_atlassian.py         # One-time consent for one user
│   └── check_access.py              # List the user's tools and call one
├── option-a-enterprise-idp/         # Recommended: IdP sign-in, LiteLLM Enterprise
│   ├── proxy/config.yaml
│   └── client/                      # Strands Agents agent that sends the user's IdP token
├── option-b-virtual-key/            # LiteLLM community edition
│   ├── proxy/config.yaml
│   ├── scripts/add_user.sh          # One user and one virtual key per person
│   └── client/                      # Strands Agents agent that sends the user's virtual key
├── infra/terraform/                 # Private ECS Fargate, Aurora, internal ALB, AWS WAF, KMS, SSM bastion
└── images/
```

The Python scripts use the standard library only. The sample agents use [Strands Agents](https://strandsagents.com/).

## Prerequisites

- An AWS account with [Amazon Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) to Anthropic Claude Sonnet 4.6 in the Region you deploy to. The sample uses the `eu.anthropic.claude-sonnet-4-6` cross-Region inference profile in `eu-west-1`. The first use of an Anthropic model in an account needs the Anthropic first-time-use form, and an administrator with AWS Marketplace subscribe permissions must invoke the model once; the gateway's task role deliberately has no Marketplace permissions.
- A private DNS name for the gateway, and an AWS Certificate Manager certificate for it in the deployment Region, for example from [AWS Private CA](https://docs.aws.amazon.com/privateca/latest/userguide/PcaWelcome.html). Every client (administrator and user workstations, and the agent runtime) must trust the issuing CA. For the Python scripts, point `SSL_CERT_FILE` at the CA chain, for example `export SSL_CERT_FILE=~/private-ca-chain.pem`.
- A private network path from users and agents to the VPC (corporate network, AWS Client VPN, VPC peering or AWS Transit Gateway). The gateway is never exposed to the internet.
- Terraform 1.11 or later, the AWS CLI v2 with the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html), and Python 3.10 or later.
- An Atlassian Cloud site with Confluence and the Atlassian Rovo MCP server. Before the first user consents, an Atlassian organization admin adds the gateway origin (for example `https://litellm.internal.example.com`) to the domains allowed to connect: by default only `localhost`, `127.0.0.1` and domains that Atlassian lists can complete OAuth 2.1 sign-in ([Available Atlassian MCP server domains](https://support.atlassian.com/security-and-access-policies/docs/available-atlassian-mcp-server-domains/)). Allow only the gateway's domain. If the organization uses an IP allowlist, also allow the gateway's NAT addresses (Terraform output `nat_public_ips`).
- Option A only: a LiteLLM Enterprise license, and an OIDC IdP that issues access tokens with `sub`, `email` and a `groups` claim for a fixed audience.

## Deploy

Follow these in order:

1. Deploy the infrastructure, register the gateway at Atlassian, store the secrets, start the gateway and set up DNS: [`infra/terraform/README.md`](infra/terraform/README.md#deploy).
2. Create the teams, then (option B) the users and keys, then have each user connect Atlassian and run `check_access.py`: [option A set-up](option-a-enterprise-idp/README.md#set-up) or [option B set-up](option-b-virtual-key/README.md#set-up).

## Verify per-user attribution

- **Atlassian:** in Atlassian Administration, the audit log shows each Confluence read under the person who made it, not a service account.
- **LiteLLM:** each MCP call, allowed or denied, is written to the spend logs with the gateway user, the team, the namespaced tool name and the status. Request and response content is not stored by default.
- **Isolation:** a user who has not consented gets `auth_required` for the server, and a user's gateway credential sent directly to `https://mcp.atlassian.com/v2/mcp` is rejected with HTTP 401.

## Security notes

These were observed on LiteLLM 1.109.1 with the Atlassian Rovo MCP server (v2) and shape the sample.

**Atlassian connection**

- **Use the v2 authorization server.** Discover it from the v2 protected resource metadata (`https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp`), as `register_atlassian_client.py` does. The host root metadata describes the v1 authorization server, and its tokens fail on `/v2/mcp`.
- **Set `upstream_resource`.** LiteLLM sends the [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) `resource` parameter only when it is configured. Without it, Atlassian rejects the token with `invalid_token`.
- **Keep `allowed_tools` explicit.** An empty list exposes every upstream tool. The scope list in `config.yaml` is the default request, not a ceiling: a user can ask for more scopes at consent. `allowed_tools`, narrowed per team by `mcp_tool_permissions`, is the control.
- **Consent is site-scoped.** A user's token works only for the Atlassian sites chosen at consent; other sites return "isn't explicitly granted by the user".

**Consent and tokens**

- **Treat the callback address as a password.** The gateway cannot check that the Atlassian account that consents belongs to the gateway user who started the flow: the Atlassian v2 authorization server issues no ID token to compare. Someone who starts a flow with their own gateway credential and obtains another person's callback address can bind that person's Atlassian account to their own gateway user. `connect_atlassian.py` shows the connected account and sites and disconnects them if the user says they are not theirs. Tell users never to share the callback address, and allow only the gateway domain at Atlassian.
- **The gateway's `/token` endpoint returns raw Atlassian tokens.** It returns them to the consent client as well as storing them, and it accepts refresh grants from anyone who holds a refresh token. `connect_atlassian.py` discards them. The AWS WAF web ACL in `infra/terraform` blocks refresh grants through the gateway, which never needs them: it refreshes stored tokens internally.
- **The callback carries a one-time code.** `LITELLM_DISABLE_ACCESS_LOG_PATHS=/callback` keeps it out of the gateway access log, and the WAF log redacts query strings. Restrict who can read load balancer access logs if you enable them.
- **Never rotate `LITELLM_SALT_KEY`** after the first user connects. It encrypts every stored token; changing it makes them unreadable. Anyone with the salt key and a copy of the database can decrypt every user's token, so treat a leak of either as an incident: delete all stored credentials, have the Atlassian admin revoke the gateway app's access, then rotate the salt key and have users consent again.

**Users, teams and keys**

- **One gateway group per person (option A).** LiteLLM runs each request as one team: the first of the user's groups, in sorted order, that exists as a LiteLLM team. Tools are not combined across teams. `sync_user_role_and_teams: true` in the option A config removes memberships that are no longer in the token; without it, a user removed from every group keeps the last team's tools (tested). Use immutable group IDs, emit only gateway groups in the token, and never let users create groups whose name or ID could match a team.
- **Only administrators issue keys.** Both configs set `key_generation_settings` so users cannot mint keys for themselves, and `upperbound_key_generate_params` caps every key at 30 days (tested).
- **Administrators can act as any connected user.** A proxy admin can issue a key for another user and read Confluence as that person, and can add MCP servers through the API; LiteLLM 1.109.1 does not audit MCP server changes. Keep the admin role to a few named people who sign in through SSO, keep the master key as break-glass only, and alert on reads of the master key secret in AWS CloudTrail. LiteLLM Admin UI SSO is [free for up to 5 users](https://docs.litellm.ai/docs/proxy/admin_ui_sso); in the code, that limit counts every LiteLLM user, so option B deployments with more than 5 users need a license for SSO.
- **The admin API shares the listener.** Every client range in `allowed_client_cidrs` reaches the management endpoints too (they are protected by LiteLLM roles). Keep those ranges narrow, or add load balancer rules that allow `/ui`, `/key`, `/team`, `/user` and MCP server management only from administrator ranges.

**Offboarding**

1. Disable the user in the IdP (option A) or delete the LiteLLM user, which deletes their keys (option B). Option A tokens already issued keep working until they expire; a deleted key can keep working for up to 60 seconds on other gateway tasks.
2. Delete the stored Atlassian token:
   ```bash
   curl -fsS -X DELETE -H "Authorization: Bearer $LITELLM_ADMIN_KEY" \
     "$LITELLM_PROXY_URL/v1/mcp/server/atlassian-confluence/oauth-user-credential?user_id=<user-id>"
   ```
   Users can call it without `user_id` for themselves. Each gateway task caches a user's Atlassian access token until 60 seconds before it expires, and the delete clears the cache only on the task that served it. For immediate effect, also restart the service ([force a new deployment](infra/terraform/README.md#rotate-the-database-password) with `aws ecs update-service`).
3. To end the grant at Atlassian too, ask the Atlassian admin to deactivate the account or remove its access to the gateway app.

**Content and runtime**

- **Guardrails on tool results.** In LiteLLM 1.109.1 the Amazon Bedrock Guardrails integration has no `post_mcp_call` hook. To screen Confluence content, attach a guardrail with `mode: pre_call` and `scan_only_tool_results: true` (commented example in each `config.yaml`), so content is checked when the agent sends it back to the model through the gateway. Read-only scopes and explicit tools limit what an injected instruction in a page can do.
- **Outbound traffic.** The gateway tasks hold the salt key and every token, and need outbound HTTPS to `mcp.atlassian.com`, `auth.atlassian.com`, your IdP's JWKS host, `ghcr.io` and `pkg-containers.githubusercontent.com` (or Amazon ECR) and `truststore.pki.rds.amazonaws.com`. In production, restrict egress to those domains with AWS Network Firewall or an egress proxy ([outbound traffic](infra/terraform/README.md#outbound-traffic)).
- **`available_on_public_internet: false`** limits the server to callers from internal IP ranges. Behind a load balancer, LiteLLM sees the load balancer's private address; the network controls in `infra/` keep the gateway private.
- **Never enable debug logging in production** (`LITELLM_LOG=DEBUG`): debug logs include request headers and bodies.

## Clean up

1. While the gateway is still running, delete each user's stored token (offboarding step 2), and ask the Atlassian admin to remove the gateway app's access.
2. Destroy the infrastructure as described in [`infra/terraform/README.md`](infra/terraform/README.md#clean-up).
3. Delete the local `atlassian-oauth-client.json` and any `*.key` files.

## Cost

You pay for Amazon Bedrock tokens and for the resources in `infra/terraform`: NAT gateway (hourly and per GB), Application Load Balancer, AWS WAF (web ACL, rules and requests), Fargate tasks, Aurora Serverless v2 capacity and storage, interface VPC endpoints (hourly per Availability Zone), the bastion instance, AWS KMS, Secrets Manager and CloudWatch Logs. Use the [AWS Pricing Calculator](https://calculator.aws/) for an estimate. Option A also needs a LiteLLM Enterprise license.

## Security

See [CONTRIBUTING](../../CONTRIBUTING.md#security-issue-notifications).

## License

MIT-0. See [LICENSE](../../LICENSE).
