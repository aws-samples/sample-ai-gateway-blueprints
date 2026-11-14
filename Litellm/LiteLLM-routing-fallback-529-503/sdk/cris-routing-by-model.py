"""
Does CRIS routing behaviour vary by model?

The AWS post "Implementing resilience patterns with Amazon Bedrock and LLM
gateway" shows 10 requests on a US geographic profile split 10%/70%/20% across
three Regions, and says "routing behavior varies by model; some models route
dynamically across Regions based on real-time capacity, while others run in a
fixed inference Region."

Our earlier measurement showed Claude Opus 5.5 pinned ~100% to one Region. This
script tests several US-geo models side by side, same account, same entry Region,
same moment, and tallies the ACTUAL serving Region from Bedrock model invocation
logs (inferenceRegion). It is the evidence for the blog's serving-Region claim.

Run:
    python cris-routing-by-model.py
"""

import json
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from litellm import completion
from aws_bedrock_token_generator import provide_token

ENTRY = "us-east-1"
N = 30
LOG_GROUP = "/aws/bedrock/model-invocations"

MODELS = [
    "us.anthropic.claude-opus-5-5",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
    "us.meta.llama3-3-70b-instruct-v1:0",
]

token = provide_token(region=ENTRY)
pending = []  # (tag, model)


def one(model):
    tag = f"bymodel-{uuid.uuid4().hex[:12]}"
    completion(model=f"bedrock/{model}",
               messages=[{"content": "Reply with one word: ok", "role": "user"}],
               aws_region_name=ENTRY, api_key=token, max_tokens=5,
               requestMetadata={"trace_tag": tag, "test": "cris-by-model"})
    return tag


for m in MODELS:
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(one, m) for _ in range(N)]
        for f in as_completed(futs):
            try:
                pending.append((f.result(), m)); ok += 1
            except Exception as e:
                fail += 1
                if fail == 1:
                    print(f"  {m}: first failure -> {str(e)[:110]}")
    print(f"{m}: {ok} ok, {fail} failed")

print(f"\n{len(pending)} calls made; waiting for invocation logs...")
time.sleep(60)  # nosemgrep: arbitrary-sleep

tagmap = {}
logs = boto3.client("logs", region_name=ENTRY)
start = int((time.time() - 1800) * 1000)
for _ in range(10):
    nxt = None
    while True:
        kw = dict(logGroupName=LOG_GROUP, startTime=start, limit=1000)
        if nxt:
            kw["nextToken"] = nxt
        r = logs.filter_log_events(**kw)
        for e in r.get("events", []):
            try:
                rec = json.loads(e["message"])
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"  skipped unparseable invocation-log event: {exc}")
                continue
            t = rec.get("requestMetadata", {}).get("trace_tag", "")
            if t.startswith("bymodel-"):
                tagmap[t] = rec.get("inferenceRegion") or rec.get("region")
        nxt = r.get("nextToken")
        if not nxt:
            break
    if all(t in tagmap for t, _ in pending):
        break
    print(f"  resolved {sum(t in tagmap for t,_ in pending)}/{len(pending)}; waiting 20s")
    time.sleep(20)  # nosemgrep: arbitrary-sleep

dist = defaultdict(Counter)
for tag, m in pending:
    dist[m][tagmap.get(tag, "(not found)")] += 1

print("\n" + "=" * 72)
print(f"SERVING REGION DISTRIBUTION  (entry {ENTRY}, {N} calls per model)")
print("=" * 72)
for m in MODELS:
    c = dist[m]; total = sum(c.values())
    if not total:
        print(f"\n{m}: no successful calls"); continue
    regions = sorted(c.items(), key=lambda x: -x[1])
    verdict = "PINNED (1 Region)" if len(regions) == 1 else f"SPREAD ({len(regions)} Regions)"
    print(f"\n{m}  ->  {verdict}")
    for reg, n in regions:
        print(f"    {reg:18} {n:3}  ({100*n/total:.0f}%)")
