"""AgentCore agent that reads Confluence through the LiteLLM MCP gateway.

Two credentials travel on every call:
  x-litellm-api-key                             team virtual key (gateway access control)
  x-mcp-atlassian_confluence_read-authorization user's OAuth token (forwarded upstream
                                                as Authorization, so Confluence acts as the user)
"""
import os

from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models.litellm import LiteLLMModel
from strands.tools.mcp import MCPClient

LITELLM_PROXY_URL = os.environ["LITELLM_PROXY_URL"]  # private NLB endpoint
VIRTUAL_KEY = os.environ["LITELLM_VIRTUAL_KEY"]
MCP_URL = LITELLM_PROXY_URL.rstrip("/") + "/mcp/"
MCP_SERVER_ALIAS = "atlassian_confluence_read"


def mcp_transport(user_token: str):
    headers = {"x-litellm-api-key": "Bearer " + VIRTUAL_KEY}
    if user_token:
        headers[f"x-mcp-{MCP_SERVER_ALIAS}-authorization"] = "Bearer " + user_token
    return streamablehttp_client(MCP_URL, headers=headers)


def ask(question: str, user_token: str) -> str:
    model = LiteLLMModel(
        client_args={"api_key": VIRTUAL_KEY, "api_base": LITELLM_PROXY_URL},
        model_id="litellm_proxy/claude-sonnet",
    )
    client = MCPClient(lambda: mcp_transport(user_token))
    with client:
        agent = Agent(model=model, tools=client.list_tools_sync())
        return str(agent(question))


if __name__ == "__main__":
    # USER_OAUTH_TOKEN comes from the interactive Atlassian consent (OAuth 2.1 + PKCE).
    print(ask("List the Confluence spaces I can see.", os.environ["USER_OAUTH_TOKEN"]))
