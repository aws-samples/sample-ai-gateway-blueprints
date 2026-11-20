# LiteLLM MCP gateway: per-user Confluence access for AgentCore agents

This blueprint shows how to expose Confluence to Amazon Bedrock AgentCore agents through a self-hosted LiteLLM MCP gateway, with two independent authorization layers:

| Layer | Direction | Credential | Decides |
|---|---|---|---|
| Governance | Agent to gateway | LiteLLM virtual key (team access group) | Which MCP servers and tools the agent can list and call |
| Identity | Gateway to Confluence | User's own OAuth 2.1 token | Which content Confluence returns, and as whom |

An agent gets a result only when both checks pass. The gateway stores no Confluence credential, and the pattern runs on the LiteLLM community edition.

## Architecture

![LiteLLM MCP gateway architecture: an AgentCore Runtime agent calls LiteLLM on Amazon ECS through an internal NLB; LiteLLM checks the team virtual key and forwards the user's OAuth token to the Atlassian MCP server](images/litellm-mcp-gateway-architecture.png)

The gateway side is configured in [`proxy/config.yaml`](proxy/config.yaml) (or registered at runtime with [`scripts/register_mcp_server.sh`](scripts/register_mcp_server.sh)). The agent side, which sends the team virtual key and the user's token, is [`client/agent.py`](client/agent.py).

## Contents

```
litellm-mcp-gateway/
├── proxy/config.yaml               # Gateway config with a read-only Confluence MCP server
├── scripts/register_mcp_server.sh  # Register the server in the DB and create a team key
├── client/
│   ├── agent.py                    # Strands agent that sends both headers
│   └── requirements.txt
└── images/
    └── litellm-mcp-gateway-architecture.png
```

## Prerequisites

- LiteLLM proxy (community edition, tested with 1.109.1) on Amazon ECS with AWS Fargate, backed by Aurora PostgreSQL, with `store_model_in_db: true`
- A private entry point for agents, such as an internal Network Load Balancer. No public exposure
- Amazon Bedrock model access in the same Region
- `LITELLM_MASTER_KEY` in AWS Secrets Manager
- An Atlassian Cloud organization that allows the [Atlassian MCP server](https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server/) (v2). `localhost` origins are allowed by default. For a hosted chat UI, an admin adds its origin under **Atlassian Administration > Connectors > Atlassian MCP server > Add domain** ([domain rules](https://support.atlassian.com/security-and-access-policies/docs/available-atlassian-mcp-server-domains/))
- Users consent with these OAuth scopes on the resource `https://mcp.atlassian.com/v2/mcp`: `read:me`, `read:account`, `offline_access`, `read:confluence:agent-interface`, `search:confluence:agent-interface`

## Deploy

1. Use `proxy/config.yaml` as the gateway config, or register the server at runtime:
   ```bash
   export LITELLM_PROXY_URL=https://litellm.internal.example.com
   export LITELLM_MASTER_KEY=sk-...
   ./scripts/register_mcp_server.sh
   ```
   Servers stored in the database are picked up without a restart. Changes to `config.yaml` need a new ECS deployment.
2. Confirm the tools the gateway exposes. LiteLLM prefixes each tool with the server name, so you should see exactly the five allowlisted tools, for example `atlassian_confluence_read-listConfluenceSpaces`:
   ```bash
   curl -sS "$LITELLM_PROXY_URL/mcp/" \
     -H "x-litellm-api-key: Bearer $LITELLM_VIRTUAL_KEY" \
     -H "x-mcp-atlassian_confluence_read-authorization: Bearer $USER_OAUTH_TOKEN" \
     -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
   ```
3. Run the sample agent:
   ```bash
   pip install -r client/requirements.txt
   export LITELLM_VIRTUAL_KEY=sk-... USER_OAUTH_TOKEN=...
   python client/agent.py
   ```

## Verify per-user attribution

In Atlassian Administration, open **Security > Audit log**. Each Confluence read should appear under the calling user, not a service account.

## Notes

- **Header names:** some proxies (nginx by default) drop headers that contain underscores. If yours does, use a hyphenated server name such as `confluence-read`.
- **Atlassian MCP v1 to v2:** this blueprint uses `https://mcp.atlassian.com/v2/mcp?tools=all`, the flat tool list Atlassian recommends for MCP gateways. On March 1, 2027, v1 connections (`/v1/mcp` and `/v1/mcp/authv2`) start exposing v2 tools, so v1 tool names such as `getConfluenceSpaces` stop matching an allowlist. v2 is a separate OAuth resource: v1 tokens do not carry over, and each user consents once more. v2 uses the `*:confluence:agent-interface` scopes instead of the v1 Confluence scopes. See [How to upgrade from Atlassian MCP v1 to v2](https://support.atlassian.com/atlassian-ai-gateway/docs/how-to-upgrade-from-atlassian-mcp-v1-to-atlassian-mcp-v2/).
- **Tool names:** the v2 read tools in this allowlist are `listConfluenceSpaces`, `listConfluenceContent` (pages in a space), `getConfluenceContent`, `getAccessibleAtlassianResources` (returns the `cloudId` the other tools need) and `atlassianUserInfo`. To list everything your tenant offers, run `tools/list` directly against the upstream URL with a user token.
- **Search:** to let agents search, add `searchConfluence` (CQL) to `allowed_tools`.
- **Least privilege:** register the same upstream as several servers (for example `confluence_read` and `confluence_write`), each with its own `allowed_tools` and access group.
- **Token handling:** keep the user token in session memory with a short TTL. Do not persist it.

## Clean up

1. Remove the `confluence_read` access group from team keys.
2. Delete the MCP server from the gateway (Admin UI or API), or remove it from `config.yaml` and redeploy.
3. Delete any ECS, Aurora, ElastiCache and load balancer resources you created for testing.

## Cost

Amazon Bedrock charges per token. Amazon ECS, Aurora, ElastiCache and Elastic Load Balancing charge for the resources you run. Use the AWS Pricing Calculator for estimates.

## Security

See [CONTRIBUTING](../../CONTRIBUTING.md#security-issue-notifications).

## License

MIT-0. See [LICENSE](../../LICENSE).
