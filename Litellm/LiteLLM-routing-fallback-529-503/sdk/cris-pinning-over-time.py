"""
Does CRIS placement stay on one serving Region over time?

cris-routing-by-model.py shows placement in a single burst. This script answers
the follow-up question: does the serving Region for a given
(account, entry Region, model, profile) stay fixed for hours, or does it move?

Method
------
Every INTERVAL minutes, for DURATION hours, send CALLS_PER_ROUND small requests
per (profile, entry Region) target. Each call carries a unique requestMetadata
trace tag. After each round, resolve the ACTUAL serving Region from Bedrock model
invocation logs (inferenceRegion), written in the entry Region's log group.

Every call is appended to a CSV as soon as it resolves, so a crash or Ctrl+C
loses nothing. Re-running with --report reads the CSV and prints the summary
and chart without sending new calls.

Output
------
  cris-pinning-<timestamp>.csv   one row per call
  cris-pinning-<timestamp>.png   serving Region vs time, one panel per target
  printed summary                per target: share by Region, number of Region
                                 changes between rounds, longest pinned stretch

Run
---
  python cris-pinning-over-time.py                     # 24h, every 15 min
  python cris-pinning-over-time.py --hours 48 --interval 10
  python cris-pinning-over-time.py --hours 0.5 --interval 2   # quick smoke test
  python cris-pinning-over-time.py --report cris-pinning-20261108-2100.csv

Prereqs: model invocation logging enabled in every entry Region below.
Cost: tiny prompts, max_tokens=5. 24h at 15 min x 5 calls x 3 targets = 1,440 calls.
"""

import argparse
import csv
import json
import os
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
from litellm import completion
from aws_bedrock_token_generator import provide_token

LOG_GROUP = "/aws/bedrock/model-invocations"
TEST_NAME = "cris-pinning"

# (label, inference profile, entry Region). Add or remove targets freely.
# Two entry Regions on the same profile test "call Region does not change
# serving Region"; global vs eu tests "Global and Geo can resolve to the same
# serving Region".
TARGETS = [
    ("opus-eu@eu-west-1",      "eu.anthropic.claude-opus-5-5",     "eu-west-1"),
    ("opus-eu@eu-north-1",     "eu.anthropic.claude-opus-5-5",     "eu-north-1"),
    ("opus-global@eu-north-1", "global.anthropic.claude-opus-5-5", "eu-north-1"),
]

FIELDS = ["ts_utc", "round", "target", "profile", "entry_region", "trace_tag",
          "status", "latency_ms", "inference_region", "error"]

_tokens = {}


def token_for(region):
    # Bedrock API keys from provide_token are short-lived; refresh every 10 min.
    tok, at = _tokens.get(region, (None, 0))
    if not tok or time.time() - at > 600:
        tok = provide_token(region=region)
        _tokens[region] = (tok, time.time())
    return tok


def one_call(rnd, label, profile, entry):
    tag = f"pin-{uuid.uuid4().hex[:12]}"
    row = dict(ts_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               round=rnd, target=label, profile=profile, entry_region=entry,
               trace_tag=tag, status="ok", latency_ms="", inference_region="",
               error="")
    t0 = time.time()
    try:
        completion(model=f"bedrock/{profile}",
                   messages=[{"role": "user", "content": "Reply with one word: ok"}],
                   aws_region_name=entry, api_key=token_for(entry), max_tokens=5,
                   requestMetadata={"trace_tag": tag, "test": TEST_NAME,
                                    "target": label})
    except Exception as e:  # keep going; failures are data too (503/529/429)
        row["status"] = "error"
        row["error"] = str(e)[:200].replace("\n", " ")
    row["latency_ms"] = int((time.time() - t0) * 1000)
    return row


def resolve(rows, since_ms, attempts=8, delay=20):
    """Fill inference_region for successful rows from the entry Region logs."""
    want = defaultdict(set)
    for r in rows:
        if r["status"] == "ok":
            want[r["entry_region"]].add(r["trace_tag"])
    found = {}
    for _ in range(attempts):
        for region, tags in want.items():
            if all(t in found for t in tags):
                continue
            logs = boto3.client("logs", region_name=region)
            kw = dict(logGroupName=LOG_GROUP, startTime=since_ms,
                      filterPattern=f'{{ $.requestMetadata.test = "{TEST_NAME}" }}')
            while True:
                try:
                    resp = logs.filter_log_events(**kw)
                except Exception as e:
                    print(f"    log query failed in {region}: {e}")
                    break
                for ev in resp.get("events", []):
                    try:
                        rec = json.loads(ev["message"])
                    except ValueError:
                        continue
                    t = rec.get("requestMetadata", {}).get("trace_tag")
                    if t in tags:
                        found[t] = rec.get("inferenceRegion") or "(missing)"
                if not resp.get("nextToken"):
                    break
                kw["nextToken"] = resp["nextToken"]
        if all(t in found for ts in want.values() for t in ts):
            break
        time.sleep(delay)
    for r in rows:
        if r["status"] == "ok":
            r["inference_region"] = found.get(r["trace_tag"], "(unresolved)")


def run(hours, interval_min, calls, out_csv):
    rounds = max(1, int(hours * 60 / interval_min))
    print(f"{len(TARGETS)} targets x {calls} calls x {rounds} rounds "
          f"(every {interval_min} min for {hours} h) -> {out_csv}")
    new = not os.path.exists(out_csv)
    with open(out_csv, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for rnd in range(1, rounds + 1):
            start = time.time()
            since_ms = int((start - 120) * 1000)
            jobs = [(rnd, *t) for t in TARGETS for _ in range(calls)]
            with ThreadPoolExecutor(max_workers=8) as ex:
                rows = list(ex.map(lambda j: one_call(*j), jobs))
            time.sleep(45)  # invocation logs land with some delay  # nosemgrep: arbitrary-sleep
            resolve(rows, since_ms)
            w.writerows(rows)
            fh.flush()

            line = []
            for label, _, _ in TARGETS:
                c = Counter(r["inference_region"] or "ERR"
                            for r in rows if r["target"] == label)
                line.append(f"{label}: " + ",".join(f"{k}x{v}" for k, v in c.items()))
            print(f"[round {rnd}/{rounds} {datetime.now():%H:%M}] " + " | ".join(line))

            wait = interval_min * 60 - (time.time() - start)
            if rnd < rounds and wait > 0:
                time.sleep(wait)


def report(csv_path):
    with open(csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    by_target = defaultdict(list)
    for r in rows:
        by_target[r["target"]].append(r)

    print("\n" + "=" * 78)
    print(f"CRIS PLACEMENT OVER TIME  ({csv_path})")
    print("=" * 78)
    for label, rs in by_target.items():
        ok = [r for r in rs if r["status"] == "ok"
              and not r["inference_region"].startswith("(")]
        errs = len([r for r in rs if r["status"] == "error"])
        if not ok:
            print(f"\n{label}: no resolved calls ({errs} errors)")
            continue
        t0, t1 = ok[0]["ts_utc"], ok[-1]["ts_utc"]
        hrs = (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds() / 3600
        share = Counter(r["inference_region"] for r in ok)

        # Majority serving Region per round, then count changes between rounds.
        per_round = {}
        for rnd in sorted({int(r["round"]) for r in ok}):
            c = Counter(r["inference_region"] for r in ok if int(r["round"]) == rnd)
            per_round[rnd] = c.most_common(1)[0][0]
        seq = list(per_round.values())
        changes = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        mixed = sum(1 for rnd in per_round
                    if len({r["inference_region"] for r in ok if int(r["round"]) == rnd}) > 1)

        print(f"\n{label}  ({rs[0]['profile']} from {rs[0]['entry_region']})")
        print(f"  calls resolved: {len(ok)}   errors: {errs}   window: {hrs:.1f} h, "
              f"{len(per_round)} rounds")
        for reg, n in share.most_common():
            print(f"    {reg:16} {n:5}  ({100 * n / len(ok):.1f}%)")
        print(f"  Region changes between rounds: {changes}   "
              f"rounds with more than one Region: {mixed}")
        if errs:
            kinds = Counter(next((k for k in ("503", "529", "429", "Timeout")
                                  if k in r["error"]), "other")
                            for r in rs if r["status"] == "error")
            print(f"  error kinds: {dict(kinds)}")

    plot(by_target, csv_path.replace(".csv", ".png"))


def plot(by_target, out_png):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not installed; skipping chart)")
        return
    n = len(by_target)
    fig, axes = plt.subplots(n, 1, figsize=(11, 2.2 * n), sharex=True, squeeze=False)
    for ax, (label, rs) in zip(axes[:, 0], by_target.items()):
        ok = [r for r in rs if r["status"] == "ok"
              and not r["inference_region"].startswith("(")]
        regions = sorted({r["inference_region"] for r in ok})
        ypos = {reg: i for i, reg in enumerate(regions)}
        ax.scatter([datetime.fromisoformat(r["ts_utc"]) for r in ok],
                   [ypos[r["inference_region"]] for r in ok], s=8)
        errs = [r for r in rs if r["status"] == "error"]
        if errs:
            ax.scatter([datetime.fromisoformat(r["ts_utc"]) for r in errs],
                       [-1] * len(errs), s=8, marker="x", color="red")
        ax.set_yticks(list(ypos.values()) + ([-1] if errs else []))
        ax.set_yticklabels(regions + (["error"] if errs else []))
        ax.set_title(label, fontsize=9, loc="left")
        ax.grid(axis="x", alpha=0.3)
    axes[-1, 0].set_xlabel("time (UTC)")
    fig.suptitle("Serving Region (inferenceRegion) over time", fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    print(f"\nchart: {out_png}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--interval", type=float, default=15, help="minutes between rounds")
    p.add_argument("--calls", type=int, default=5, help="calls per target per round")
    p.add_argument("--out", default=None, help="CSV path (appends if it exists)")
    p.add_argument("--report", default=None, help="only summarise an existing CSV")
    a = p.parse_args()

    if a.report:
        report(a.report)
    else:
        out = a.out or f"cris-pinning-{datetime.now():%Y%m%d-%H%M}.csv"
        try:
            run(a.hours, a.interval, a.calls, out)
        except KeyboardInterrupt:
            print("\nstopped; summarising what was collected")
        report(out)
