"""Lost Thread: blinded answer packs for the judges, code checks for checkable facts, and the final score table.

    python3 -m bench.pack make  --runs lost-thread-developer --runs ... --pack lost-thread-20261006 [--shards 6]
    python3 -m bench.pack score --pack pilot-20261006

make  -> grading/<pack>/
          items.json             per item: question, need, can_know, reference answer, key facts (+ optional), negative,
                                 hallucination note (frozen items come from lost-thread/questions.lock.json)
          shard-<k>.jsonl        blinded answers, grouped by item, shuffled: {blind_id, item_id, answer}
          code_checks.jsonl      per blind_id: the regex/number test of each checkable fact (NOT shown to judges)
          mapping.json           blind_id -> run, trace, arm, contestant, model (NOT shown to judges)
score -> reads grading/<pack>/grades/*.jsonl (judge output, see GRADING.md), reconciles with code checks, and writes
          disagreements.jsonl and results.json / results.md (by arm x contestant x type).

Blinding: the judges see the question, the reference and one answer at a time, never the arm, the contestant, the
model, the tool calls or the memory block. Answers are normalized so style can't reveal the app: markdown emphasis,
headings, links and code fences are flattened, bullets made uniform, DayDream item ids and tool/app self-references
replaced with neutral words.
"""
from __future__ import annotations

import argparse
import getpass
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from .common import PERSONAS, ROOT, RUNS, append_jsonl, read_json, read_jsonl, write_json, write_jsonl

GRADING = ROOT / "grading"
LOCK = ROOT / "lost-thread" / "questions.lock.json"
TITLE = "Lost Thread"

SELF_REFS = [
    (r"\bDayDream(?:'s)?\b", "the activity history"),
    (r"\bmac-mem\b", "the activity history"),
    (r"\b(ChatGPT|Codex)'s memory\b", "memory"),
    (r"\bmy (saved )?memor(y|ies)\b", "memory"),
    (r"\bAs (an AI|ChatGPT|Claude|Codex)[^,.]*[,.]\s*", ""),
    (r"\bI'?m (Claude|ChatGPT|Codex)\b[^.]*\.\s*", ""),
]


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def normalize(text: str, known_emails: set[str] | None = None) -> str:
    t = text or ""
    # Claude Code puts the signed-in account's email in its context even with --system-prompt (README "Symmetric
    # prompt"). Any address the persona's world doesn't contain is redacted, so neither the operator's address nor its
    # presence (a tell for the contestant) reaches the judges.
    t = EMAIL.sub(lambda m: m.group(0) if m.group(0).lower() in (known_emails or set()) else "<email>", t)
    t = re.sub(r"/private/tmp/assistant-chat", "<folder>", t)
    # Tool calls a model wrote out as text when it had no tools (Claude Code's default prompt, arms without DayDream):
    # they reveal the setup and are not part of what the person reads as the answer. Any claims made about their
    # "results" in the prose stay and are graded as claims.
    t = re.sub(r"<invoke name=.*?</invoke>", "", t, flags=re.S)
    t = re.sub(r"<function_results>.*?</function_results>", "", t, flags=re.S)
    t = re.sub(r"/Users/[^/\s\"'`]+", "/Users/<user>", t)                   # local account names in paths
    # An account name a model claims to have looked up (made up from the path above in the pilot): "logged in as X".
    m = re.search(r"logged in as\s*`?([\w.-]+)", t)
    if m:
        t = re.sub(r"\b" + re.escape(m.group(1)) + r"\b", "<account>", t)
    if getpass.getuser():
        t = re.sub(re.escape(getpass.getuser()), "<user>", t, flags=re.I)
    t = re.sub(r"```[a-zA-Z]*\n?(.*?)```", lambda m: m.group(1).strip(), t, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", t)                     # links -> text
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)                               # bold
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)       # italics
    t = re.sub(r"(?<![\w`])`([^`\n]+)`", r"\1", t)                           # inline code
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)                      # headings
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "- ", t, flags=re.M)            # bullets
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)                               # quotes
    t = re.sub(r"^\s*([-*_]\s*){3,}$", "", t, flags=re.M)                    # rules
    t = re.sub(r"\(?\b\d{4}-[a-z0-9]{5}\b\)?", "", t)                         # DayDream item ids
    for rx, rep in SELF_REFS:
        t = re.sub(rx, rep, t, flags=re.I)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(hours?|hrs?|h\b|minutes?|mins?|m\b)?", re.I)


def number_check(answer: str, chk: dict) -> dict:
    """Closest stated quantity (hours converted to minutes when the unit is minutes) vs the ledger value."""
    target = float(chk["value"])
    cands = []
    for m in NUM.finditer(answer):
        v, unit = float(m.group(1)), (m.group(2) or "").lower()
        if unit.startswith("h") and chk.get("unit", "minutes").startswith("min"):
            v *= 60
        elif not unit and not (0.2 * target <= v <= 5 * target):
            continue
        cands.append(v)
    hm = re.search(r"(\d+)\s*h(?:ours?|rs?)?\s*(?:and\s*)?(\d+)\s*m", answer, re.I)
    if hm:
        cands.append(int(hm.group(1)) * 60 + int(hm.group(2)))
    if not cands:
        return {"score": 0, "found": None}
    best = min(cands, key=lambda v: abs(v - target))
    err = abs(best - target) / max(target, 1e-9)
    return {"score": 2 if err <= chk.get("full", 0.25) else 1 if err <= chk.get("half", 0.5) else 0, "found": best,
            "rel_error": round(err, 3)}


def code_checks(answer: str, item: dict) -> dict:
    out = {}
    for f in item["facts"]:
        chk = f.get("check")
        if not chk:
            continue
        if chk["kind"] == "regex":
            hit = bool(re.search(chk["pattern"], answer, re.I))
            out[f["id"]] = {"kind": "regex", "score": 2 if hit else 0}
        elif chk["kind"] == "number":
            out[f["id"]] = {"kind": "number", **number_check(answer, chk)}
    return out


def make(run_ids: list[str], pack: str, shards: int, seed: int):
    pdir = GRADING / pack
    if pdir.exists() and any(pdir.iterdir()):
        raise SystemExit(f"{pdir} exists; choose a new --pack name")
    traces = []
    for r in run_ids:
        latest = {}
        for t in read_jsonl(RUNS / r / "traces.jsonl"):
            latest[(t["item_id"], t["arm"], t["contestant"])] = t   # re-runs (after a timeout) replace the earlier trace
        traces += list(latest.values())
    items, emails = {}, defaultdict(set)
    lock = read_json(LOCK) if LOCK.exists() else None
    for r in run_ids:   # the frozen items (Lost Thread lock), else the run's inputs snapshot / the persona's current ones
        info = read_json(RUNS / r / "run.json")
        if info.get("lock_sha256"):
            if not lock or lock["sha256"] != info["lock_sha256"]:
                raise SystemExit(f"{r} ran under lock {info['lock_sha256'][:12]} but the lock is now "
                                 f"{(lock or {}).get('sha256', 'missing')[:12]}; amendments need a rerun first")
            for it in lock["items"]:
                if it["persona"] == info["persona"]:
                    items[it["id"]] = it
        else:
            snap = RUNS / r / "inputs" / "questions.jsonl"
            src = snap if snap.exists() else PERSONAS / info["persona"] / "questions.jsonl"
            for it in read_jsonl(src):
                items[it["id"]] = it
        world = RUNS / r / "inputs" / "ledger.json"
        if world.exists():
            emails[info["persona"]] |= {e.lower() for e in EMAIL.findall(world.read_text())}
    rng = random.Random(seed)
    order = list(range(len(traces)))
    rng.shuffle(order)
    blind = {}
    for n, i in enumerate(order, 1):
        blind[i] = f"A{n:04d}"
    mapping, code, by_item = {}, [], defaultdict(list)
    for i, t in enumerate(traces):
        b = blind[i]
        ans = normalize(t.get("answer") or "", emails[t["persona"]])
        if not ans:
            ans = "(no answer)"
        mapping[b] = {"run_id": t["run_id"], "trace_id": t["trace_id"], "item_id": t["item_id"], "arm": t["arm"],
                      "contestant": t["contestant"], "model": t.get("model"), "cli": t.get("cli"),
                      "n_tool_calls": t.get("n_tool_calls"), "errors": t.get("errors"), "seconds": t.get("seconds")}
        code.append({"blind_id": b, "item_id": t["item_id"], "checks": code_checks(ans, items[t["item_id"]])})
        by_item[t["item_id"]].append({"blind_id": b, "item_id": t["item_id"], "answer": ans})
    # Shards: whole items per shard (a judge grades every answer to an item side by side, consistently), answers
    # shuffled inside the item.
    item_ids = sorted(by_item)
    rng.shuffle(item_ids)
    for k in range(shards):
        rows = []
        for iid in item_ids[k::shards]:
            group = by_item[iid][:]
            rng.shuffle(group)
            rows += group
        write_jsonl(pdir / f"shard-{k + 1}.jsonl", rows)
    public_items = {iid: {"item_id": iid, "type": it["type"], "question": it["question"], "need": it.get("need"),
                          "can_know": it.get("can_know"), "reference": it["reference"],
                          "negative": it["negative"], "hallucination": it.get("hallucination"),
                          "facts": [{"id": f["id"], "text": f["text"], "optional": f["optional"]} for f in it["facts"]]}
                    for iid, it in items.items() if iid in by_item}
    write_json(pdir / "items.json", public_items)
    write_json(pdir / "mapping.json", mapping)
    write_jsonl(pdir / "code_checks.jsonl", code)
    (pdir / "grades").mkdir(parents=True, exist_ok=True)
    print(f"{len(traces)} answers, {len(public_items)} items, {shards} shards -> {pdir}")


def item_score(g: dict, item: dict) -> float:
    """0..1: mean of the required key facts (0/1/2 each, /2), minus 0.25 per material hallucination and 0.25 per
    overclaim (a claim beyond the item's can_know, e.g. "you sent it"), floored at -0.5.
    Negatives: the abstention/denial verdict replaces the first fact (2 = says it didn't happen / no sign of it,
    1 = can't tell, 0 = claims it happened). need_met (0/1/2, helpfulness against the item's need) is reported
    separately, not mixed into this score."""
    req = [f for f in item["facts"] if not f["optional"]]
    vals = [g["facts"].get(f["id"], 0) for f in req]
    if item["negative"] and req:
        vals[0] = {"correct": 2, "unsure": 1, "wrong": 0}.get(g.get("negative_verdict"), vals[0])
    base = sum(vals) / (2 * len(vals)) if vals else 0
    return max(-0.5, base - 0.25 * int(g.get("hallucinations", 0)) - 0.25 * int(g.get("overclaims", 0)))


def combine(gs: list[dict]) -> tuple[dict, list[str]]:
    """One grade from the judges' grades: an adjudicated grade wins; otherwise the per-fact median, the median
    hallucination count and the majority verdicts. Returns (grade, reasons it needs adjudication)."""
    adj = [g for g in gs if g.get("adjudicated")]
    if adj:
        return adj[-1], []
    med = lambda xs: sorted(xs)[len(xs) // 2]
    facts = {}
    why = []
    for fid in sorted({k for g in gs for k in g["facts"]}):
        vals = [g["facts"].get(fid, 0) for g in gs]
        facts[fid] = med(vals)
        if max(vals) - min(vals) >= 2:
            why.append(f"fact {fid}: {vals}")
    hal = [int(g.get("hallucinations", 0)) for g in gs]
    if len(set(h > 0 for h in hal)) > 1:
        why.append(f"hallucinations: {hal}")
    over = [int(g.get("overclaims", 0)) for g in gs]
    if len(set(o > 0 for o in over)) > 1:
        why.append(f"overclaims: {over}")
    need = [int(g.get("need_met", 0)) for g in gs]
    if max(need) - min(need) >= 2:
        why.append(f"need_met: {need}")
    nv = [g.get("negative_verdict") for g in gs if g.get("negative_verdict")]
    verdict = max(set(nv), key=nv.count) if nv else None
    if nv and nv.count(verdict) * 2 <= len(nv):
        why.append(f"negative_verdict: {nv}")
    ab = [bool(g.get("abstained")) for g in gs]
    return {"facts": facts, "hallucinations": med(hal), "overclaims": med(over), "need_met": med(need),
            "negative_verdict": verdict, "abstained": ab.count(True) * 2 > len(ab)}, why


def score(pack: str):
    pdir = GRADING / pack
    items = read_json(pdir / "items.json")
    mapping = read_json(pdir / "mapping.json")
    code = {c["blind_id"]: c for c in read_jsonl(pdir / "code_checks.jsonl")}
    grades = {}
    for f in sorted((pdir / "grades").glob("*.jsonl")):
        for g in read_jsonl(f):
            grades.setdefault(g["blind_id"], []).append(g)
    need_adj, dis, rows = [], [], []
    for b, m in mapping.items():
        gs = grades.get(b)
        if not gs:
            continue
        g, why = combine(gs)
        it = items[m["item_id"]]
        for fid, c in code[b]["checks"].items():
            j = g["facts"].get(fid)
            if j is not None and (j > 0) != (c["score"] > 0):
                why.append(f"fact {fid}: judges {j} vs code {c}")
                dis.append({"blind_id": b, "item_id": m["item_id"], "fact": fid, "judge": j, "code": c})
        if why:
            need_adj.append({"blind_id": b, "item_id": m["item_id"], "why": why, "grades": gs})
        rows.append({**m, "type": it["type"], "score": item_score(g, it), "hallucinations": int(g.get("hallucinations", 0)),
                     "overclaims": int(g.get("overclaims", 0)), "need_met": int(g.get("need_met", 0)),
                     "abstained": bool(g.get("abstained")), "negative_verdict": g.get("negative_verdict")})
    write_jsonl(pdir / "disagreements.jsonl", dis)
    write_jsonl(pdir / "needs_adjudication.jsonl", need_adj)
    table = defaultdict(list)
    for r in rows:
        table[(r["arm"], r["contestant"])].append(r)
        table[(r["arm"], r["contestant"], r["type"])].append(r)
    res = {" | ".join(k): {"n": len(v), "mean": round(sum(x["score"] for x in v) / len(v), 3),
                           "need_met": round(sum(x["need_met"] for x in v) / (2 * len(v)), 3),
                           "hallucinations": sum(x["hallucinations"] for x in v),
                           "overclaims": sum(x["overclaims"] for x in v),
                           "abstained": sum(x["abstained"] for x in v)} for k, v in sorted(table.items())}
    # Headline: each model against itself, native -> native+dd, paired by item.
    head = {}
    for c in sorted({r["contestant"] for r in rows}):
        a = {r["item_id"]: r for r in rows if r["contestant"] == c and r["arm"] == "native"}
        b = {r["item_id"]: r for r in rows if r["contestant"] == c and r["arm"] == "native+dd"}
        both = sorted(set(a) & set(b))
        if both:
            d = [b[i]["score"] - a[i]["score"] for i in both]
            mean = lambda xs: round(sum(xs) / len(xs), 3)
            head[c] = {"items": len(both), "native": mean([a[i]["score"] for i in both]),
                       "native+dd": mean([b[i]["score"] for i in both]),
                       "uplift": mean(d), "better": sum(x > 0 for x in d),
                       "worse": sum(x < 0 for x in d), "same": sum(x == 0 for x in d),
                       "need_met_native": mean([a[i]["need_met"] / 2 for i in both]),
                       "need_met_native+dd": mean([b[i]["need_met"] / 2 for i in both])}
    write_json(pdir / "results.json", {"title": TITLE, "graded": len(rows), "of": len(mapping), "needs_adjudication": len(need_adj),
                                       "judge_code_disagreements": len(dis), "headline": head, "cells": res})
    lines = [f"# {TITLE}: results", "", f"Graded {len(rows)} of {len(mapping)} answers.", "",
             "| arm | contestant | type | n | mean score | need met | hallucinations | overclaims | abstained |",
             "|---|---|---|---|---|---|---|---|---|"]
    for k, v in res.items():
        parts = k.split(" | ") + ([""] if k.count(" | ") == 1 else [])
        lines.append(f"| {parts[0]} | {parts[1]} | {parts[2] or 'ALL'} | {v['n']} | {v['mean']} | {v['need_met']} | "
                     f"{v['hallucinations']} | {v['overclaims']} | {v['abstained']} |")
    (pdir / "results.md").write_text("\n".join(lines) + "\n")
    (pdir / "results.svg").write_text(chart(res))
    print(f"graded {len(rows)}/{len(mapping)}; {len(need_adj)} need adjudication; {len(dis)} judge/code disagreements")
    print(json.dumps(head, indent=1))


def chart(res: dict) -> str:
    """Grouped bars: mean score per arm for each contestant (all items). Plain SVG, no dependencies."""
    arms = ["native", "native+dd", "dd"]
    names = {"native": "Native memory", "native+dd": "Native + DayDream", "dd": "DayDream only"}
    cs = sorted({k.split(" | ")[1] for k in res})
    colors = {"native": "#9aa5b1", "native+dd": "#3b6fd8", "dd": "#7fb0f0"}
    W, H, top, base = 640, 360, 60, 300
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="-apple-system,Helvetica,Arial" font-size="13">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="20" y="30" font-size="18" font-weight="600">{TITLE}: mean score by setup</text>',
           f'<line x1="60" y1="{base}" x2="{W - 20}" y2="{base}" stroke="#333"/>']
    gw = (W - 100) / max(1, len(cs))
    for ci, c in enumerate(cs):
        x0 = 70 + ci * gw
        for ai, a in enumerate(arms):
            v = res.get(f"{a} | {c}", {}).get("mean")
            if v is None:
                continue
            h = max(0, v) * (base - top)
            x = x0 + ai * (gw - 30) / 3
            out.append(f'<rect x="{x:.0f}" y="{base - h:.0f}" width="{(gw - 40) / 3:.0f}" height="{h:.0f}" fill="{colors[a]}"/>')
            out.append(f'<text x="{x + (gw - 40) / 6:.0f}" y="{base - h - 6:.0f}" text-anchor="middle">{v:.2f}</text>')
        label = {"codex": "ChatGPT (Codex CLI)", "claude": "Claude (Claude Code)"}.get(c, c)
        out.append(f'<text x="{x0 + (gw - 40) / 2:.0f}" y="{base + 20}" text-anchor="middle">{label}</text>')
    for ai, a in enumerate(arms):
        out.append(f'<rect x="{70 + ai * 170}" y="{H - 28}" width="12" height="12" fill="{colors[a]}"/>'
                   f'<text x="{88 + ai * 170}" y="{H - 18}">{names[a]}</text>')
    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--runs", action="append", required=True)
    m.add_argument("--pack", required=True)
    m.add_argument("--shards", type=int, default=4)
    m.add_argument("--seed", type=int, default=20261006)
    s = sub.add_parser("score")
    s.add_argument("--pack", required=True)
    a = ap.parse_args()
    if a.cmd == "make":
        make(a.runs, a.pack, a.shards, a.seed)
    else:
        score(a.pack)
