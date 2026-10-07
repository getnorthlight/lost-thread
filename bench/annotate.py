"""Draft `can_know` for every item: what DayDream 0.1.6 can and cannot know for that question (Lost Thread,
request 2026-10-06). Grading penalizes answers that claim more than `can_know` allows.

    python3 -m bench.annotate --persona developer      # writes personas/<p>/generated/annotations.json

DeepSeek drafts each line from CAPABILITIES (below, taken from the 0.1.6 code) and the item's ground truth; the harness
author then reviews every line against the ledger BEFORE the freeze, and edits are recorded in the file
(`reviewed`, `edited`). bench.build merges the lines into questions.jsonl; bench.freeze locks them.
"""
from __future__ import annotations

import argparse
import json
import sys

from .common import PERSONAS, read_json, read_jsonl, write_json
from .llm import Generator

# What an AI app can learn through DayDream 0.1.6's shipped MCP tools (agent-tools v2: timeline, search, details, status),
# from the release worktree (wt-claude-rel-016b @ 6852390):
# - recording: EventCapture writes rows on focus / title / click / typing events only, never for time spent in a window.
# - send state: PrivacyPolicy/SendRules.swift decides a *send gesture* at seal time ("detected" | "none" | "unknown",
#   never "sent"): Return in AI apps, chat apps (Slack, Discord, WhatsApp, Teams...) and their web hosts, terminals and
#   search boxes; Mail.app's own Send; Cmd-Return on social sites; Messages only when the composer empties after Return;
#   webmail (Gmail, Outlook web) only when the compose window closes. Notes, docs and editors are "none".
#   But the AI tools never show it: AgentSharePolicy.sendStateLine "Never shown: whether a message went out; DayDream
#   can't tell." and instructionsV2 "DayDream can't tell whether a message went out: never tell the person it was sent."
CAPABILITIES = """What an AI assistant can learn about this person through DayDream's tools (and nothing else):
CAN KNOW
- Which app and window/tab title was in front, with local times, at the moments the person switched, clicked or typed.
- Chrome pages: the tab title and site (Chrome only). Google searches typed in Chrome.
- Typed words and where they were typed: the app, the window or page title (for Gmail, the email subject), the Slack
  channel or person, the Messages conversation, a name typed in Mail's To field. Passwords and codes are removed.
  This includes questions typed to ChatGPT/Claude and commands typed in terminals during the week.
CANNOT KNOW
- Whether a message or email actually went out, was delivered or read. DayDream records only that words were typed in
  a composer; its tools never say sent/unsent/draft. So "did I send/reply X" can only be answered as "you typed a reply
  to X at <time>; DayDream can't tell whether it was sent" (or "no reply was typed on this Mac").
- What other people wrote (incoming emails, Slack messages, comments), beyond what a window or tab title shows.
- Page, document or file contents beyond titles and the person's own typing; what they read or how carefully.
- How long something was looked at: only the times of switches. Idle time, lunch, and calls or meetings with no
  typing leave gaps, so durations are estimates from the timeline (a lower or upper bound, not a measurement).
- Anything off this Mac (phone, in person, other computers), other browsers than Chrome, and the future.
- The person's older chats with the assistant before this week, unless they were typed on this Mac this week."""

SYSTEM = ("You annotate a benchmark about an AI assistant that can read a personal activity recorder. For each question, "
          "state precisely what the recorder can and cannot know for THAT question, using only the capability list and "
          "the question's ground truth. Be concrete and short. Reply with JSON only.")


def draft(persona: str) -> dict:
    pdir = PERSONAS / persona
    items = read_jsonl(pdir / "questions.jsonl")
    led = read_json(pdir / "ledger.json")
    typed = [{"day": t.get("day"), "at": (t.get("at") or "")[11:16], "where": t.get("where"), "place": t.get("place"),
              "to": t.get("to"), "text": (t.get("text") or "")[:160], "sent_in_truth": t.get("sent"),
              "visible_to_daydream": t.get("visible_to_daydream")} for t in led["typed"]]
    rows = [json.loads(x) for x in (pdir / "events.jsonl").read_text().splitlines() if x.strip()]
    refused = {r["id"] for r in read_json(pdir / "build-check.json").get("refused_by_daydream", [])}
    seen, recorded = set(), []
    for r in rows:
        if r["id"] in refused or not (r.get("title") or r.get("text")):
            continue
        k = (r["at"][:10], r["app"], r.get("title") or "", (r.get("text") or "")[:100])
        if k not in seen:
            seen.add(k)
            recorded.append(" | ".join(x for x in k if x))
    view = [{"id": i["id"], "type": i["type"], "question": i["question"], "need": i.get("need"),
             "reference": i["reference"], "negative": i["negative"],
             "facts": [{"text": f["text"], "in_daydream": f["in_daydream"], "in_native_memory": f["in_native"]}
                       for f in i["facts"]]} for i in items]
    user = f"""{CAPABILITIES}

The person's typed words this week (ground truth; sent_in_truth is what really happened, which DayDream can't see):
{json.dumps(typed, ensure_ascii=False)}

What DayDream actually recorded this week (date | app | window/page title | typed words), deduplicated (UTC dates):
{chr(10).join(recorded)}

Questions (with the ideal answer and which facts DayDream's rows hold; in_daydream is computed from the rows and is
authoritative):
{json.dumps(view, ensure_ascii=False)}

For EVERY question write "can_know": one or two sentences, "Can know: ... Cannot know: ...", naming the concrete things
from this question (e.g. "Can know: that a reply to Marcus was typed in Gmail on Thursday at 4:10 PM and its words.
Cannot know: whether it was sent, or whether Marcus replied."). For control questions (answered by past chats), say
what DayDream holds about it, usually nothing. Return JSON {{"items": {{"<id>": {{"can_know": "..."}}}}}}."""
    ids = {i["id"] for i in items}
    g = Generator(pdir)

    def v(o):
        got = set((o.get("items") or {}).keys())
        p = [f"missing {x}" for x in sorted(ids - got)] + [f"unknown id {x}" for x in sorted(got - ids)]
        for k, x in (o.get("items") or {}).items():
            c = (x or {}).get("can_know", "")
            if not (c.startswith("Can know") and "Cannot know" in c):
                p.append(f"{k}: can_know must read 'Can know: ... Cannot know: ...'")
        return p

    o = g.json("can-know", SYSTEM, user, validate=v, temperature=0.2, max_tokens=8000)
    # Items in the generated spec are keyed q01..; built ids are <persona>-q01.
    out = {"_provenance": f"Drafted by {g.label} from bench/annotate.py CAPABILITIES (0.1.6 code); reviewed by the "
                          "harness author against ledger.json before the freeze. edited=true marks hand edits.",
           "items": {k.removeprefix(persona + "-"): {"can_know": x["can_know"], "drafted": x["can_know"],
                                                      "reviewed": False, "edited": False}
                     for k, x in o["items"].items()}}
    write_json(pdir / "generated" / "annotations.json", out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    a = ap.parse_args()
    r = draft(a.persona)
    for k, x in r["items"].items():
        print(k, "|", x["can_know"])
    sys.exit(0)
