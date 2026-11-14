"""
Time-to-last-token for Claude Opus 5.5 on long generations (2,000+ output tokens).

Sends N non-streaming calls through LiteLLM to the EU geo profile, each asking
for a long answer, and records wall-clock time from request to the full
response (= time to last token). Use p99 to size the gateway `timeout`.

Only calls that produced >= MIN_OUT output tokens count towards the percentiles.

Run:
    python opus-long-generation-latency.py [--n 30] [--workers 5]
"""

import argparse
import csv
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from litellm import completion
from aws_bedrock_token_generator import provide_token

MODEL = "bedrock/eu.anthropic.claude-opus-5-5"
ENTRY = "eu-west-1"
MAX_TOKENS = 4096
MIN_OUT = 2000
CLIENT_TIMEOUT = 900  # generous so the measurement is not cut short

TOPICS = [
    "the history of the printing press", "how TCP congestion control works",
    "the water cycle and climate", "designing a relational database schema",
    "the causes of the French Revolution", "how vaccines train the immune system",
    "the economics of renewable energy", "how compilers optimise code",
    "the life cycle of stars", "the evolution of written language",
]

PROMPT = ("Write a detailed, well-structured explainer of at least 2,500 words "
          "about {topic}. Use section headings and full paragraphs. Do not stop "
          "early and do not summarise at the end.")


def pct(sorted_vals, p):
    """Nearest-rank percentile."""
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(round(p / 100 * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


def one(i, token):
    tag = f"latency-{uuid.uuid4().hex[:12]}"
    t0 = time.perf_counter()
    r = completion(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT.format(topic=TOPICS[i % len(TOPICS)])}],
        aws_region_name=ENTRY, api_key=token, max_tokens=MAX_TOKENS,
        timeout=CLIENT_TIMEOUT, num_retries=0,
        requestMetadata={"trace_tag": tag, "test": "long-gen-latency"},
    )
    dt = time.perf_counter() - t0
    return {"i": i, "tag": tag, "seconds": round(dt, 2),
            "out_tokens": r.usage.completion_tokens, "in_tokens": r.usage.prompt_tokens,
            "finish": r.choices[0].finish_reason}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args()

    token = provide_token(region=ENTRY)
    rows, errors = [], []
    print(f"{MODEL} from {ENTRY}: {a.n} calls, {a.workers} concurrent, max_tokens={MAX_TOKENS}")
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(one, i, token): i for i in range(a.n)}
        for f in as_completed(futs):
            try:
                r = f.result(); rows.append(r)
                print(f"  call {r['i']:2}: {r['seconds']:7.2f}s  out={r['out_tokens']:5}  finish={r['finish']}")
            except Exception as e:
                errors.append(str(e)[:200]); print(f"  call {futs[f]:2}: ERROR {str(e)[:150]}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out = f"opus-long-generation-latency-{stamp}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["i", "tag", "seconds", "out_tokens", "in_tokens", "finish"])
        w.writeheader(); w.writerows(sorted(rows, key=lambda r: r["i"]))

    long_rows = [r for r in rows if r["out_tokens"] >= MIN_OUT]
    secs = sorted(r["seconds"] for r in long_rows)
    toks = [r["out_tokens"] for r in long_rows]
    print("\n" + "=" * 64)
    print(f"TIME TO LAST TOKEN  ({MODEL}, entry {ENTRY})")
    print("=" * 64)
    print(f"calls ok={len(rows)} errors={len(errors)}  counted (out>={MIN_OUT})={len(long_rows)}")
    if secs:
        print(f"output tokens: min={min(toks)} median={int(statistics.median(toks))} max={max(toks)}")
        print(f"p50={pct(secs,50):.1f}s  p95={pct(secs,95):.1f}s  p99={pct(secs,99):.1f}s  "
              f"min={secs[0]:.1f}s  max={secs[-1]:.1f}s")
        print(f"throughput median={statistics.median(t/s for t, s in zip(toks, [r['seconds'] for r in long_rows])):.1f} tok/s")
    print(f"raw rows: {out}")


if __name__ == "__main__":
    main()
