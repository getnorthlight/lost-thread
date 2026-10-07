#!/usr/bin/env python3
"""Re-grade Lost Thread's 162 answers with your own judge, through any OpenAI-compatible chat API.

Uses the same instructions as the published grades (judge/grade-prompt.md, judge/pair-prompt.md) and writes the same
line format, so the published scorer works on the result:

  export OPENAI_API_KEY=...                       # or --api-key-env NAME; --base-url for other providers
  python3 regrade.py --model gpt-6.1 --name gpt61 --dry-run --limit 1   # print one request, send nothing
  python3 regrade.py --model gpt-6.1 --name gpt61                         # 3 graders per answer, 2 per pair
  python3 bench/lt_judge.py score grading/lost-thread-20261006 --out out-gpt61

Judges see the item (question, what really happened, the person's need, what the history can know) and one answer,
never which model or setup wrote it. Reruns resume: lines already written are skipped. Python 3.10+, no packages.
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ITEM_FIELDS = ("question", "reference", "facts", "negative", "hallucination", "need", "can_know")
GRADE_FORMAT = ('Reply with one JSON object only: {"correct": 0, "helpful": 0, "fabrications": 0, "overclaim": 0, '
                '"negative_verdict": null, "note": "..."}')
PAIR_FORMAT = 'Reply with one JSON object only: {"winner": "1" | "2" | "tie", "reason": "one short sentence"}'


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def done_ids(path, key):
    return {r[key] for r in read_jsonl(path)} if os.path.exists(path) else set()


def ask(args, system, user):
    body = {"model": args.model, "temperature": args.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if args.json_mode:
        body["response_format"] = {"type": "json_object"}
    if args.dry_run:
        print(json.dumps({"url": args.base_url.rstrip("/") + "/chat/completions", "body": body}, indent=1, ensure_ascii=False))
        return None
    key = os.environ.get(args.api_key_env)
    if not key:
        sys.exit(f"set {args.api_key_env} (or pass --api-key-env)")
    req = urllib.request.Request(args.base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as r:
                text = json.load(r)["choices"][0]["message"]["content"] or ""
            start, end = text.find("{"), text.rfind("}")
            return json.loads(text[start:end + 1])
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"retry {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError("the judge kept failing; stopping so nothing half-graded is scored")


def item_text(item):
    return json.dumps({k: item.get(k) for k in ITEM_FIELDS if k in item}, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--name", help="output folder suffix: judge/out-<name>/ (default: the model name)")
    ap.add_argument("--pack", default=os.path.join(HERE, "grading", "lost-thread-20261006"))
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY")
    ap.add_argument("--judges", type=int, default=3, help="independent graders per answer (published: 3)")
    ap.add_argument("--pair-judges", type=int, default=2, help="graders per pairwise file (published: 2; 0 to skip)")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--no-json-mode", dest="json_mode", action="store_false")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many requests (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="print the requests instead of sending them")
    args = ap.parse_args()

    pack = args.pack
    items = json.load(open(os.path.join(pack, "items.json"), encoding="utf-8"))
    grade_rules = open(os.path.join(HERE, "judge", "grade-prompt.md"), encoding="utf-8").read()
    pair_rules = open(os.path.join(HERE, "judge", "pair-prompt.md"), encoding="utf-8").read()
    name = args.name or args.model.replace("/", "-")
    out = os.path.join(pack, "judge", f"out-{name}")
    if not args.dry_run:
        os.makedirs(out, exist_ok=True)
    sent = 0

    jobs = []
    for path in sorted(glob.glob(os.path.join(pack, "judge", "answers-*.jsonl"))):
        shard = path.rsplit("-", 1)[1].split(".")[0]
        for j in range(1, args.judges + 1):
            jobs.append(("grade", path, os.path.join(out, f"grade-j{j}-s{shard}.jsonl"), f"j{j}"))
    if args.pair_judges:
        for path in sorted(glob.glob(os.path.join(pack, "judge", "pairs-*.jsonl"))):
            shard = path.rsplit("-", 1)[1].split(".")[0]
            for j in range(1, args.pair_judges + 1):
                jobs.append(("pair", path, os.path.join(out, f"pair-j{j}-s{shard}.jsonl"), f"p{j}"))

    for kind, src, dest, judge in jobs:
        key = "blind_id" if kind == "grade" else "pair_id"
        skip = set() if args.dry_run else done_ids(dest, key)
        for row in read_jsonl(src):
            if row[key] in skip:
                continue
            if args.limit and sent >= args.limit:
                print(f"stopped at --limit {args.limit}", file=sys.stderr)
                return
            item = item_text(items[row["item_id"]])
            if kind == "grade":
                user = f"Item:\n{item}\n\nAnswer to grade:\n{row['answer']}\n\n{GRADE_FORMAT}"
                verdict = ask(args, grade_rules, user)
                line = {"blind_id": row["blind_id"], "item_id": row["item_id"], "judge": judge, **(verdict or {})}
            else:
                user = (f"Item:\n{item}\n\nAnswer 1:\n{row['answer_1']}\n\nAnswer 2:\n{row['answer_2']}\n\n{PAIR_FORMAT}")
                verdict = ask(args, pair_rules, user)
                line = {"pair_id": row["pair_id"], "item_id": row["item_id"], "judge": judge, **(verdict or {})}
            sent += 1
            if verdict is not None:
                with open(dest, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"done: {sent} requests; score with: python3 bench/lt_judge.py score {os.path.relpath(pack, HERE)} --out out-{name}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
