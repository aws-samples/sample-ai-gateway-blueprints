# AI Gateway Blueprints for Amazon Bedrock

[![Checkov](https://github.com/aws-samples/sample-ai-gateway-blueprints/actions/workflows/checkov.yml/badge.svg?branch=main)](https://github.com/aws-samples/sample-ai-gateway-blueprints/actions/workflows/checkov.yml)
[![CodeQL](https://github.com/aws-samples/sample-ai-gateway-blueprints/actions/workflows/github-code-scanning/codeql/badge.svg?branch=main)](https://github.com/aws-samples/sample-ai-gateway-blueprints/actions/workflows/github-code-scanning/codeql)
[![License: MIT-0](https://img.shields.io/badge/license-MIT--0-blue)](LICENSE)

Reference configurations and code samples for running an **AI gateway** in front of [Amazon Bedrock](https://aws.amazon.com/bedrock/). Each blueprint targets one problem you hit when you take generative AI workloads to production: routing and failover, capacity and throttling, data residency, observability, and governed access to tools through the Model Context Protocol (MCP).

The samples cover two gateway families:

- **[LiteLLM](https://docs.litellm.ai/)**: an open-source, OpenAI-compatible model gateway. You can run it as a standalone proxy or embed it as a Python library (the `Router`).
- **[Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html)**: a managed service that turns APIs, AWS Lambda functions and existing services into MCP-compatible tools for agents, with built-in inbound and outbound authentication.

> These samples are for learning and reference. Review, test and adapt them to your own security, resilience and compliance requirements before you use them in production.

## What is an AI gateway?

An AI gateway sits between your applications or agents and the models and tools they call. It gives you one control point for:

| Concern | What the gateway provides |
|---------|---------------------------|
| Resilience | Load balancing across deployments, retries, cooldowns, and failover across AWS Regions and models when Bedrock returns `503` (service unavailable), `529` (overloaded) or `429` (throttled) |
| Data residency | Routing pools scoped to a geography (for example EU-only Bedrock cross-Region inference profiles), so failover never crosses a residency boundary you did not approve |
| Observability | Which model and deployment actually served each request, degrade rates, cost and token usage |
| Access and governance | Virtual keys, budgets, rate limits, and authenticated access to tools (MCP) |
| Portability | One OpenAI-compatible or MCP interface for clients, whatever the backing model or tool |

## Repository structure

```
.
├── Litellm/
│   ├── LiteLLM-routing-fallback-529-503/   # Bedrock routing and fallback with LiteLLM (proxy + SDK)
│   │   ├── proxy/                          # LiteLLM proxy config for the routing and fallback setup
│   │   ├── sdk/                            # Python Router tests and Bedrock placement measurement scripts
│   │   └── images/                         # Architecture and behaviour diagrams
│   └── litellm-mcp-gateway/                # (coming soon) LiteLLM as an MCP gateway
└── AgentCore-gateway/                      # (coming soon) Amazon Bedrock AgentCore Gateway samples
```

## Blueprints

| Blueprint | Gateway | Status | What it shows |
|-----------|---------|--------|---------------|
| [LiteLLM routing and fallback for 529/503](Litellm/LiteLLM-routing-fallback-529-503/) | LiteLLM proxy and Python SDK | Available | Routing for Claude Opus 5.5 from the EU profile to the global profile, then fallback to Claude Opus 5. Retry, cooldown and timeout settings for Bedrock overload errors, and scripts that measure which Region serves each call |
| LiteLLM MCP gateway | LiteLLM | Coming soon | Exposing and governing MCP tool servers through LiteLLM |
| AgentCore Gateway | Amazon Bedrock AgentCore Gateway | Coming soon | Turning APIs and Lambda functions into authenticated MCP tools for agents |

## Cost

These samples call Amazon Bedrock models, which incur charges per input and output token. Running a gateway on Amazon EKS, Amazon ECS or Amazon EC2 also incurs compute and networking charges. Failover tests that retry across Regions and models can multiply token usage. See [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) and use the [AWS Pricing Calculator](https://calculator.aws/) for estimates. Clean up any resources you create when you have finished.

## Contributing

Contributions are welcome, including new gateway blueprints. See [CONTRIBUTING](CONTRIBUTING.md) for guidelines and the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
