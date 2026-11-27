"""Agent that reads Confluence through the LiteLLM MCP gateway as one user (option B).

One credential travels on every call: the user's own LiteLLM virtual key, as
"Authorization: Bearer". The key identifies the person, so the gateway calls Atlassian with the
Atlassian token it holds for that user, within the tool ceiling of the user's team. The agent
never holds an Atlassian token.

Use one key per person. Run locally with USER_VIRTUAL_KEY set; in a hosted agent, load the
calling user's key from your secret store for each request.
"""
import os

from strands import Agent
from strands.models.litellm import LiteLLMModel
from strands.tools.mcp import MCPClient

LITELLM_PROXY_URL = os.environ["LITELLM_PROXY_URL"]  # internal load balancer endpoint
MCP_URL = LITELLM_PROXY_URL.rstrip("/") + "/mcp/"


def ask(question: str, user_virtual_key: str) -> str:
    model = LiteLLMModel(
        client_args={"api_key": user_virtual_key, "api_base": LITELLM_PROXY_URL},
        model_id="litellm_proxy/claude-sonnet",
    )
    headers = {"Authorization": f"Bearer {user_virtual_key}"}
    # MCPClient(url=...) owns its HTTP client and closes it when the block ends.
    client = MCPClient(url=MCP_URL, headers=headers)
    with client:
        agent = Agent(model=model, tools=client.list_tools_sync())
        return str(agent(question))


if __name__ == "__main__":
    print(ask("List the Confluence spaces I can see.", os.environ["USER_VIRTUAL_KEY"]))
