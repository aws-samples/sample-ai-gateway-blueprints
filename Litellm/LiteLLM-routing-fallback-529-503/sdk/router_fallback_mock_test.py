"""Mocked Bedrock responses (no AWS calls): checks how proxy/litellm-config.yaml retries and falls back on 529 and 503."""
"""
How LiteLLM 1.109.1 maps a Bedrock 529 and 503, and what the router_settings
in proxy/litellm-config.yaml actually do with them. No AWS calls: every Bedrock HTTP
request is answered by an httpx MockTransport, and each hit is recorded.
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
