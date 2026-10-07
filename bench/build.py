"""Materialize one persona for an ask day: world -> simulated DayDream rows -> isolated store -> memory blocks -> questions.

    python3 -m bench.build --persona dev-jonah [--source fixture|generated] [--ask-day YYYY-MM-DD] [--seed 11]

Inputs (personas/<p>/<source>/): world.json (persona, threads, week plan, chats), memory.json (saved memories + Claude
summary derived from the chats), questions.json (items with reference answers and key facts).
Outputs (personas/<p>/): world.json, events.jsonl, ledger.json, persona.json, native_memory.json, questions.jsonl,
anchor.json; stores/<p>/ (home/memory.sqlite, typed.key, load-report.json).

Validation (fails the build): every DayDream row was accepted by DayDream's own ingest; every key fact marked as being in
DayDream matches a stored row and every fact marked as in native memory matches both memory blocks; vague questions share
no content word with their target; negatives' "absent" conditions hold. Facts found where they were not expected are
reported as warnings (e.g. a control that DayDream can also answer).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys

from . import simulate as simmod
from . import store
from .common import PERSONAS, TZ, fmt_clock, fmt_day, local_dt, read_json, write_json, write_jsonl

TYPED_RETENTION = "30d"

STOP = set("""about after again also and any are been before being but can could did does doing done for from had has have
how into its just last like made make more most much not now off once one only other our out over own same should some such
than that the their them then there these they this those through too under until very was were what when where which while
who why will with would your you yours mine week day days thing something things gonna kind sort""".split())


def day_ref(d: dt.date, ask_day: dt.date) -> str:
    """How a person names a recent workday on the ask day."""
    delta = (ask_day - d).days
    if delta == 1:
        return "yesterday"
    monday = ask_day - dt.timedelta(days=ask_day.weekday())
    name = d.strftime("%A")
    return name if d >= monday else ("last " + name if delta < 7 else d.strftime("%A, %B %-d"))


def chat_when(chat: dict, dates: list[dt.date]) -> dt.datetime:
    """Chats with day >= 1 happen on that workday at `time`; day <= 0 are calendar days before workday 1."""
    if chat["day"] >= 1:
        return local_dt(dates[chat["day"] - 1], chat.get("time", "12:00"))
    d = dates[0] + dt.timedelta(days=chat["day"])
    h = int(hashlib.sha256(chat["id"].encode()).hexdigest(), 16)
    return local_dt(d, 8 * 3600 + h % (13 * 3600))


def ago(t: dt.datetime, ask_day: dt.date) -> str:
    n = (ask_day - t.date()).days
    return "today" if n == 0 else ("1 day ago" if n == 1 else f"{n} days ago")


def render_memory(world: dict, mem: dict, dates: list[dt.date], ask_day: dt.date) -> dict:
    chats = {c["id"]: c for c in world["chats"]}
    when = {cid: chat_when(c, dates) for cid, c in chats.items()}
    # ChatGPT style: saved memories ("Memory") + recent chat history (the user's messages, newest first).
    lines = ["# What you remember about this user from past ChatGPT conversations",
             "", "## Saved memories"]
    for m in mem["saved_memories"]:
        lines.append(f"- {m['text']} (saved {when[m['from']].strftime('%b %-d, %Y')})")
    lines += ["", "## Recent conversation history (most recent first; the user's own messages)"]
    for cid in sorted(chats, key=lambda c: when[c], reverse=True):
        c, t = chats[cid], when[cid]
        lines.append(f"- {ago(t, ask_day)} ({t.strftime('%a %b %-d, %-I:%M %p')}), \"{c['title']}\": {c['user']}")
    chatgpt = "\n".join(lines)
    # Claude style: the synthesized memory summary Claude keeps (work / personal / top of mind / brief history).
    lines = ["# What you remember about this user from past Claude conversations (memory summary)", ""]
    for k, v in mem["claude_summary"].items():
        lines += [f"**{k}**", v, ""]
    claude = "\n".join(lines).strip()
    return {"codex": chatgpt, "claude": claude,
            "_note": "Approximation of each app's native memory (ChatGPT saved memories + chat history; Claude memory "
                     "summary), derived from the persona's past chats and assumed fully caught up. See README."}


class Ctx:
    def __init__(self, ledger: dict, ask_day: dt.date):
        self.l = ledger
        self.ask = ask_day
        self.dates = [dt.date.fromisoformat(d) for d in ledger["dates"]]

    def at(self, step_id: str) -> str:
        typed = [t for t in self.l["typed"] if t["step"] == step_id]
        if typed:
            return fmt_clock(dt.datetime.fromisoformat(typed[0]["at"]))
        segs = [s for s in self.l["segments"] if s["step"] == step_id]
        if not segs:
            raise KeyError(f"no step {step_id} in the ledger")
        return fmt_clock(dt.datetime.fromisoformat(segs[0]["start"]))

    def render(self, s: str) -> str:
        def sub(m):
            key = m.group(1)
            if re.fullmatch(r"[dD]\d+", key):
                r = day_ref(self.dates[int(key[1:]) - 1], self.ask)
                return r[0].upper() + r[1:] if key[0] == "D" else r
            if key.startswith("date:d"):
                return fmt_day(self.dates[int(key[6:]) - 1])
            if key.startswith("at:"):
                return self.at(key[3:])
            if key.startswith("min:"):
                _, kind, day, name = key.split(":")
                v = self.l["metrics"]["by_" + kind].get(day, {}).get(name, 0)
                return str(int(round(v)))
            raise KeyError(key)
        return re.sub(r"\{([A-Za-z]+(?::[^{}]+)?|[dD]\d+)\}", sub, s) if isinstance(s, str) else s


def row_text(r: dict) -> str:
    return "\n".join(x for x in (r.get("title"), r.get("text"), r.get("page")) if x)


def row_day(r: dict, dates: list[dt.date]) -> int:
    d = dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00")).astimezone(TZ).date()
    return dates.index(d) + 1 if d in dates else 0


def words(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z][a-z'-]{3,}", s.lower()):
        w = w.strip("'-").removesuffix("'s")
        if w.endswith("s") and len(w) > 4:
            w = w[:-1]
        if w not in STOP:
            out.add(w)
    return out


def build_questions(qspec: dict, ctx: Ctx, rows: list[dict], blocks: dict, persona: str,
                    annotations: dict | None = None, truth: list[str] | None = None,
                    chats: list[str] | None = None) -> tuple[list, list, list]:
    """A fact's evidence regex must match the ground truth (`truth`: everything that happened, typed and seen; `chats`:
    the past chats). Matching the truth but not DayDream's rows (typed in Slack desktop, a refused page...) or not the
    memory blocks is a coverage gap, part of the result: a warning, and in_daydream / in_native say so."""
    annotations = annotations or {}
    truth, chats = truth or [], chats or []
    errors, warnings, out = [], [], []
    corpus = [row_text(r) for r in rows]
    for it in qspec["items"]:
        q = ctx.render(it["question"])
        item = {"id": f"{persona}-{it['id']}", "persona": persona, "type": it["type"], "question": q,
                "reference": ctx.render(it["reference"]), "negative": bool(it.get("negative")),
                "hallucination": ctx.render(it.get("hallucination")), "need": ctx.render(it.get("need")),
                "can_know": ctx.render((annotations.get(it["id"]) or {}).get("can_know")), "facts": []}
        for f in it["facts"]:
            ev, nm = f.get("ev"), f.get("nm")
            # Where a fact should be found follows from which evidence regex it gives (week rows: ev, past chats: nm);
            # a given regex that matches nothing is an error. (The generator's own "expect" labels were unreliable.)
            expect = ({"daydream"} if ev else set()) | ({"native"} if nm else set())
            in_dd = bool(ev) and any(re.search(ev, c, re.I | re.M) for c in corpus)
            in_nat = {k: bool(nm) and bool(re.search(nm, b, re.I)) for k, b in blocks.items() if not k.startswith("_")}
            if "daydream" in expect and not in_dd:
                if any(re.search(ev, c, re.I | re.M) for c in truth):
                    warnings.append(f"{it['id']}/{f['id']}: happened but DayDream didn't record it ({ev!r}): coverage gap")
                else:
                    errors.append(f"{it['id']}/{f['id']}: no row and nothing in the week matches {ev!r}")
            if "native" in expect and not all(in_nat.values()):
                if any(re.search(nm, c, re.I) for c in chats):
                    warnings.append(f"{it['id']}/{f['id']}: in the chats but not in memory {[k for k, v in in_nat.items() if not v]} ({nm!r})")
                else:
                    errors.append(f"{it['id']}/{f['id']}: nothing in the past chats or memory matches {nm!r}")
            chk = f.get("check")
            if chk:
                chk = {k: ctx.render(v) for k, v in chk.items()}
                if chk["kind"] == "number":
                    chk["value"] = float(chk["value"])
                if chk["kind"] == "regex" and "native" not in expect and not item["negative"]:
                    leak = [k for k, b in blocks.items() if not k.startswith("_") and re.search(chk["pattern"], b, re.I)]
                    if leak:
                        warnings.append(f"{it['id']}/{f['id']}: answer pattern also appears in native memory {leak} (possible lucky guess)")
            item["facts"].append({"id": f["id"], "text": ctx.render(f["text"]), "check": chk,
                                  "optional": bool(f.get("optional")), "in_daydream": in_dd,
                                  "in_native": {k: v for k, v in in_nat.items()}})
        if it["type"] == "vague":
            overlap = words(q) & words(it["target_ev"]) - set(it.get("allow_overlap", []))
            if overlap:
                errors.append(f"{it['id']}: vague question reuses words from its target: {sorted(overlap)}")
        for a in it.get("absent", []):
            hits = [r for r in rows if r["kind"] == a.get("kind", r["kind"]) and (not a.get("day") or row_day(r, ctx.dates) == a["day"])
                    and a.get("url_contains", "") in (r.get("url") or "")]
            if hits:
                errors.append(f"{it['id']}: negative's absent-condition violated by {len(hits)} rows ({hits[0]['id']})")
        item["answerable_from"] = {"daydream": any(f["in_daydream"] for f in item["facts"] if not f["optional"]),
                                   "native": any(all(f["in_native"].values()) and f["in_native"] for f in item["facts"] if not f["optional"])}
        req = [f for f in item["facts"] if not f["optional"]]
        if it["type"] == "control" and not all(any(f["in_native"].values()) for f in req):
            errors.append(f"{it['id']}: a control's required facts must all be in the memory text (saved memories or the "
                          "user's own messages); these aren't: " + ", ".join(f["id"] for f in req if not any(f["in_native"].values())))
        elif it["type"] == "control" and not all(all(f["in_native"].values()) for f in req):
            warnings.append(f"{it['id']}: control answerable from only one app's memory: "
                            + str({f["id"]: f["in_native"] for f in req}))
        elif it["type"] not in ("negative", "time_spent", "control") and not it.get("negative") \
                and not (item["answerable_from"]["daydream"] or item["answerable_from"]["native"]):
            errors.append(f"{it['id']}: nothing recorded or remembered answers it (its facts were typed where DayDream "
                          "doesn't record and aren't in the chats); pick facts visible in window/page titles, searches or "
                          "typed words in recorded apps, or in the past chats")
        if it["type"] == "time_spent" and "{min:" not in (it.get("reference") or ""):
            errors.append(f"{it['id']}: the time_spent reference must use the same {{min:...}} placeholder as the check")
        out.append(item)
    return out, errors, warnings


def build(persona: str, source: str, ask_day: dt.date, seed: int) -> dict:
    pdir = PERSONAS / persona
    sdir = pdir / source
    world = read_json(sdir / "world.json")
    mem = read_json(sdir / "memory.json")
    qspec = read_json(sdir / "questions.json")
    write_json(pdir / "world.json", world)
    led = simmod.simulate(persona, ask_day, seed)
    # Typed-words retention 30 days (a user setting; default 7): with a rolling 7-day cutoff the first workday's typed
    # words would expire during the ask day. README "Typed-words retention".
    report = store.load(persona, pdir / "events.jsonl", typed_retention=TYPED_RETENTION)
    # Rows DayDream's own ingest refuses (its privacy policy: e.g. a payment dashboard, a title with an address) are
    # not in the store, as in real use; they are listed in build-check.json and left out of the evidence corpus.
    refused = set(report.get("refusedIDs") or [])
    rows = [json.loads(line) for line in (pdir / "events.jsonl").read_text().splitlines() if line.strip()]
    refused_rows = [{"id": r["id"], "kind": r["kind"], "title": r.get("title"), "url": r.get("url")} for r in rows if r["id"] in refused]
    rows = [r for r in rows if r["id"] not in refused]
    dates = [dt.date.fromisoformat(d) for d in led["dates"]]
    blocks = render_memory(world, mem, dates, ask_day)
    ctx = Ctx(led, ask_day)
    ann_path = sdir / "annotations.json"   # can_know per item (bench.annotate), reviewed before the freeze
    annotations = read_json(ann_path)["items"] if ann_path.exists() else {}
    truth = [t.get("text") or "" for t in led["typed"]] + [str(g.get("title") or "") for g in led["segments"]] \
        + [str(g.get("what") or "") for g in led["segments"]]
    chat_text = [f"{c.get('title', '')}\n{c.get('user', '')}\n{c.get('gist', '')}" for c in world["chats"]]
    items, errors, warnings = build_questions(qspec, ctx, rows, blocks, persona, annotations, truth, chat_text)
    chats = [{**c, "at": chat_when(c, dates).isoformat()} for c in world["chats"]]
    ledger = {"persona": persona, "source": source, "provenance": world.get("_provenance"), "ask_day": ask_day.isoformat(),
              "dates": led["dates"], "day_names": {str(i + 1): day_ref(d, ask_day) for i, d in enumerate(dates)},
              "people": world.get("people"), "threads": world.get("threads"), "chats": chats,
              "typed": led["typed"], "segments": led["segments"], "metrics": led["metrics"],
              "rows": len(rows), "store": {"kept": report["kept"], "store_sha256": report["store_sha256"]},
              "facts": {i["id"]: i["facts"] for i in items}}
    write_json(pdir / "ledger.json", ledger)
    (pdir / "ledger-sim.json").unlink(missing_ok=True)
    write_json(pdir / "persona.json", {"id": persona, **world["persona"], "source": source})
    write_json(pdir / "native_memory.json", {"blocks": {k: v for k, v in blocks.items() if not k.startswith("_")},
                                            "note": blocks["_note"]})
    write_jsonl(pdir / "questions.jsonl", items)
    anchor = {"persona": persona, "ask_day": ask_day.isoformat(), "built_at": dt.datetime.now(TZ).isoformat(),
              "ask_at": dt.datetime.now(TZ).isoformat(), "dates": led["dates"], "seed": seed, "source": source,
              "generator": world.get("_generator") or ("fixture (hand-authored, plumbing only)" if source == "fixture" else None),
              "store_sha256": report["store_sha256"], "rows": len(rows), "kept": report["kept"], "typed_retention": TYPED_RETENTION}
    write_json(pdir / "anchor.json", anchor)
    write_json(pdir / "build-check.json", {"errors": errors, "warnings": warnings, "refused_by_daydream": refused_rows})
    return {"anchor": anchor, "errors": errors, "warnings": warnings, "items": len(items)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--source", default="fixture", choices=["fixture", "generated"])
    ap.add_argument("--ask-day", default=dt.datetime.now(TZ).date().isoformat())
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    res = build(a.persona, a.source, dt.date.fromisoformat(a.ask_day), a.seed)
    print(json.dumps({k: res["anchor"][k] for k in ("ask_day", "dates", "rows", "kept", "store_sha256")}, indent=1))
    for w in res["warnings"]:
        print("WARN ", w)
    for e in res["errors"]:
        print("ERROR", e)
    print(f"{res['items']} questions; {len(res['errors'])} errors, {len(res['warnings'])} warnings")
    sys.exit(1 if res["errors"] else 0)
