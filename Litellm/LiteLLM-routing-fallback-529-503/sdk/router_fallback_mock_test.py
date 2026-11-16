"""Mocked Bedrock responses (no AWS calls): checks how proxy/litellm-config.yaml retries and falls back on 529 and 503."""
"""
How the installed LiteLLM maps a Bedrock 529 and 503, and what the router_settings
in proxy/litellm-config.yaml actually do with them. No AWS calls: every Bedrock HTTP
request is answered by an httpx MockTransport, and each hit is recorded.

Exits non-zero if the behaviour differs from what the README documents, so CI
catches a LiteLLM upgrade (for example a Dependabot PR) that changes it.
"""
import logging

import copy, os, re, time, yaml
import litellm
from litellm import Router

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "proxy", "litellm-config.yaml")
litellm.suppress_debug_info = True

def load_config():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    role = os.environ.get("LITELLM_ROLE_ARN")  # optional: your own role ARN
    for d in cfg["model_list"]:
        if role:
            d["litellm_params"]["aws_role_name"] = role
        else:
            d["litellm_params"].pop("aws_role_name", None)  # use the calling credentials
    return cfg
import re
import sys
import time
from collections import Counter

import httpx
import yaml
import litellm
from litellm import Router
from litellm.llms.custom_httpx.http_handler import HTTPHandler


litellm.suppress_debug_info = True
from importlib.metadata import version as _pkg_version
print("litellm", _pkg_version("litellm"), flush=True)

BODIES = {
    529: {"message": "Overloaded"},  # illustrative body; Bedrock branch maps on status, not text
    503: {"message": "Bedrock is unable to process your request."},  # illustrative
}


def mock_client(status, hits):
    def handler(req: httpx.Request):
        # URL: https://bedrock-runtime.<region>.amazonaws.com/model/<id>/converse
        m = re.match(r"https://bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com/model/([^/]+)/", str(req.url))
        hits.append((time.monotonic(), m.group(1) if m else "?", httpx.URL(str(req.url)).path.split("/")[2]))
        return httpx.Response(status, json=BODIES[status], headers={"x-amzn-ErrorType": "x"})
    return HTTPHandler(client=httpx.Client(transport=httpx.MockTransport(handler)))


# What the README documents. A LiteLLM upgrade that changes any of these fails CI.
EXPECTED_EXCEPTION = {529: ("APIError", 529), 503: ("ServiceUnavailableError", 503)}
EXPECTED_ATTEMPTS_PER_ALIAS = 6  # 1 call + 5 retries on each alias
EXPECTED_ORDER = [                # primary -> same-model routing -> model fallback
    "eu.anthropic.claude-opus-5-5",
    "global.anthropic.claude-opus-5-5",
    "eu.anthropic.claude-opus-5",
]
failures = []


def check(ok, msg):
    if not ok:
        failures.append(msg)
        print(f"   CHECK FAILED: {msg}")


# ---- 1. Direct completion(): which exception class? -------------------------
print("\n=== 1. litellm.completion() against a mocked Bedrock Converse response ===")
for status in (529, 503):
    hits = []
    try:
        litellm.completion(model="bedrock/eu.anthropic.claude-opus-5-5",
                           messages=[{"role": "user", "content": "hi"}],
                           aws_region_name="eu-west-1", max_tokens=5,
                           client=mock_client(status, hits), num_retries=0)
    except Exception as e:
        print(f"HTTP {status} -> {type(e).__module__}.{type(e).__name__}  "
              f"status_code={getattr(e, 'status_code', None)}  MRO={[c.__name__ for c in type(e).__mro__[:4]]}  hits={len(hits)}")
        name, code = EXPECTED_EXCEPTION[status]
        check(type(e).__name__ == name and getattr(e, "status_code", None) == code,
              f"HTTP {status}: expected {name} (status {code}), got {type(e).__name__} "
              f"(status {getattr(e, 'status_code', None)})")
    else:
        check(False, f"HTTP {status}: completion() did not raise")

# ---- 2. Sample config through the Router, every deployment answering 529 / 503
cfg = load_config()

rs = dict(cfg["router_settings"])
print("\nrouter_settings from config:", rs)

for status in (529, 503):
    print(f"\n=== 2. Router(sample config), every Bedrock call returns HTTP {status} ===")
    hits = []
    client = mock_client(status, hits)
    router = Router(model_list=cfg["model_list"], **rs)
    t0 = time.monotonic()
    try:
        router.completion(model="claude-opus", messages=[{"role": "user", "content": "hi"}],
                          max_tokens=5, client=client)
    except Exception as e:
        print(f"final exception: {type(e).__name__}: {str(e)[:160]}")
    print(f"total Bedrock HTTP attempts: {len(hits)} in {time.monotonic()-t0:.1f}s")
    for (t, region, model), n in Counter((None, r, m) for _, r, m in hits).items():
        print(f"   {n:2} x  {model:40} entry {region}")
    print("   order:", " -> ".join(f"{m.split('.')[0]}.{m.split('.')[-1]}@{r}" for _, r, m in hits))
    per_model = Counter(m for _, _, m in hits)
    order = [m for i, (_, _, m) in enumerate(hits) if i == 0 or hits[i - 1][2] != m]
    check(order == EXPECTED_ORDER, f"HTTP {status}: alias order {order}, expected {EXPECTED_ORDER}")
    for m in EXPECTED_ORDER:
        check(per_model[m] == EXPECTED_ATTEMPTS_PER_ALIAS,
              f"HTTP {status}: {per_model[m]} attempts on {m}, expected {EXPECTED_ATTEMPTS_PER_ALIAS}")

if failures:
    print(f"\nFAILED: {len(failures)} check(s) differ from the documented behaviour.")
    print("If this follows a LiteLLM upgrade, update the README and configs before merging.")
    sys.exit(1)
print("\nAll checks passed: behaviour matches the README.")
