"""Freeze the Lost Thread question set BEFORE any contestant run (rule, 2026-10-06).

    python3 -m bench.freeze --personas developer,student,founder          # writes lost-thread/questions.lock.json
    python3 -m bench.freeze --verify                                      # recompute and compare every hash
    python3 -m bench.freeze --amend "why" --rerun developer-q03           # fixture fix: new lock version + CHANGELOG

The lock holds every item exactly as graded (question, need, can_know, reference, facts, negative/hallucination) and
sha256 hashes of each persona's questions.jsonl, native_memory.json, events.jsonl (the rows DayDream's store is loaded
from) and ledger.json. bench.harness refuses to run a locked persona whose files no longer match.

Rules: no question may be dropped or edited after results are seen. A fixture fix (world/events) is allowed only with
--amend: the question, need and can_know texts must stay identical, the reason goes into lost-thread/CHANGELOG.md, and
the listed items must be rerun for every arm and contestant.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from .common import ROOT as BENCH, PERSONAS, TZ, read_json, read_jsonl, write_json

LOST = BENCH / "lost-thread"
LOCK = LOST / "questions.lock.json"
CHANGELOG = LOST / "CHANGELOG.md"
FROZEN_FIELDS = ("id", "persona", "type", "question", "need", "can_know", "reference", "negative", "hallucination",
                 "facts", "answerable_from")
TEXT_FIELDS = ("question", "need", "can_know")


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sha_obj(o) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def persona_hashes(p: str) -> dict:
    d = PERSONAS / p
    a = read_json(d / "anchor.json")
    return {"questions_jsonl_sha256": sha_file(d / "questions.jsonl"),
            "native_memory_sha256": sha_file(d / "native_memory.json"),
            "events_sha256": sha_file(d / "events.jsonl"),
            "ledger_facts_sha256": sha_obj(read_json(d / "ledger.json")["facts"]),
            "store_sha256_at_load": a["store_sha256"], "ask_day": a["ask_day"], "rows": a["rows"],
            "typed_retention": a.get("typed_retention"), "generator": a.get("generator"), "source": a.get("source")}


def collect(personas: list[str]) -> tuple[dict, list]:
    hashes, items = {}, []
    for p in personas:
        d = PERSONAS / p
        bc = read_json(d / "build-check.json")
        if bc["errors"]:
            sys.exit(f"{p}: build has errors, can't freeze: {bc['errors'][:3]}")
        its = read_jsonl(d / "questions.jsonl")
        for i in its:
            missing = [k for k in ("need", "can_know") if not i.get(k)]
            if missing:
                sys.exit(f"{i['id']}: missing {missing}; every frozen item needs need and can_know")
        hashes[p] = {**persona_hashes(p), "n_items": len(its)}
        items += [{k: i.get(k) for k in FROZEN_FIELDS} for i in its]
    return hashes, items


def body(hashes: dict, items: list) -> dict:
    return {"name": "Lost Thread", "personas": hashes, "items": items, "items_sha256": sha_obj(items)}


def freeze(personas: list[str]) -> dict:
    if LOCK.exists():
        sys.exit(f"{LOCK} exists: the set is frozen. Use --amend for a recorded fixture fix.")
    hashes, items = collect(personas)
    b = body(hashes, items)
    lock = {**b, "version": 1, "frozen_at": dt.datetime.now(TZ).isoformat(), "sha256": sha_obj(b),
            "rules": "No item dropped or edited after results; fixture fixes only via --amend with a CHANGELOG entry "
                     "and a rerun of the item for every arm and contestant."}
    LOST.mkdir(exist_ok=True)
    write_json(LOCK, lock)
    (LOST / "questions.lock.sha256").write_text(f"{lock['sha256']}  questions.lock.json (version 1)\n")
    if not CHANGELOG.exists():
        CHANGELOG.write_text("# Lost Thread changelog\n\nEvery change to the frozen set after the freeze, with the items "
                             "rerun for every arm and contestant.\n\n"
                             f"- {lock['frozen_at']}: v1 frozen, {len(items)} items, sha256 {lock['sha256']}. "
                             "No contestant run before this.\n")
    return lock


def verify(lock: dict | None = None, personas: list[str] | None = None) -> list[str]:
    lock = lock or read_json(LOCK)
    probs = []
    b = {k: lock[k] for k in ("name", "personas", "items", "items_sha256")}
    if sha_obj(b) != lock["sha256"]:
        probs.append("lock body does not match its sha256 (the lock file was edited)")
    if sha_obj(lock["items"]) != lock["items_sha256"]:
        probs.append("items do not match items_sha256")
    for p in personas or list(lock["personas"]):
        want = lock["personas"].get(p)
        if not want:
            probs.append(f"{p}: not in the lock")
            continue
        got = persona_hashes(p)
        for k in ("questions_jsonl_sha256", "native_memory_sha256", "events_sha256", "ledger_facts_sha256", "ask_day"):
            if got[k] != want[k]:
                probs.append(f"{p}: {k} changed since the freeze ({want[k][:12]} -> {got[k][:12]})")
    return probs


def verify_texts(personas: list[str]) -> list[str]:
    """For a rerun on a later day (harness --new-day): the dates move, but every item's question, need and can_know
    must be exactly what was frozen, and no item may be added or dropped."""
    lock = read_json(LOCK)
    frozen = {i["id"]: i for i in lock["items"] if i["persona"] in personas}
    now = {}
    for p in personas:
        for i in read_jsonl(PERSONAS / p / "questions.jsonl"):
            now[i["id"]] = i
    probs = [f"items added or dropped: {sorted(set(frozen) ^ set(now))}"] if set(frozen) != set(now) else []
    for k in sorted(set(frozen) & set(now)):
        for f in TEXT_FIELDS:
            if now[k].get(f) != frozen[k].get(f):
                probs.append(f"{k}.{f} differs from the lock (the rebuilt week words it differently; rerun on a "
                             f"{dt.date.fromisoformat(lock['personas'][frozen[k]['persona']]['ask_day']).strftime('%A')})")
    return probs


def amend(reason: str, rerun: list[str]) -> dict:
    old = read_json(LOCK)
    hashes, items = collect(list(old["personas"]))
    before = {i["id"]: i for i in old["items"]}
    after = {i["id"]: i for i in items}
    if set(before) != set(after):
        sys.exit(f"items added or dropped: {sorted(set(before) ^ set(after))}; not allowed after the freeze")
    for k, i in after.items():
        for f in TEXT_FIELDS:
            if i.get(f) != before[k].get(f):
                sys.exit(f"{k}.{f} changed; questions, needs and can_know are frozen")
    changed = sorted(k for k in after if after[k] != before[k])
    if not set(changed) <= set(rerun):
        sys.exit(f"items changed but not listed for rerun: {sorted(set(changed) - set(rerun))}")
    b = body(hashes, items)
    lock = {**b, "version": old["version"] + 1, "frozen_at": old["frozen_at"], "amended_at": dt.datetime.now(TZ).isoformat(),
            "sha256": sha_obj(b), "previous_sha256": old["sha256"], "rules": old["rules"],
            "amendments": old.get("amendments", []) + [{"at": dt.datetime.now(TZ).isoformat(), "reason": reason,
                                                       "rerun": rerun, "changed": changed}]}
    write_json(LOCK, lock)
    (LOST / "questions.lock.sha256").write_text(f"{lock['sha256']}  questions.lock.json (version {lock['version']})\n")
    with CHANGELOG.open("a") as f:
        f.write(f"- {lock['amended_at']}: v{lock['version']} ({lock['sha256']}): {reason}. Changed: {changed or 'none'}. "
                f"Rerun for every arm and contestant: {rerun}.\n")
    return lock


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--personas", default="developer,student,founder")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--amend")
    ap.add_argument("--rerun", default="")
    a = ap.parse_args()
    if a.verify:
        pr = verify()
        print("\n".join(pr) if pr else f"lock ok: {read_json(LOCK)['sha256']}")
        sys.exit(1 if pr else 0)
    if a.amend:
        l = amend(a.amend, [x for x in a.rerun.split(",") if x])
    else:
        l = freeze(a.personas.split(","))
    print(json.dumps({"version": l["version"], "sha256": l["sha256"], "items": len(l["items"]),
                      "personas": {p: v["questions_jsonl_sha256"] for p, v in l["personas"].items()}}, indent=1))
