"""Lost Thread raw counts for a run (no judging): per arm x contestant, the runs, errors, time, DayDream tool use, cost/tokens, and
how many checkable key facts the answers hit by code check (a rough signal before blinded grading, not a score).

    python3 -m bench.summarize --run pilot-dev-jonah-20261006
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from .common import PERSONAS, RUNS, read_jsonl, write_json
from .pack import code_checks, normalize


def summarize(run_id: str) -> dict:
    latest = {}
    for t in read_jsonl(RUNS / run_id / "traces.jsonl"):
        latest[(t["item_id"], t["arm"], t["contestant"])] = t   # re-runs (after a timeout) replace the earlier trace
    traces = list(latest.values())
    items = {}
    snap = RUNS / run_id / "inputs" / "questions.jsonl"
    for p in {t["persona"] for t in traces}:
        items.update({i["id"]: i for i in read_jsonl(snap if snap.exists() else PERSONAS / p / "questions.jsonl")})
    cells = defaultdict(lambda: defaultdict(float))
    by_type = defaultdict(lambda: defaultdict(float))
    for t in traces:
        it = items[t["item_id"]]
        k = f"{t['arm']} | {t['contestant']}"
        c = cells[k]
        c["runs"] += 1
        c["errors"] += bool(t.get("errors"))
        c["timeouts"] += "timeout" in (t.get("errors") or [])
        c["isolation_flags"] += bool(t.get("isolation_problems"))
        c["seconds"] += t.get("seconds") or 0
        n = t.get("n_tool_calls") or 0
        c["runs_using_daydream"] += n > 0
        c["tool_calls"] += n
        c["cost_usd"] += t.get("cost_usd") or 0
        u = t.get("usage") or {}
        # Claude reports cached prompt tokens separately; Codex's input_tokens already include its cached tokens.
        c["input_tokens"] += sum((u.get(x) or 0) for x in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        c["output_tokens"] += u.get("output_tokens", 0) or 0
        if not t.get("errors"):
            chk = code_checks(normalize(t.get("answer") or ""), it)
            req = {f["id"] for f in it["facts"] if not f["optional"]}
            for fid, r in chk.items():
                if fid in req:
                    c["checkable_facts"] += 1
                    c["checkable_hits"] += r["score"] == 2
                    b = by_type[f"{k} | {it['type']}"]
                    b["checkable_facts"] += 1
                    b["checkable_hits"] += r["score"] == 2
    out = {}
    for k, c in sorted(cells.items()):
        ok = c["runs"] - c["errors"]
        out[k] = {"runs": int(c["runs"]), "errors": int(c["errors"]), "timeouts": int(c["timeouts"]),
                  "isolation_flags": int(c["isolation_flags"]),
                  "mean_seconds": round(c["seconds"] / max(c["runs"], 1), 1),
                  "runs_using_daydream": int(c["runs_using_daydream"]),
                  "mean_tool_calls": round(c["tool_calls"] / max(c["runs"], 1), 2),
                  "checkable_hits": f"{int(c['checkable_hits'])}/{int(c['checkable_facts'])}",
                  "cost_usd_api_equiv": round(c["cost_usd"], 3) if c["cost_usd"] else None,
                  "tokens_in_out": [int(c["input_tokens"]), int(c["output_tokens"])], "answered": int(ok)}
    info = json.loads((RUNS / run_id / "run.json").read_text())
    starts = sorted(t["started"] for t in traces)
    all_lines = read_jsonl(RUNS / run_id / "traces.jsonl")
    res = {"title": "Lost Thread: raw counts (not scores)", "run": run_id, "traces": len(traces),
           "trace_lines_incl_superseded": len(all_lines),
           "superseded_with_errors": sum(bool(t.get("errors")) for t in all_lines) - sum(bool(t.get("errors")) for t in traces),
           "lock_sha256": info.get("lock_sha256"), "first_start": starts[0] if starts else None,
           "last_start": starts[-1] if starts else None, "finished": info.get("finished"),
           "attempts_total": sum(t.get("attempts") or 1 for t in traces),
           "system_variants": sorted({str(t.get("system_variant")) for t in traces}),
           "cells": out,
           "by_type": {k: f"{int(v['checkable_hits'])}/{int(v['checkable_facts'])}" for k, v in sorted(by_type.items())},
           "models": sorted({f"{t['contestant']}: {t.get('model')} / {t.get('cli')}" for t in traces if t.get("model")})}
    write_json(RUNS / run_id / "summary.json", res)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    r = summarize(ap.parse_args().run)
    print(json.dumps(r, indent=1, ensure_ascii=False))
