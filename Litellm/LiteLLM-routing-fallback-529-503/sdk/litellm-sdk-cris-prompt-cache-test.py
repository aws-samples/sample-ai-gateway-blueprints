"""
Prompt caching under Bedrock cross-region inference (CRIS) -- empirical probe.

Investigates the cache-locality cost of multi-geo routing, to answer:
  Q1. Are prompt caches strictly Region-local (no cross-Region replication)?
  Q2. When CRIS places a request in a Region with no warm cache, is it a cold
      start + full re-write billed at the cache-WRITE rate?
  Q3. Is CRIS placement cache-aware (affinity to the Region holding a warm
      cache), or purely capacity/availability-driven?
  Q4. Within the TTL window, if traffic returns to the original serving Region,
      is the cache still warm, or can placement churn invalidate it?

METHOD
------
Each call sends an identical large prompt with an explicit cache checkpoint
(cache_control: ephemeral). We read two usage fields LiteLLM surfaces for
Bedrock:
    cache_creation_input_tokens  -> a cache WRITE happened (cold / (re)build)
    cache_read_input_tokens      -> a cache HIT (warm) on the serving Region
We also resolve the ACTUAL serving Region (inferenceRegion) from Bedrock Model
Invocation Logging, matched by a unique requestMetadata trace tag (the serving
Region is NOT in the API response).

Interpretation:
  WRITE on a Region   => that Region had no warm cache for this prefix.
  READ on a Region    => that Region already had the prefix cached (warm).
  Compare the serving Region across calls to see whether CRIS kept affinity to
  the warm Region (reads persist) or churned to a cold one (new writes appear).

PREREQS: model invocation logging enabled in the entry regions used below
(see enable_bedrock_invocation_logging.py) so inferenceRegion can be resolved.

Run:
    pip install litellm aws-bedrock-token-generator boto3
    python litellm-sdk-cris-prompt-cache-test.py
"""

import time
import json
import uuid

import boto3
from litellm import completion
from aws_bedrock_token_generator import provide_token

BASE = "anthropic.claude-opus-5-5"
LOG_GROUP = "/aws/bedrock/model-invocations"

# A large, STABLE prefix (identical every call) so the same cache checkpoint is
# reused. Opus cache minimum is ~1024 tokens; this is comfortably above it.
STABLE_PREFIX = (
    "You are a financial-markets assistant for a systematic trading firm.\n"
    "Reference context (do not summarise, just hold in context):\n"
    + ("Historical note: markets fluctuate; past performance is not indicative "
       "of future results. " * 220)
)


def call(prefix_geo: str, entry_region: str):
    """One cached call. Returns (tag, usage_dict)."""
    tag = f"cache-{uuid.uuid4().hex[:12]}"
    token = provide_token(region=entry_region)
    messages = [
        {"role": "system", "content": [
            {"type": "text", "text": STABLE_PREFIX,
             "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": "Reply with exactly one word: ok"},
    ]
    r = completion(
        model=f"bedrock/{prefix_geo}.{BASE}",
        messages=messages,
        aws_region_name=entry_region,
        api_key=token,
        requestMetadata={"trace_tag": tag, "test": "prompt-cache-cris",
                         "geo": prefix_geo, "entry": entry_region},
    )
    u = r.usage
    return tag, {
        "write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "prompt": u.prompt_tokens,
    }


def serving_region(tag: str, entry_region: str, *, attempts=10, delay=20):
    """Resolve inferenceRegion for a tag from invocation logs (entry region)."""
    logs = boto3.client("logs", region_name=entry_region)
    start = int((time.time() - 600) * 1000)
    for _ in range(attempts):
        try:
            evs = logs.filter_log_events(logGroupName=LOG_GROUP, startTime=start,
                                         filterPattern=f'"{tag}"', limit=10)["events"]
        except Exception:
            evs = []
        for e in evs:
            rec = json.loads(e["message"])
            if rec.get("requestMetadata", {}).get("trace_tag") == tag:
                return rec.get("inferenceRegion") or rec.get("region")
        time.sleep(delay)
    return "(unresolved)"


def show(label, tag, usage, entry, resolve=True):
    served = serving_region(tag, entry) if resolve else "(skipped)"
    kind = "WRITE" if usage["write"] else ("READ" if usage["read"] else "none")
    print(f"  {label:34} entry={entry:14} served={served:16} "
          f"{kind:5} write={usage['write']:5} read={usage['read']:5}")
    return served


def main():
    print("=" * 78)
    print("PROMPT CACHE UNDER CRIS -- empirical probe")
    print("=" * 78)

    # --- EXPERIMENT A: same geo, repeated -> affinity + warm-cache behaviour --
    # (Q3, Q4) Fire several identical calls on the SAME geo. First is a WRITE;
    # if CRIS keeps affinity to the warm region, the rest are READs. A WRITE
    # reappearing mid-sequence = placement churned to a cold region.
    print("\n[A] Same geo (eu), repeated calls -- expect 1 write then reads if\n"
          "    placement is cache-sticky within the TTL window:")
    first_tag, first_u = call("eu", "eu-west-1")
    a_regions = [show("A1 (cold build expected)", first_tag, first_u, "eu-west-1")]
    for i in range(2, 7):
        time.sleep(2)
        t, u = call("eu", "eu-west-1")
        a_regions.append(show(f"A{i}", t, u, "eu-west-1"))

    # --- EXPERIMENT B: cross-geo, same prompt -> region-locality (Q1, Q2) -----
    # Warm eu first (done in A), then hit a DIFFERENT geo that serves from a
    # different region. A full WRITE there => caches are region-local and a cold
    # region pays the write rate. A READ => cross-region cache sharing.
    print("\n[B] Cross-geo, SAME prompt -- does a different serving region reuse\n"
          "    the eu warm cache (READ) or pay a fresh WRITE (region-local)?")
    for geo, entry in [("us", "us-east-1"), ("jp", "ap-northeast-1"),
                       ("au", "ap-southeast-2")]:
        t, u = call(geo, entry)
        show(f"B {geo}", t, u, entry)

    # --- EXPERIMENT C: return to eu within TTL (Q4) ---------------------------
    print("\n[C] Return to eu within TTL -- is the original eu cache still warm?")
    t, u = call("eu", "eu-west-1")
    show("C eu (return)", t, u, "eu-west-1")

    print("\n" + "=" * 78)
    print("READ the WRITE/READ column against the served-region column:")
    print("  * eu calls all READ after the first + all served from one region")
    print("    => region-local cache + cache-sticky placement within TTL.")
    print("  * cross-geo calls WRITE afresh => caches are strictly region-local;")
    print("    a cold serving region pays the full cache-WRITE rate (Q1+Q2).")
    print("  * a WRITE reappearing on eu => placement churned off the warm")
    print("    region (Q3/Q4: placement is capacity-driven, not cache-aware).")
    print("=" * 78)


if __name__ == "__main__":
    main()
