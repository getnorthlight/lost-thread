"""Neutral persona generation with DeepSeek (V4.x Flash), step by step, every call cached and validated.

    python3 -m bench.generate --persona student-mara --archetype student --days 5
    python3 -m bench.build --persona student-mara --source generated

Writes personas/<p>/generated/{world,memory,questions}.json in the same shape as the hand-authored pilot fixture, so
`bench.build` treats both the same. The generator never sees DayDream's capture rules: it plans a realistic week in
ordinary apps and sites; the deterministic simulator (bench/simulate.py) decides what DayDream 0.1.6 would have recorded,
and `bench.build` checks every question's facts against the stored rows and the memory blocks.

Steps (each one a cached call in personas/<p>/llm-cache/):
  1 persona     person, job, invented people, 4-5 threads that run across the week, apps and sites they use, noise
  2 plan-dN     one workday as time blocks of steps (app / page / search / type / idle), typed text left as an intent
  3 fill-dN     exact window and page titles and the typed words for that day's steps
  4 chats       15-30 past chats with ChatGPT/Claude before the week (the in-week ones come from the plan's AI prompts)
  5 memory      what the apps' native memory would hold, from the chats ONLY (saved memories + a Claude-style summary)
  6 questions   9 items over 8 types (each with the person's need), with reference answers and key facts; retried until bench.build's checks pass
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys

from .common import PERSONAS, TZ, read_json, workdays_before, write_json
from .llm import Generator, GeneratorUnavailable
from .simulate import APPS

STEP_KINDS = {"app", "page", "search", "type", "idle"}
QUESTION_TYPES = {
    "exact_recall": "a specific identifier, number, name or phrase from the week (a ticket/PR/invoice number, a price, a title)",
    "where_left_off": "where the person left one ongoing thread at the end of a day (last file/doc/command/page and state)",
    "cross_app": "something only answerable by combining two places (what they told someone by email after researching it elsewhere)",
    "time_spent": "roughly how long they spent on an app, site, meeting type or thread on one day",
    "decision": "how a decision or number changed across days and where it landed",
    "vague": "a fuzzy description of something they saw or did, phrased as a person would, sharing NO content words with its title",
    "negative": "something that did NOT happen (an unsent reply, a meeting that never took place); the right answer says so",
    "control": "something about the person's life that their chat history with the assistant answers on its own",
}
# Lost Thread (design decision, 2026-10-06): 3 personas x 9 items = 27, 8 types. One of each type per persona plus one
# extra, rotated, so the totals are exact_recall 4, where_left_off 3, cross_app 3, time_spent 3, decision 3, vague 4,
# negative 4, control 3 (the memory-only controls).
LOST_THREAD_EXTRAS = ["exact_recall", "vague", "negative"]


def quota_for(index: int) -> dict:
    q = {t: 1 for t in QUESTION_TYPES}
    q[LOST_THREAD_EXTRAS[index % 3]] += 1
    return q


SYSTEM = ("You generate realistic synthetic test data for a research benchmark about personal memory. Everything you "
          "write is fiction: invent every person, company, client and product name (never use real people you know of, "
          "and avoid famous companies as employers). Write like real life, not like a tidy diary: people get "
          "interrupted, leave things half done, revisit things, procrastinate, and type casually. Reply with JSON only.")


def app_catalog() -> str:
    return ", ".join(f"{k} ({v[0]})" for k, v in APPS.items() if k != "chrome")


# ---------------------------------------------------------------------------------------------------------------------
# validators


def v_persona(o: dict) -> list[str]:
    p = []
    for k in ("persona", "people", "threads", "noise"):
        if k not in o:
            p.append(f"missing {k}")
    if p:
        return p
    if not (5 <= len(o["people"]) <= 9):
        p.append("people: 5-9 invented people")
    th = o["threads"]
    if not (4 <= len(th) <= 6):
        p.append("threads: 4-6")
    kinds = {t.get("kind") for t in th}
    for need in ("decision", "correspondence", "personal"):
        if need not in kinds:
            p.append(f"threads need one of kind '{need}'")
    for t in th:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,23}", t.get("key", "")):
            p.append(f"thread key {t.get('key')!r} must be short snake_case")
    for a in o.get("apps", []):
        if a not in APPS:
            p.append(f"unknown app key {a!r}; use only: {', '.join(APPS)}")
    if len(o["noise"].get("pages", [])) < 4:
        p.append("noise.pages: at least 4 distraction pages with host and title")
    return p


WEB_ALIASES = {"browser": "www.google.com", "chrome": "www.google.com", "web": "www.google.com", "google": "www.google.com",
               "gmail": "mail.google.com", "google_docs": "docs.google.com", "gdocs": "docs.google.com",
               "google_calendar": "calendar.google.com", "youtube": "www.youtube.com", "linkedin": "www.linkedin.com"}


def _host(name: str) -> str:
    n = name.strip().lower().removeprefix("web:")
    if n in WEB_ALIASES:
        return WEB_ALIASES[n]
    return n if "." in n else n.replace("_", "") + ".com"


def normalize_plan(o) -> dict:
    """In place where possible: DeepSeek sometimes returns the blocks as a bare list, names a website as an app
    ("github", "docs.google.com", "browser"), or types into a bare host. Websites become page steps in Chrome."""
    if isinstance(o, list):
        o = {"blocks": o}
    for b in o.get("blocks") or []:
        for st in b.get("steps") or []:
            if not isinstance(st, dict):
                continue
            if st.get("do") == "app" and st.get("app") not in APPS:
                st["do"], st["host"] = "page", _host(str(st.pop("app", "browser")))
            if st.get("do") == "type":
                w = str(st.get("where", ""))
                if w not in APPS and not w.startswith("web:") and w:
                    st["where"] = "web:" + _host(w)
                elif w == "chrome":
                    st["where"] = "web:www.google.com"
    return o


def v_plan(o: dict, threads: set[str]) -> list[str]:
    p = []
    if not isinstance(o, dict):
        return ["reply must be a JSON object {\"blocks\": [...], \"summary\": ...}"]
    normalize_plan(o)
    blocks = o.get("blocks") or []
    if len(blocks) < 9:
        p.append("a workday needs at least 9 blocks (real days are fragmented)")
    last = "00:00"
    used = set()
    for bi, b in enumerate(blocks):
        if not re.fullmatch(r"\d\d:\d\d", b.get("start", "")):
            p.append(f"block {bi}: start must be HH:MM")
            continue
        if b["start"] < last:
            p.append(f"block {bi}: starts before the previous block")
        last = b["start"]
        if b.get("thread") not in threads:
            p.append(f"block {bi}: thread must be one of {sorted(threads)}")
        used.add(b.get("thread"))
        for si, s in enumerate(b.get("steps") or []):
            k = s.get("do")
            if k not in STEP_KINDS:
                p.append(f"block {bi} step {si}: do must be one of {sorted(STEP_KINDS)}")
            if k == "app" and s.get("app") not in APPS:
                p.append(f"block {bi} step {si}: app {s.get('app')!r} not in the catalog")
            if k == "app" and s.get("app") == "chrome":
                p.append(f"block {bi} step {si}: use do=page for websites, not app=chrome")
            if k == "page" and not s.get("host"):
                p.append(f"block {bi} step {si}: page needs host")
            if k == "type":
                w = s.get("where", "")
                if not (w.startswith("web:") or w in APPS):
                    p.append(f"block {bi} step {si}: where must be an app key or web:<host>")
                if not s.get("intent"):
                    p.append(f"block {bi} step {si}: type needs an intent (what they write and to whom)")
            if k in ("app", "page", "idle") and not isinstance(s.get("min"), (int, float)):
                p.append(f"block {bi} step {si}: min (minutes) is required")
    if len(used) < 3:
        p.append("a day should touch at least 3 threads")
    return p


def normalize_fill(o: dict, plan: dict) -> None:
    """In place: DeepSeek sometimes numbers blocks/steps from 1 (b1s1) and nests a step under its kind
    ({"app": {"title": ...}}). Shift and unwrap so the keys match the plan's 0-based b<block>s<step>."""
    steps = o.get("steps")
    need = [(bi, si) for bi, b in enumerate(plan["blocks"]) for si, s in enumerate(b["steps"])
            if s["do"] in ("app", "page", "type", "search")]
    if steps is None and o and all(re.fullmatch(r"\w*?b\d+_?s\d+", k) for k in o):   # keys at the top level
        steps = dict(o)
    if isinstance(steps, list) and len(steps) == len(need):
        steps = {f"b{bi}s{si}": v for (bi, si), v in zip(need, steps)}
    if not isinstance(steps, dict):
        return
    fixed = {}
    for k, v in steps.items():   # "d3b0s0", "b0_s0", "block0step0", "B0S0" -> "b0s0"
        m = re.search(r"b(?:lock)?_?(\d+)\D{0,6}?s(?:tep)?_?(\d+)$", k, re.I)
        fixed[f"b{m.group(1)}s{m.group(2)}" if m else k] = v
    steps = fixed
    zero = sum(f"b{bi}s{si}" in steps for bi, si in need)
    one = sum(f"b{bi + 1}s{si + 1}" in steps for bi, si in need)
    if one > zero:
        steps = {f"b{bi}s{si}": steps[f"b{bi + 1}s{si + 1}"] for bi, si in need if f"b{bi + 1}s{si + 1}" in steps}
    for k, v in list(steps.items()):
        if isinstance(v, dict) and len(v) == 1 and next(iter(v)) in ("app", "page", "type", "search") and isinstance(next(iter(v.values())), dict):
            steps[k] = next(iter(v.values()))
    o["steps"] = steps


def fill_keys(plan: dict) -> str:
    out = []
    for bi, b in enumerate(plan["blocks"]):
        for si, s in enumerate(b["steps"]):
            d = s["do"]
            if d not in ("app", "page", "type", "search"):
                continue
            where = {"app": s.get("app"), "page": s.get("host"), "type": "in " + str(s.get("where")), "search": "google"}[d]
            out.append(f"b{bi}s{si}: {d} {where}: {s.get('about') or s.get('intent') or ''}")
    return "\n".join(out)


def v_fill(o: dict, plan: dict) -> list[str]:
    p = []
    normalize_fill(o, plan)
    steps = o.get("steps") or {}
    for bi, b in enumerate(plan["blocks"]):
        for si, s in enumerate(b["steps"]):
            sid = f"b{bi}s{si}"
            f = steps.get(sid)
            if s["do"] in ("app", "page", "type", "search") and not f:
                p.append(f"missing steps.{sid}")
                continue
            if s["do"] == "type" and not (f or {}).get("text"):
                p.append(f"steps.{sid}: text is required")
            if s["do"] in ("app", "page") and not (f or {}).get("title"):
                p.append(f"steps.{sid}: title is required")
            if s["do"] == "search" and not ((f or {}).get("query") or (f or {}).get("text") or (f or {}).get("q")):
                p.append(f"steps.{sid}: query is required")
            if s["do"] == "page" and (f or {}).get("title") and s.get("host") == "docs.google.com" and not f["title"].endswith(" - Google Docs"):
                p.append(f"steps.{sid}: Google Docs titles end with ' - Google Docs'")
            if s["do"] == "page" and (f or {}).get("title") and s.get("host", "").endswith("youtube.com") and not f["title"].endswith(" - YouTube"):
                p.append(f"steps.{sid}: YouTube titles end with ' - YouTube'")
    return p


def v_chats(o: dict) -> list[str]:
    cs = o.get("chats") or []
    p = [] if 10 <= len(cs) <= 26 else ["10-26 older chats (the in-week ones are added from the plan)"]
    for c in cs:
        if not (-75 <= int(c.get("day", 0)) <= -1):
            p.append(f"chat {c.get('id')}: day must be -75..-1 (days before the week)")
        for k in ("id", "title", "user", "gist"):
            if not c.get(k):
                p.append(f"chat {c.get('id')}: missing {k}")
    return p


def v_memory(o: dict) -> list[str]:
    p = []
    if not (4 <= len(o.get("saved_memories") or []) <= 12):
        p.append("saved_memories: 4-12")
    cs = o.get("claude_summary") or {}
    for k in ("Work context", "Personal context", "Top of mind", "Brief history"):
        if not cs.get(k):
            p.append(f"claude_summary.{k} missing")
    return p


def v_questions(o: dict, quota: dict) -> list[str]:
    p = []
    items = o.get("items") or []
    got = {}
    for it in items:
        got[it.get("type")] = got.get(it.get("type"), 0) + 1
        for k in ("id", "type", "question", "reference", "facts", "need"):
            if k not in it:
                p.append(f"{it.get('id')}: missing {k}")
        if it.get("type") == "vague" and not it.get("target_ev"):
            p.append(f"{it.get('id')}: vague items need target_ev (the exact title/words of the thing)")
        if it.get("type") == "negative":
            it["negative"] = True   # the type says it; the flag is often forgotten
        for f in it.get("facts", []):
            for rx in (f.get("ev"), f.get("nm"), (f.get("check") or {}).get("pattern")):
                if rx:
                    try:
                        re.compile(rx)
                    except re.error as e:
                        p.append(f"{it.get('id')}/{f.get('id')}: bad regex {rx!r}: {e}")
    for it in items:
        for f in it.get("facts", []):
            if not it.get("negative") and re.search(r"\b(never|not|didn't|did not|wasn't) (sent|send|delivered)\b|\bunsent\b|\bwas sent\b|\bdelivered\b",
                         f.get("text", ""), re.I):
                p.append(f"{it.get('id')}/{f.get('id')}: a fact can't depend on whether a message was sent or delivered")
            timed = "{min:" in str((f.get("check") or {}).get("value", ""))   # truth from the ledger's minutes
            if not it.get("negative") and not f.get("optional") and not timed and not (f.get("ev") or f.get("nm")):
                p.append(f"{it.get('id')}/{f.get('id')}: give ev and/or nm")
        if it.get("type") == "time_spent":
            if not any("{min:" in str((f.get("check") or {}).get("value", "")) for f in it.get("facts", [])):
                p.append(f"{it.get('id')}: time_spent needs a number check whose value is a {{min:app:<day>:<app>}} placeholder")
    for t, n in quota.items():
        if got.get(t, 0) != n:
            p.append(f"need exactly {n} items of type {t}, got {got.get(t, 0)}")
    return p


# ---------------------------------------------------------------------------------------------------------------------
# steps


def gen_persona(g: Generator, archetype: str, seed_hint: str) -> dict:
    user = f"""Invent one person for a 5-workday slice of their life. Archetype: {archetype}. Variety seed: {seed_hint}.
They live in the US Central time zone and use a Mac.

Return JSON:
{{"persona": {{"name", "role", "company" (invented), "summary", "workday": {{"start": "HH:MM", "end": "HH:MM", "lunch": "HH:MM"}}}},
 "people": [{{"key", "name" (invented, first+last unless family), "role", "channel"}}],   // 5-9
 "threads": [{{"key" (snake_case), "title", "kind": one of "main"|"decision"|"correspondence"|"side_project"|"personal",
              "arc": "how it develops over the 5 days, with concrete numbers/identifiers that change"}}],  // 4-6
 "apps": [app keys they use, from: {app_catalog()}],
 "sites": [hosts they use for work and life, e.g. "docs.google.com", "mail.google.com", "github.com", "canvas.instructure.com"],
 "noise": {{"slack_titles": [window titles if they use Slack], "pages": [{{"host", "title", "path"}}]}}   // distractions
}}
Threads must include a decision whose number or choice changes across days, a correspondence with someone outside
their team over email, and something personal. Include at least one concrete identifier (ticket, PR, order, invoice,
room, course code). Use Google Chrome for all websites."""
    return g.json("persona", SYSTEM, user, validate=v_persona, temperature=0.9)


def gen_plan(g: Generator, persona: dict, day_n: int, date: dt.date, prior: list[str]) -> dict:
    threads = {t["key"] for t in persona["threads"]}
    user = f"""Person and threads:
{json.dumps(persona, ensure_ascii=False)}

Plan workday {day_n} ({date.strftime('%A')}) as JSON {{"blocks": [...], "summary": "2-3 sentences of what happened"}}.
What happened on earlier days: {json.dumps(prior, ensure_ascii=False)}

Each block: {{"start": "HH:MM", "thread": one of {sorted(threads)}, "noise": true|false, "steps": [...]}}.
Steps (in order):
- {{"do": "app", "app": <key from {', '.join(k for k in APPS if k != 'chrome')}>, "about": "what window/document", "min": minutes}}
- {{"do": "page", "host": "<site>", "about": "what page", "min": minutes, "new_tab": optional bool}}
- {{"do": "search", "about": "what they googled"}}
- {{"do": "type", "where": <app key or "web:<host>">, "intent": "what they write, to whom, and whether they send it",
   "sent": true|false, "to": "<person name if a message/email>"}}
- {{"do": "idle", "min": minutes, "about": "lunch / walk / away"}}
Make it a real day: 9-16 blocks, threads interleaved, meetings (zoom/calendar), messages that interrupt, distraction
browsing, something left unfinished, something read but not answered, an abandoned tab. Keep each thread's arc moving.
Typing happens wherever it really would: Slack, email in the browser, Google Docs, notes, terminals, AI chat apps."""
    return g.json(f"plan-d{day_n}", SYSTEM, user, validate=lambda o: v_plan(o, threads), temperature=0.8)


def gen_fill(g: Generator, persona: dict, day_n: int, plan: dict) -> dict:
    user = f"""Person: {json.dumps(persona['persona'], ensure_ascii=False)}
People: {json.dumps(persona['people'], ensure_ascii=False)}
Day {day_n} plan: {json.dumps(plan, ensure_ascii=False)}

Fill in exact content as JSON {{"steps": {{"b<block>s<step>": {{...}}}}}} for every app/page/search/type step:
- app: {{"title": exact macOS window title, e.g. "budget_v3.xlsx", "retry_worker.go — fern-api", "#design (Channel) - Acme - Slack",
         "titles": optional list of window titles they rotate through}}
- page: {{"title": the tab title exactly as the site shows it ("... - Google Docs", "... - YouTube", Gmail: the email subject,
         "Inbox" for the inbox), "path": a plausible URL path}}
- search: {{"query": the words typed into Google}}
- type: {{"text": the exact words typed (casual where casual, typos allowed in chats), "title": window title for apps,
         "place": the page/subject it was typed in for web:<host>, "field": "message"|"body"|"textArea"|"oneLine",
         "seal": "submit" (Return/send) | "idle" (stopped typing) | "submitChord" (Cmd-Return in email)}}
Keep identifiers and numbers consistent with the thread arcs and earlier days.
Return exactly these keys (block and step numbers start at 0), each a flat object with the fields above:
{fill_keys(plan)}"""
    return g.json(f"fill-d{day_n}", SYSTEM, user, validate=lambda o: v_fill(o, plan), temperature=0.7, max_tokens=12000)


def merge_day(plan: dict, fill: dict) -> dict:
    blocks = []
    for bi, b in enumerate(plan["blocks"]):
        steps = []
        for si, s in enumerate(b["steps"]):
            f = fill["steps"].get(f"b{bi}s{si}", {})
            st = {k: v for k, v in s.items() if k not in ("about", "intent")}
            st["what"] = s.get("about") or s.get("intent") or ""
            st.update({k: v for k, v in f.items() if v is not None})
            if s["do"] == "search" and not st.get("query"):   # some replies put the search words under text/q
                st["query"] = f.get("text") or f.get("q")
            if s["do"] == "type" and st.get("where") in APPS and not st.get("title"):
                st["title"] = APPS[st["where"]][0]
            if s["do"] == "type" and s.get("where", "") in ("messages",) and s.get("to"):
                st.setdefault("title", s["to"])
                st["typo"] = True
            steps.append(st)
        blocks.append({**{k: v for k, v in b.items() if k != "steps"}, "steps": steps})
    return {"blocks": blocks}


def in_week_chats(days: list[dict]) -> list[dict]:
    """Typed prompts to ChatGPT/Claude during the week become chats in the contestant's app (README: chats are assumed
    to be in the contestant's own app)."""
    out, n = [], 0
    for di, d in enumerate(days, 1):
        for b in d["blocks"]:
            for s in b["steps"]:
                w = s.get("where", "")
                if s.get("do") == "type" and (w in ("chatgpt", "claude") or w in ("web:chatgpt.com", "web:claude.ai")):
                    n += 1
                    out.append({"id": f"w{n:02d}", "day": di, "time": b["start"], "title": s.get("what", "")[:60] or "Chat",
                                "user": s["text"], "gist": ""})
    return out


def gen_chats(g: Generator, persona: dict) -> dict:
    user = f"""Person: {json.dumps(persona, ensure_ascii=False)}
Write 12-22 short past chats this person had with an AI assistant in the 75 days BEFORE the week, as JSON
{{"chats": [{{"id": "c01", "day": -60 (days before the week, -75..-1), "title", "user": their first message, "gist": one
line of what the assistant answered}}]}}. Mix work, the side project and personal life; mention names of a few people
and plans the way people do; some chats should touch the threads' background (not the week's outcomes)."""
    return g.json("chats", SYSTEM, user, validate=v_chats, temperature=0.8)


def gen_memory(g: Generator, chats: list[dict]) -> dict:
    user = f"""Here are ALL of one user's past chats with an assistant (newest last):
{json.dumps(chats, ensure_ascii=False)}

From these chats ONLY, write what the assistant's native memory would plausibly hold, as JSON:
{{"saved_memories": [{{"from": chat id, "text": a short durable fact like "Is a ... at ..."}}],   // 4-12, like ChatGPT's saved memories
 "claude_summary": {{"Work context": ..., "Personal context": ..., "Top of mind": recent chats' themes, "Brief history": ...}}}}
Use nothing that is not in the chats. Don't invent outcomes the chats don't state."""
    return g.json("memory", SYSTEM, user, validate=v_memory, temperature=0.3)


def gen_questions(g: Generator, world: dict, quota: dict, feedback: str = "", memory_text: str = "") -> dict:
    types = "\n".join(f"- {t} x{n}: {QUESTION_TYPES[t]}" for t, n in quota.items())
    user = f"""This is everything that happened in one person's week (ground truth) and their past chats:
{json.dumps({k: world[k] for k in ('persona', 'people', 'threads', 'days', 'chats')}, ensure_ascii=False)}

This is ALL the assistant's memory holds about them (controls must be answered from this text alone; nm regexes for
controls must match it):
{memory_text}

Apps where typed words are NOT recorded (only the window title is): Slack desktop, VS Code, Zoom, Figma, Notion
desktop, Word, Outlook, Google Docs pages. Facts for non-control questions should be visible in window/page titles,
searches, or words typed in other apps and sites.

Write {sum(quota.values())} questions this person might ask their AI assistant on the next workday, as JSON
{{"items": [{{"id": "q01", "type", "question" (how they'd really ask it),
  "need": one line on what the person actually wants to accomplish by asking, e.g. "wants to know whether Marcus
          already has the results, so they don't double-send or forget",
  "reference" (the ideal answer),
  "facts": [{{"id", "text" (one key fact of the answer), "check": {{"kind": "regex", "pattern"}} or
             {{"kind": "number", "value", "unit", "full": 0.25, "half": 0.5}} or null,
             "ev": regex matching the exact words in the week's titles/typed text that prove it (or null),
             "nm": regex matching the past chats that prove it (or null), "optional": bool}}],
  "negative": bool, "hallucination": "what a made-up answer would claim" (negatives),
  "target_ev": the exact title/words of the thing (vague only), "allow_overlap": [] }}]}}
Types and counts:
{types}
Rules:
- Days: never write a weekday name, "today" or "yesterday" yourself. In questions use {{d1}}..{{d5}} (filled in as
  "yesterday", "last Thursday", ...); in references use {{date:d1}}..{{date:d5}} ("Thursday, October 1"). Clock times:
  {{at:<step id>}} (step ids are like d2b3s1).
- time_spent: the fact's check is {{"kind": "number", "value": "{{min:app:<day number>:<app key>}}", "unit": "minutes",
  "full": 0.25, "half": 0.5}} (or min:host:<day>:<host>), so the truth comes from the simulated week, and the
  reference uses the same placeholder.
- ev must be a regex that matches the exact words of a window/page title or typed text in the week; nm a regex that
  matches the past chats. Give at least one of them for every required fact (negatives: the facts can have neither).
- Key facts must be things a record of this person's Mac (titles, pages, searches, typed words, times) or their past
  chats can show. Never make a fact out of whether a message was actually sent, delivered or read, what someone
  else replied, or what happened off the computer.
- Negatives: something with NO trace at all in the week (nothing typed about it, no page or window about it), not a
  message that was typed but not sent.
- Controls must be answerable from the past chats alone. Vague questions must share no content word with target_ev. Negatives must be
about something plausible that did not happen in the ground truth, and nothing else in the week may answer or
half-answer them (an earlier email that already covered the point makes the negative ambiguous).{feedback}"""
    return g.json("questions" + ("-retry" if feedback else ""), SYSTEM, user,
                  validate=lambda o: v_questions(o, quota), temperature=0.5, max_tokens=12000)


def repair(persona_id: str, ask_day: dt.date, fixes: dict[str, str]) -> dict:
    """Pre-freeze only: DeepSeek rewrites the named items (same id and type) given why each is broken (a negative the
    ground truth contradicts, a reference on the wrong day...). Every repair is logged in generated/repairs.json."""
    pdir = PERSONAS / persona_id
    out = pdir / "generated"
    g = Generator(pdir)
    world, mem, qspec = read_json(out / "world.json"), read_json(out / "memory.json"), read_json(out / "questions.json")
    from .build import build, render_memory
    dates = workdays_before(ask_day, len(world["days"]))
    blocks = render_memory(world, mem, dates, ask_day)
    mem_text = "\n\n".join(v for k, v in blocks.items() if not k.startswith("_"))
    old = {it["id"]: it for it in qspec["items"]}
    quota = {}
    for k in fixes:
        quota[old[k]["type"]] = quota.get(old[k]["type"], 0) + 1
    why = "\n".join(f"- {k} ({old[k]['type']}): {fixes[k]}. The broken item was: {json.dumps(old[k], ensure_ascii=False)}" for k in fixes)
    feedback = (f"\n\nREPAIR: write ONLY replacements for these items, keeping their id and type, as "
                f"{{\"items\": [...]}}. Check every claim against the ground truth above:\n{why}")
    res = None
    for attempt in range(3):
        q = gen_questions(g, world, quota, feedback, mem_text)
        new = {it["id"]: it for it in q["items"]}
        if set(new) != set(fixes):
            feedback += f"\n- Return exactly the ids {sorted(fixes)}."
            continue
        merged = {**qspec, "items": [new.get(it["id"], it) for it in qspec["items"]]}
        write_json(out / "questions.json", merged)
        res = build(persona_id, "generated", ask_day, seed=11)
        bad = [e for e in res["errors"] if any(e.startswith(k + "/") or e.startswith(k + ":") for k in fixes)]
        if not res["errors"]:
            break
        write_json(out / "questions.json", qspec)
        feedback += "\n- A checker found: " + "; ".join(bad or res["errors"][:5])
    log = read_json(out / "repairs.json") if (out / "repairs.json").exists() else []
    log.append({"at": dt.datetime.now(TZ).isoformat(), "fixes": fixes, "before": {k: old[k] for k in fixes},
                "ok": bool(res and not res["errors"])})
    write_json(out / "repairs.json", log)
    return res or {"errors": ["repair failed"], "warnings": []}


def generate(persona_id: str, archetype: str, n_days: int, ask_day: dt.date, quota: dict) -> dict:
    pdir = PERSONAS / persona_id
    out = pdir / "generated"
    g = Generator(pdir)
    p = gen_persona(g, archetype, persona_id)
    dates = workdays_before(ask_day, n_days)
    # Plans are sequential (each sees the earlier days' summaries); each day's fill only needs its own plan, so fills
    # and the older chats run alongside the next plans.
    from concurrent.futures import ThreadPoolExecutor
    plans, prior, fills = [], [], {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        chats_f = pool.submit(gen_chats, g, p)
        for n, d in enumerate(dates, 1):
            plan = gen_plan(g, p, n, d, prior)
            plans.append(plan)
            fills[n] = pool.submit(gen_fill, g, p, n, plan)
            prior.append(f"Day {n}: {plan.get('summary', '')}")
        days = []
        for n, plan in enumerate(plans, 1):
            day = merge_day(plan, fills[n].result())
            for bi, b in enumerate(day["blocks"]):
                for si, s in enumerate(b["steps"]):
                    s.setdefault("id", f"d{n}b{bi}s{si}")
            days.append({"index": n, **day})
        older = chats_f.result()["chats"]
    chats = sorted(older, key=lambda c: c["day"]) + in_week_chats(days)
    mem = gen_memory(g, chats)
    world = {"_provenance": f"Generated by {g.label}; see llm-cache/ for every prompt and reply.", "_generator": g.label,
             "persona": {"key": persona_id, "archetype": archetype, **p["persona"]}, "people": p["people"],
             "threads": p["threads"], "noise": p["noise"], "days": days, "chats": chats}
    write_json(out / "world.json", world)
    write_json(out / "memory.json", mem)
    # Questions: retried with bench.build's own checks as feedback until they hold against the stored rows.
    from .build import build
    feedback = ""
    for attempt in range(5):
        from .build import render_memory
        blocks = render_memory(world, mem, dates, ask_day)
        mem_text = "\n\n".join(v for k, v in blocks.items() if not k.startswith("_"))
        q = gen_questions(g, world, quota, feedback, mem_text)
        write_json(out / "questions.json", q)
        res = build(persona_id, "generated", ask_day, seed=11)
        if not res["errors"]:
            break
        feedback = "\n\nA checker found these problems with your previous questions; fix them:\n- " + "\n- ".join(res["errors"][:15])
    return {"calls": g.calls, "errors": res["errors"], "warnings": res["warnings"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True, help="id, e.g. student-mara")
    ap.add_argument("--archetype", required=True, help="developer | student | founder | designer | sales")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--index", type=int, default=0, help="0, 1 or 2: which of the full set's question quotas to use")
    ap.add_argument("--ask-day", default=dt.datetime.now(TZ).date().isoformat())
    ap.add_argument("--repair", help='JSON {"q08": "why it is broken", ...}: rewrite those items (pre-freeze only)')
    a = ap.parse_args()
    if a.repair:
        r = repair(a.persona, dt.date.fromisoformat(a.ask_day), json.loads(a.repair))
        print(json.dumps({"errors": r["errors"], "warnings": r["warnings"]}, indent=1))
        sys.exit(1 if r["errors"] else 0)
    try:
        r = generate(a.persona, a.archetype, a.days, dt.date.fromisoformat(a.ask_day), quota_for(a.index))
    except GeneratorUnavailable as e:
        sys.exit(f"generator unavailable: {e}")
    print(json.dumps({"calls": len(r["calls"]), "errors": r["errors"], "warnings": r["warnings"]}, indent=1))
    sys.exit(1 if r["errors"] else 0)
