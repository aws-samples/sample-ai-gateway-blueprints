"""Option A agent on Amazon Bedrock AgentCore Runtime (tested).

The runtime's JWT authorizer accepts only access tokens from your IdP, and the Authorization
header allowlist passes the token to this code. Each request runs as the signed-in user: the
token goes to the gateway on model and MCP calls, and is never cached or replaced by a shared
credential. Same ask() as ../client/agent.py.
"""
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from strands import Agent
from strands.models.litellm import LiteLLMModel
from strands.tools.mcp import MCPClient

LITELLM_PROXY_URL = os.environ["LITELLM_PROXY_URL"]  # internal load balancer endpoint
MCP_URL = LITELLM_PROXY_URL.rstrip("/") + "/mcp/"

app = BedrockAgentCoreApp()


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


@app.entrypoint
def invoke(payload, context: RequestContext):
    authorization = (context.request_headers or {}).get("Authorization", "")
    return ask(payload["prompt"], authorization.removeprefix("Bearer "))


if __name__ == "__main__":
    app.run()
