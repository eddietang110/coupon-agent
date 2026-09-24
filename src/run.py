"""Run the coupon decision pipeline over the sampled reviews.

Three layers, cheapest first:
  1. deterministic input screen  - flagged rows never reach the model (0 tokens)
  2. batched schema-constrained LLM call
  3. deterministic output validation, with one narrow repair retry
"""
import argparse
import csv
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent
import guardrails
import policy

SAMPLE = "data/sample.csv"
OUT_CSV = "out/decisions.csv"
OUT_LOG = "out/audit.jsonl"
OUT_USAGE = "out/usage.json"
MIN_REVIEW_CHARS = 20


def load_key():
    k = os.environ.get(agent.KEY_ENV)
    if not k and os.path.exists(".env"):
        for line in open(".env"):
            if line.startswith(agent.KEY_ENV + "="):
                k = line.split("=", 1)[1].strip()
    if not k:
        sys.exit(f"{agent.KEY_ENV} not set (put it in .env)")
    return k


def blocked(rc, note):
    return ({"send": False, "amt": 0, "rc": rc, "why": "", "msg": "", "conf": 1.0}, [note])


def process_rows(client, rows, batch=8, workers=6, effort="low"):
    """Push rows through all three layers. Returns ({id: result}, n_sent_to_model).

    Shared by main() and redteam/provider.py so the red team attacks the real pipeline,
    not a copy of it.
    """
    results, lock = {}, threading.Lock()
    to_model = []

    # --- layer 1: input guardrail, before a single token is spent -------------
    for r in rows:
        hit, pats = guardrails.screen_input(r["review"])
        r["_prefilter"] = pats
        if hit:
            d, v = blocked("POLICY_BLOCK", "input guardrail: " + ", ".join(pats))
            results[r["id"]] = {"row": r, "raw": None, "decision": d, "violations": v,
                                "caught_by": "prefilter", "repaired": False}
        elif len(guardrails.normalize(r["review"])) < MIN_REVIEW_CHARS:
            d, v = blocked("UNVERIFIABLE", "review too short to act on")
            results[r["id"]] = {"row": r, "raw": None, "decision": d, "violations": v,
                                "caught_by": "", "repaired": False}
        else:
            to_model.append(r)

    batches = [to_model[i:i + batch] for i in range(0, len(to_model), batch)]

    def handle(batch):
        recs = [guardrails.wrap_untrusted(r["id"], r["review"]) for r in batch]
        decided = {}
        for attempt in range(2):  # one retry: an empty/partial batch is usually
            try:                  # a transient provider hiccup, not a schema bug
                decided, _ = client.decide(recs, effort=effort)
            except Exception as e:
                print(f"  batch failed (attempt {attempt + 1}): {e}", file=sys.stderr)
                decided = {}
            if len(decided) >= len(batch):
                break
        if len(decided) < len(batch):
            print(f"  batch incomplete after retry: {len(decided)}/{len(batch)} ids returned",
                  file=sys.stderr)
        for r, rec in zip(batch, recs):
            raw = decided.get(r["id"])
            rating = float(r["rating"]) if r["rating"] else None
            followers = int(r["followers"] or 0)
            model_inj = bool(raw and raw.get("inj"))
            d, viol, fatal = guardrails.validate_output(
                raw, r["review"], rating, followers, injected=model_inj)
            repaired = False
            if fatal and d["send"]:
                try:
                    new_msg = client.repair(rec, d)
                except Exception:
                    new_msg = ""
                d2, viol2, fatal2 = guardrails.validate_output(
                    raw | {"msg": new_msg, "amt": d["amt"], "rc": d["rc"], "send": True},
                    r["review"], rating, followers, injected=model_inj)
                if not fatal2:
                    d, viol, repaired = d2, viol + ["repaired message on retry"], True
                else:
                    d = guardrails.safe_default(d)
                    viol = viol + viol2 + ["repair failed; fell back to no-send"]
            elif fatal:
                d = guardrails.safe_default(d)
            with lock:
                results[r["id"]] = {"row": r, "raw": raw, "decision": d, "violations": viol,
                                    "caught_by": "model" if model_inj else "", "repaired": repaired}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(handle, batches))
    return results, len(to_model)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="first N rows only (smoke test)")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    a = ap.parse_args()

    rows = list(csv.DictReader(open(SAMPLE)))
    if a.limit:
        rows = rows[:a.limit]
    client = agent.OpenRouter(load_key())

    results, n_to_model = process_rows(client, rows, a.batch, a.workers, a.effort)
    print(f"{len(rows)} rows | {len(rows) - n_to_model} short-circuited | "
          f"{n_to_model} sent to model")

    os.makedirs("out", exist_ok=True)
    fields = ["id", "restaurant", "reviewer", "rating", "followers", "send", "amount_usd",
              "reason_code", "reason_to_customer", "customer_message", "confidence",
              "injection_flag", "caught_by", "guardrail_notes"]
    with open(OUT_CSV, "w", newline="") as f, open(OUT_LOG, "w") as lg:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            res = results.get(r["id"])
            if not res:
                continue
            d = res["decision"]
            w.writerow({"id": r["id"], "restaurant": r["restaurant"], "reviewer": r["reviewer"],
                        "rating": r["rating"], "followers": r["followers"],
                        "send": d["send"], "amount_usd": d["amt"], "reason_code": d["rc"],
                        "reason_to_customer": d["why"], "customer_message": d["msg"],
                        "confidence": round(d["conf"], 2),
                        "injection_flag": d["rc"] == "POLICY_BLOCK",
                        "caught_by": res["caught_by"],
                        "guardrail_notes": " | ".join(res["violations"])})
            lg.write(json.dumps({
                "id": r["id"], "is_injection_truth": int(r["is_injection"]),
                "injection_family": r["injection_family"], "rating": r["rating"],
                "prefilter_hits": r["_prefilter"], "model_raw": res["raw"],
                "final": d, "violations": res["violations"],
                "caught_by": res["caught_by"], "repaired": res["repaired"],
                "review": r["review"]}, ensure_ascii=False) + "\n")

    u = client.usage
    u["rows"] = len(rows)
    u["rows_short_circuited"] = len(rows) - n_to_model
    u["model"] = client.model
    u["batch_size"] = a.batch
    u["tokens_per_row"] = round((u["prompt_tokens"] + u["completion_tokens"]) / max(1, len(rows)), 1)
    u["cost_per_1k_rows_usd"] = round(u["cost"] / max(1, len(rows)) * 1000, 3)
    json.dump(u, open(OUT_USAGE, "w"), indent=2)
    print(json.dumps(u, indent=2))


if __name__ == "__main__":
    main()
