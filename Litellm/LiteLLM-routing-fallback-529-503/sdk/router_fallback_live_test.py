"""Live Router test: sends one request per alias, forces failures to show fallback order, and reads the invocation logs."""
"""
Q4: load the blog's model_list + router_settings into the LiteLLM Router and
send real requests. aws_role_name is removed (the blog uses a placeholder
account 111111111111); calls use the local IAM credentials instead.
Then resolve every call's route tag + inferenceRegion from invocation logs.
"""
import copy
import json
import os
import re
import sys
import time

os.environ["LITELLM_LOG"] = "INFO"
import boto3

import copy, os, re, time, yaml
import litellm
from litellm import Router

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "proxy", "litellm-config-blog.yaml")
litellm.suppress_debug_info = True

def load_blog_config():
    cfg = yaml.safe_load(open(CONFIG))
    role = os.environ.get("LITELLM_ROLE_ARN")  # optional: your own role ARN
    for d in cfg["model_list"]:
        if role:
            d["litellm_params"]["aws_role_name"] = role
        else:
            d["litellm_params"].pop("aws_role_name", None)  # use the calling credentials
    return cfg

MSG = [{"role": "user", "content": "Reply with one word: ok"}]
litellm.suppress_debug_info = True

cfg = load_blog_config()
RS = dict(cfg["router_settings"])
T0 = int(time.time() * 1000)


def run(label, model_list, alias, **kw):
    print(f"\n######## {label}: request to alias '{alias}' ########", flush=True)
    r = Router(model_list=model_list, **RS)
    t = time.time()
    try:
        resp = r.completion(model=alias, messages=MSG, max_tokens=5, **kw)
        hp = resp._hidden_params
        print(f"RESULT {label}: OK in {time.time()-t:.1f}s  response.model={resp.model}  "
              f"served_by_model_group={hp.get('litellm_model_name') or ''} "
              f"api_base={hp.get('api_base')}  "
              f"attempted_fallbacks={resp._hidden_params.get('additional_headers', {}).get('x-litellm-attempted-fallbacks')}",
              flush=True)
    except Exception as e:
        print(f"RESULT {label}: FAILED in {time.time()-t:.1f}s  {type(e).__name__}: {str(e)[:300]}", flush=True)


# A. one request per alias, config as published
for alias in ("claude-opus", "claude-opus-global", "claude-opus-5"):
    run(f"A-{alias}", cfg["model_list"], alias)

# B. primary broken -> expect claude-opus-global
b = copy.deepcopy(cfg["model_list"])
for d in b:
    if d["model_name"] == "claude-opus":
        d["litellm_params"]["model"] = "bedrock/eu.anthropic.claude-opus-5-5-BROKEN"
run("B-primary-broken", b, "claude-opus")

# C. primary + global broken -> expect claude-opus-5
c = copy.deepcopy(b)
for d in c:
    if d["model_name"] == "claude-opus-global":
        d["litellm_params"]["model"] = "bedrock/global.anthropic.claude-opus-5-5-BROKEN"
run("C-primary-and-global-broken", c, "claude-opus")

# D. per-deployment timeout honoured: timeout 5s on a long generation, no fallbacks
d_list = [copy.deepcopy(next(x for x in cfg["model_list"] if x["model_name"] == "claude-opus"))]
d_list[0]["litellm_params"]["timeout"] = 5
r = Router(model_list=d_list, num_retries=0)
print("\n######## D-timeout-5s: long generation on a deployment with timeout: 5 ########", flush=True)
t = time.time()
try:
    r.completion(model="claude-opus", max_tokens=3000,
                 messages=[{"role": "user", "content": "Write a 2,500-word essay on the history of bridges."}])
    print(f"RESULT D: completed in {time.time()-t:.1f}s (timeout NOT applied)")
except Exception as e:
    print(f"RESULT D: {type(e).__name__} after {time.time()-t:.1f}s: {str(e)[:200]}")

# ---- invocation logs: which deployment really answered ----------------------
print("\nwaiting 60s for invocation logs...", flush=True)
time.sleep(60)
print("\n######## Invocation-log records with requestMetadata.app=llm-gateway ########")
for region in ("eu-west-1", "eu-north-1"):
    logs = boto3.client("logs", region_name=region)
    for _ in range(6):
        evs = logs.filter_log_events(logGroupName="/aws/bedrock/model-invocations", startTime=T0,
                                     filterPattern='"llm-gateway"').get("events", [])
        if evs:
            break
        time.sleep(20)
    for e in evs:
        rec = json.loads(e["message"])
        print(f"  logGroupRegion={region:10} time={rec.get('timestamp')} modelId={rec.get('modelId').split('/')[-1]:34} "
              f"route={rec.get('requestMetadata', {}).get('route'):15} region={rec.get('region')} "
              f"inferenceRegion={rec.get('inferenceRegion')} outTokens={rec.get('output', {}).get('outputTokenCount')}")
