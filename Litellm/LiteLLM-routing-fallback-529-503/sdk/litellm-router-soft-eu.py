"""
Claude Opus 5.5 on Amazon Bedrock: SOFT EU-FIRST (weighted) routing (LiteLLM
SDK Router). Proxy equivalent: ../proxy/litellm-config-soft-eu.yaml

Applications call one alias, claude-opus-5-5, whose pool holds one geo profile
per geography. simple-shuffle is a weighted random pick, not strict priority:
EU 10 (two entries of 5), US 3, JP 2, AU 1, so under normal load EU gets about
10/16 (63%) of traffic and the other geos take the rest. Measured on LiteLLM
1.109.1 over 20,000 picks: EU 62%, US 19%, JP 12%, AU 6%.

If EU fails, retries and cooldown move its share to the remaining geos. If the
whole pool fails, requests go to claude-opus-5-5-global.

Cache trade-off: prompt caches are per serving Region, so this shape keeps up
to four regional caches warm and pays a full cache write in each as it expires,
even when nothing is wrong. For "EU unless EU is down", use
litellm-router-hard-eu.py instead.

Serving Regions measured in one account (placement can differ in yours):
    eu. -> eu-north-1      us. -> us-east-1      jp. -> ap-northeast-1
    au. -> ap-southeast-4  global. -> worldwide (ap-southeast-4 when measured)

No data-residency constraint is assumed: normal traffic leaves the EU.

Credentials: set LITELLM_ROLE_ARN to a role to assume, or leave it unset to use
your calling credentials. Tested on LiteLLM 1.109.1.

Run:
    pip install "litellm==1.109.1"
    python litellm-router-soft-eu.py
"""

import asyncio
import os

from litellm import Router
from litellm.router import RetryPolicy

ROLE_ARN = os.environ.get("LITELLM_ROLE_ARN")  # optional


def deployment(alias: str, profile: str, entry_region: str, geo: str, tier: str, weight: int | None = None) -> dict:
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
    if weight is not None:
        params["weight"] = weight
    if ROLE_ARN:
        params["aws_role_name"] = ROLE_ARN
    return {"model_name": alias, "litellm_params": params}


model_list = [
    # EU: total weight 10 across two entry Regions. Both serve from eu-north-1,
    # so the second entry adds entry-path redundancy only.
    deployment("claude-opus-5-5", "eu.anthropic.claude-opus-5-5", "eu-central-1", "eu", "primary", weight=5),
    deployment("claude-opus-5-5", "eu.anthropic.claude-opus-5-5", "eu-west-1", "eu", "primary-entry2", weight=5),
    deployment("claude-opus-5-5", "us.anthropic.claude-opus-5-5", "us-east-1", "us", "secondary", weight=3),
    deployment("claude-opus-5-5", "jp.anthropic.claude-opus-5-5", "ap-northeast-1", "jp", "secondary", weight=2),
    # AU lowest: global also landed on ap-southeast-4 when measured.
    deployment("claude-opus-5-5", "au.anthropic.claude-opus-5-5", "ap-southeast-2", "au", "secondary-last", weight=1),
    # Last resort, used only through fallbacks: global profile, two entry Regions.
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
    routing_strategy="simple-shuffle",  # weighted random pick using `weight`
    num_retries=5,
    retry_after=1,
    allowed_fails=2,
    cooldown_time=30,
    retry_policy=retry_policy,
    enable_pre_call_checks=True,
    fallbacks=[{"claude-opus-5-5": ["claude-opus-5-5-global"]}],
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
