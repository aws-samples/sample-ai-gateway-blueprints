"""
Claude Opus 5.5 on Amazon Bedrock: HARD EU-FIRST routing (LiteLLM SDK Router).
"EU unless EU is down." Proxy equivalent: ../proxy/litellm-config-hard-eu.yaml

The primary alias pool (claude-opus-5-5) is EU only, so 100% of normal traffic
is served from one Region (eu-north-1) with one warm prompt cache. Each other
geo is a separate alias reached only through an ordered `fallbacks` chain:
EU -> US -> JP -> AU -> global. LiteLLM moves to the next alias only after the
current alias's pool is exhausted (retries and cooldowns), so cross-geo cache
writes happen only during an EU outage.

Serving Regions measured in one account (placement can differ in yours):
    eu. -> eu-north-1      us. -> us-east-1      jp. -> ap-northeast-1
    au. -> ap-southeast-4  global. -> worldwide (ap-southeast-4 when measured)

No data-residency constraint is assumed: failover sends requests outside the EU.

Credentials: set LITELLM_ROLE_ARN to a role to assume, or leave it unset to use
your calling credentials. Tested on LiteLLM 1.109.1.

Run:
    pip install "litellm==1.109.1"
    python litellm-router-hard-eu.py
"""

import asyncio
import os

from litellm import Router
from litellm.router import RetryPolicy

ROLE_ARN = os.environ.get("LITELLM_ROLE_ARN")  # optional


def deployment(alias: str, profile: str, entry_region: str, geo: str, tier: str) -> dict:
    params = {
        "model": f"bedrock/{profile}",
        "aws_region_name": entry_region,
        "timeout": 300,  # above the p99 of your longest generations
        # requestMetadata appears in Bedrock invocation logs, next to inferenceRegion
        "requestMetadata": {
            "app": "litellm-sdk",
            "alias": alias,
            "geo": geo,
            "tier": tier,
            "entry_region": entry_region,
        },
    }
    if ROLE_ARN:
        params["aws_role_name"] = ROLE_ARN
    return {"model_name": alias, "litellm_params": params}


model_list = [
    # Primary: EU only. Two entry Regions give entry-path redundancy; both serve
    # from eu-north-1, so the fallback chain covers an eu-north-1 capacity event.
    deployment("claude-opus-5-5", "eu.anthropic.claude-opus-5-5", "eu-central-1", "eu", "primary"),
    deployment("claude-opus-5-5", "eu.anthropic.claude-opus-5-5", "eu-west-1", "eu", "primary-entry2"),
    # Routing aliases: same model, one geo each, reached only via the fallbacks chain.
    deployment("claude-opus-5-5-us", "us.anthropic.claude-opus-5-5", "us-east-1", "us", "route-1"),
    deployment("claude-opus-5-5-jp", "jp.anthropic.claude-opus-5-5", "ap-northeast-1", "jp", "route-2"),
    # AU before global: both landed on ap-southeast-4 when measured.
    deployment("claude-opus-5-5-au", "au.anthropic.claude-opus-5-5", "ap-southeast-2", "au", "route-3"),
    # Last resort: global profile, two entry Regions.
    deployment("claude-opus-5-5-global", "global.anthropic.claude-opus-5-5", "eu-central-1", "global", "last-resort"),
    deployment("claude-opus-5-5-global", "global.anthropic.claude-opus-5-5", "us-east-1", "global", "last-resort"),
]

retry_policy = RetryPolicy(
    RateLimitErrorRetries=3,           # 429
    ServiceUnavailableErrorRetries=5,  # 503
    InternalServerErrorRetries=3,      # 500
    TimeoutErrorRetries=2,
    # A 529 surfaces as APIError, which has no RetryPolicy field: it uses num_retries.
)

router = Router(
    model_list=model_list,
    routing_strategy="simple-shuffle",  # spreads load across the two EU entries
    num_retries=5,
    retry_after=1,
    allowed_fails=2,
    cooldown_time=30,
    retry_policy=retry_policy,
    enable_pre_call_checks=True,
    fallbacks=[
        {"claude-opus-5-5": [
            "claude-opus-5-5-us",
            "claude-opus-5-5-jp",
            "claude-opus-5-5-au",
            "claude-opus-5-5-global",
        ]}
    ],
    # With several gateway workers, share cooldown state through Redis:
    #   redis_host=..., redis_port=..., redis_password=...
)


async def main():
    resp = await router.acompletion(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "Reply with one word: ok"}],
    )
    print("served by deployment:", resp._hidden_params.get("model_id"))
    print(resp.choices[0].message.content)
    # The serving Region is not in the response. Read inferenceRegion from the
    # Bedrock invocation log in the entry Region, filtered by requestMetadata.


if __name__ == "__main__":
    asyncio.run(main())
