"""Agent that reads Confluence through the LiteLLM MCP gateway as the signed-in user (option A).

One credential travels on every call: the user's access token (JWT) from the company identity
provider, as "Authorization: Bearer". The gateway validates it, maps the user's IdP groups to
LiteLLM teams, and calls Atlassian with the Atlassian token it holds for that user. The agent
never holds an Atlassian token and needs no LiteLLM key.

In Amazon Bedrock AgentCore Runtime, the user's token reaches the agent as the inbound
Authorization header (configure a JWT authorizer and allow the Authorization header); pass it
to ask() for each request. Run locally with USER_ACCESS_TOKEN set.
"""
import os

from strands import Agent
from strands.models.litellm import LiteLLMModel
from strands.tools.mcp import MCPClient

LITELLM_PROXY_URL = os.environ["LITELLM_PROXY_URL"]  # internal load balancer endpoint
MCP_URL = LITELLM_PROXY_URL.rstrip("/") + "/mcp/"


def ask(question: str, user_access_token: str) -> str:
    model = LiteLLMModel(
        client_args={"api_key": user_access_token, "api_base": LITELLM_PROXY_URL},
        model_id="litellm_proxy/claude-sonnet",
    )
    headers = {"Authorization": f"Bearer {user_access_token}"}
    # MCPClient(url=...) owns its HTTP client and closes it when the block ends.
    client = MCPClient(url=MCP_URL, headers=headers)
    with client:
        agent = Agent(model=model, tools=client.list_tools_sync())
        return str(agent(question))


if __name__ == "__main__":
    print(ask("List the Confluence spaces I can see.", os.environ["USER_ACCESS_TOKEN"]))
