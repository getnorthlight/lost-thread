#!/usr/bin/env python3
"""Lost Thread judging: build the judge inputs from a blinded pack, then score the judges' outputs.

  python3 bench/lt_judge.py prep  grading/<pack>      # writes judge/answers-<k>.jsonl, judge/pairs-<k>.jsonl (judges read these)
  python3 bench/lt_judge.py score grading/<pack>      # reads judge/out/*.jsonl + mapping.json, writes results.json
  python3 bench/lt_judge.py score grading/<pack> --out out-<name>   # score another judge's outputs (regrade.py)

Judges never see mapping.json or judge/pairs_key.json.
Pairs: for each (item, contestant), the `native` answer vs the `native+dd` answer, shown twice with sides swapped.
"""
import json, os, random, sys, glob, statistics as st
from collections import defaultdict

SEED = 20261006
BASE, HEAD = "native", "native+dd"


def load_pack(pack):
    items = json.load(open(os.path.join(pack, "items.json")))
    mapping = json.load(open(os.path.join(pack, "mapping.json")))
    answers = {}
    for f in sorted(glob.glob(os.path.join(pack, "shard-*.jsonl"))):
        for line in open(f):
            if line.strip():
                a = json.loads(line); answers[a["blind_id"]] = a
    return items, mapping, answers


def prep(pack, shards=4):
    items, mapping, answers = load_pack(pack)
    out = os.path.join(pack, "judge"); os.makedirs(os.path.join(out, "out"), exist_ok=True)
    rng = random.Random(SEED)
    # 1) per-answer grading shards: whole items per shard
    item_ids = sorted(items)
    by_item = defaultdict(list)
    for b, a in answers.items(): by_item[a["item_id"]].append(a)
    files = [open(os.path.join(out, f"answers-{k+1}.jsonl"), "w") for k in range(shards)]
    for i, iid in enumerate(item_ids):
        rows = by_item[iid][:]; rng.shuffle(rows)
        for r in rows: files[i % shards].write(json.dumps({"blind_id": r["blind_id"], "item_id": iid, "answer": r["answer"]}) + "\n")
    for f in files: f.close()
    # 2) pairwise: same item, same contestant, BASE vs HEAD, both orders
    groups = defaultdict(dict)
    for b, m in mapping.items():
        if m["arm"] in (BASE, HEAD) and b in answers: groups[(m["item_id"], m["contestant"])][m["arm"]] = b
    pairs, key = [], {}
    n = 0
    for (iid, c), g in sorted(groups.items()):
        if BASE not in g or HEAD not in g: continue
        for order in (0, 1):
            n += 1; pid = f"P{n:04d}"
            left, right = (g[BASE], g[HEAD]) if (order == 0) == (rng.random() < 0.5) else (g[HEAD], g[BASE])
            pairs.append({"pair_id": pid, "item_id": iid, "answer_1": answers[left]["answer"], "answer_2": answers[right]["answer"]})
            key[pid] = {"item_id": iid, "contestant": c, "answer_1": left, "answer_2": right}
    # make the two orders of a pair land as true swaps
    fixed = {}
    for pid, k in key.items():
        sib = (k["item_id"], k["contestant"])
        fixed.setdefault(sib, []).append(pid)
    for sib, pids in fixed.items():
        a, b = pids
        if key[a]["answer_1"] == key[b]["answer_1"]:
            ka = key[b]; ka["answer_1"], ka["answer_2"] = ka["answer_2"], ka["answer_1"]
            p = next(x for x in pairs if x["pair_id"] == b); p["answer_1"], p["answer_2"] = p["answer_2"], p["answer_1"]
    rng.shuffle(pairs)
    pf = [open(os.path.join(out, f"pairs-{k+1}.jsonl"), "w") for k in range(shards)]
    for i, p in enumerate(pairs): pf[i % shards].write(json.dumps(p) + "\n")
    for f in pf: f.close()
    json.dump(key, open(os.path.join(out, "pairs_key.json"), "w"), indent=1)
    print(json.dumps({"answers": len(answers), "items": len(items), "pairs": len(pairs), "shards": shards}))


def score(pack, outdir="out"):
    items, mapping, answers = load_pack(pack)
    out = os.path.join(pack, "judge")
    key = json.load(open(os.path.join(out, "pairs_key.json")))
    grades, prefs = defaultdict(list), defaultdict(list)
    for f in glob.glob(os.path.join(out, outdir, "*.jsonl")):
        for line in open(f):
            line = line.strip()
            if not line: continue
            try: r = json.loads(line)
            except Exception: continue
            if "blind_id" in r: grades[r["blind_id"]].append(r)
            elif "pair_id" in r: prefs[r["pair_id"]].append(r)
    # per answer: median of judges
    per = {}
    for b, rs in grades.items():
        def med(k):
            v = [r[k] for r in rs if isinstance(r.get(k), (int, float))]
            return st.median(v) if v else None
        per[b] = {"correct": med("correct"), "helpful": med("helpful"), "overclaim": med("overclaim"),
                  "fabrications": med("fabrications"), "n_judges": len(rs), "len": len(answers[b]["answer"]),
                  "spread": (max(r.get("helpful", 0) for r in rs) - min(r.get("helpful", 0) for r in rs)) if rs else None}
    # aggregate by contestant x arm
    agg = defaultdict(lambda: defaultdict(list))
    for b, s in per.items():
        m = mapping[b]; a = agg[(m["contestant"], m["arm"])]
        for k in ("correct", "helpful", "overclaim", "fabrications"):
            if s[k] is not None: a[k].append(s[k])
        a["helpful3"].append(1 if (s["helpful"] or 0) >= 3 else 0)
        a["model"] = m["model"]
    table = {}
    for (c, arm), a in sorted(agg.items()):
        n = len(a["helpful"])
        table[f"{c}|{arm}"] = {"model": a["model"], "n": n,
            "helpful_mean": round(st.mean(a["helpful"]), 2) if n else None,
            "helpful_3plus": sum(a["helpful3"]),
            "correct_mean": round(st.mean(a["correct"]), 2) if a["correct"] else None,
            "answers_with_fabrication": sum(1 for x in a["fabrications"] if x >= 1),
            "answers_overclaiming": sum(1 for x in a["overclaim"] if x >= 1)}
    # pairwise: per (item, contestant) combine both orders
    pw = defaultdict(lambda: {"head": 0, "base": 0, "tie": 0})
    per_pair = defaultdict(list)
    for pid, rs in prefs.items():
        k = key[pid]
        for r in rs:
            w = r.get("winner")
            if w in ("1", "2", 1, 2):
                chosen = k["answer_1"] if str(w) == "1" else k["answer_2"]
                per_pair[(k["item_id"], k["contestant"])].append("head" if mapping[chosen]["arm"] == HEAD else "base")
            else:
                per_pair[(k["item_id"], k["contestant"])].append("tie")
    for (iid, c), votes in per_pair.items():
        h, b = votes.count("head"), votes.count("base")
        pw[c]["head" if h > b else "base" if b > h else "tie"] += 1
    # length bias check: within each (item, contestant) arm pair, does longer answer win more than the judges' helpful scores imply?
    lens = [(s["len"], s["helpful"]) for s in per.values() if s["helpful"] is not None]
    def corr(xs, ys):
        mx, my = st.mean(xs), st.mean(ys); sx = sum((x-mx)**2 for x in xs)**.5; sy = sum((y-my)**2 for y in ys)**.5
        return round(sum((x-mx)*(y-my) for x, y in zip(xs, ys))/(sx*sy), 2) if sx and sy else None
    res = {"table": table, "pairwise": pw, "length_helpful_corr": corr([l for l, _ in lens], [h for _, h in lens]),
           "judge_disagreement_helpful_ge2": sum(1 for s in per.values() if (s["spread"] or 0) >= 2),
           "answers_graded": len(per), "answers_total": len(answers), "pairs_judged": len(prefs), "pairs_total": len(key)}
    dest = out if outdir == "out" else os.path.join(out, outdir)
    json.dump(res, open(os.path.join(dest, "results.json"), "w"), indent=1, default=dict)
    json.dump(per, open(os.path.join(dest, "per_answer.json"), "w"), indent=1)
    print(json.dumps(res, indent=1, default=dict))


if __name__ == "__main__":
    cmd, pack = sys.argv[1], sys.argv[2]
    if cmd == "score" and "--out" in sys.argv:
        score(pack, sys.argv[sys.argv.index("--out") + 1])
    else:
        {"prep": prep, "score": score}[cmd](pack)
