"""Deterministic simulator: a persona's week plan (world.json) -> raw DayDream 0.1.6 rows (Evidence JSONL) + a ledger.

The rows follow what the shipped recorder writes (see README "Capture rules"), because the loader feeds them through
DayDream's own `ingest`, which refuses anything the recorder could not have produced:
- native app switch: `app.activated` then `window.changed` (same second); a title change: `window.changed`; focus moving
  inside a window re-emits the same title; `mouse.click` rows in native (non-browser) apps only;
- Google Chrome: never `app.activated`; one `window.changed` page row per page/title change, url = origin only, the
  link kept separately in `page`, site-only hosts (search, chat/AI, webmail without a subject) with an empty title,
  Google results pages titled with the search words; proofs from the page reader (`chrome-appleevents-page-v1`);
- typed words: one `keyboard.text_input` row per unit (final text, typos that weren't fixed stay), only in apps and
  sites the 0.1.6 typing table allows; Slack desktop, VS Code, Zoom, Figma, Google Docs record no typing; send facts are
  decided by DayDream's SendRules inside the loader;
- idle, lunch, meetings without input and notification banners write nothing (gaps).

Mess added here, seeded and reproducible: start-time jitter, Slack/Messages notification glances, short distraction
browsing, focus re-emits, clicks, uncorrected typos in casual messages, and abandoned tabs from the plan.
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import re
import uuid
from pathlib import Path

from .common import PERSONAS, TZ, fmt_clock, iso_utc, local_dt, read_json, workdays_before, write_json, write_jsonl

# ---------------------------------------------------------------------------------------------------------------------
# App catalog: what 0.1.6 records for each app (typing table: PrivacyPolicy/TypingCategories.swift, release flags on).

APPS = {
    "chrome": ("Google Chrome", "com.google.Chrome", "browser"),
    "slack": ("Slack", "com.tinyspeck.slackmacgap", None),            # typing never recorded (signer unconfirmed)
    "vscode": ("Code", "com.microsoft.VSCode", None),                  # typing never recorded
    "terminal": ("Terminal", "com.apple.Terminal", "code"),           # commands recorded; Claude Code prompts too
    "xcode": ("Xcode", "com.apple.dt.Xcode", "code"),
    "zoom": ("zoom.us", "us.zoom.xos", None),
    "messages": ("Messages", "com.apple.MobileSMS", "messagesAndEmail"),
    "mail": ("Mail", "com.apple.mail", "messagesAndEmail"),
    "notes": ("Notes", "com.apple.Notes", "writing"),
    "chatgpt": ("ChatGPT", "com.openai.chat", "searchAndAI"),
    "claude": ("Claude", "com.anthropic.claudefordesktop", "searchAndAI"),
    "spotify": ("Spotify", "com.spotify.client", None),
    "finder": ("Finder", "com.apple.finder", None),
    "figma": ("Figma", "com.figma.Desktop", None),
    "calendar": ("Calendar", "com.apple.iCal", None),
    "preview": ("Preview", "com.apple.Preview", None),
    "excel": ("Microsoft Excel", "com.microsoft.Excel", None),
    "keynote": ("Keynote", "com.apple.Keynote", None),
    "notion": ("Notion", "notion.id", None),                           # desktop app: typing not recorded
}
TYPING_APPS = {k for k, v in APPS.items() if v[2] and v[2] != "browser"}

# Chrome hosts whose page rows are site-only (empty title, no link): search, chat/AI, social message paths.
SITE_ONLY_HOSTS = ("chatgpt.com", "claude.ai", "gemini.google.com", "perplexity.ai", "app.slack.com", "discord.com",
                   "web.whatsapp.com", "teams.microsoft.com", "meet.google.com", "www.bing.com", "duckduckgo.com")
EMAIL_HOSTS = ("mail.google.com", "outlook.office.com", "outlook.live.com", "mail.yahoo.com", "app.fastmail.com")
NO_TYPING_HOSTS = ("docs.google.com", "accounts.google.com")
SEARCH_HOSTS = ("www.google.com",)
AI_HOSTS = ("chatgpt.com", "claude.ai", "gemini.google.com", "perplexity.ai")


def clean_webmail_title(title: str) -> str:
    """What the page reader keeps of a webmail tab title: the subject, or "Inbox", never the account address or the
    unread count ("Inbox (12) - someone@school.edu - Gmail" -> "Inbox")."""
    t = re.sub(r"\s+-\s+[^-]*@[^-]*(\s+-\s+(Gmail|Outlook|Mail))?\s*$", "", title or "")
    t = re.sub(r"\s+-\s+(Gmail|Outlook|Mail)\s*$", "", t)
    t = re.sub(r"^(Inbox|Sent Mail|Drafts|Starred|Archive)\s*\(\d+\)", r"\1", t)
    t = re.sub(r"\S+@\S+", "", t).strip(" -")
    return t or "Inbox"


class Sim:
    def __init__(self, world: dict, ask_day: dt.date, seed: int):
        self.w = world
        self.ask_day = ask_day
        self.rng = random.Random(seed)
        self.rows: list[dict] = []
        self.segments: list[dict] = []   # ground-truth activity: what the person actually did and for how long
        self.typed: list[dict] = []      # ground-truth typed units (also those DayDream can't see, e.g. Slack desktop)
        self.ns = uuid.UUID(int=seed)
        self.front = None                # (appkey, title)
        self.last_page = None            # (tab, host, title) of the last Chrome page row saved
        self.tabs: dict[str, str] = {}   # page key -> tab id
        self.next_tab = 1733
        self.seq = 0
        self.session = None
        self.day_index = 0
        self.notes: list[str] = []

    # --- ids and proofs ---------------------------------------------------------------------------------------------

    def rid(self) -> str:
        self.seq += 1
        return f"native-{self.session}-{self.seq}"

    def uuid_for(self, *parts) -> str:
        return str(uuid.uuid5(self.ns, "/".join(map(str, parts)))).upper()

    def base(self, t: dt.datetime, kind: str, app: str, title: str = "", url: str = "", text: str = "", precise=False):
        a = APPS[app]
        at = t.astimezone(dt.timezone.utc)
        at_s = at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z" if precise else iso_utc(t)
        return {"id": self.rid(), "at": at_s, "kind": kind, "app": a[0], "bundle": a[1], "title": title, "url": url,
                "text": text, "secure": False, "privateWindow": False, "synthetic": False}

    # --- native apps ------------------------------------------------------------------------------------------------

    def focus(self, t: dt.datetime, app: str, title: str):
        if app == "chrome":
            raise ValueError("use page() for Chrome")
        if not self.front or self.front[0] != app:
            self.rows.append(self.base(t, "app.activated", app, title))
            self.rows.append(self.base(t, "window.changed", app, title))
        elif self.front[1] != title:
            self.rows.append(self.base(t, "window.changed", app, title))
        self.front = (app, title)

    def dwell_native(self, t0: dt.datetime, t1: dt.datetime, app: str, title: str, clicks_per_min: float):
        """Clicks and focus re-emits while the person works in one native window."""
        t = t0
        while True:
            gap = self.rng.expovariate(clicks_per_min / 60.0) if clicks_per_min > 0 else 10 ** 9
            t = t + dt.timedelta(seconds=max(4, gap))
            if t >= t1:
                break
            if self.rng.random() < 0.25:
                self.rows.append(self.base(t, "window.changed", app, title))   # focus moved inside the window
            else:
                self.rows.append(self.base(t, "mouse.click", app, title))

    # --- Chrome -----------------------------------------------------------------------------------------------------

    def tab_for(self, key: str, new: bool) -> str:
        if new or key not in self.tabs:
            self.next_tab += self.rng.randint(1, 4)
            self.tabs[key] = str(self.next_tab)
        return self.tabs[key]

    def page(self, t: dt.datetime, host: str, title: str, path: str = "", tab_key: str | None = None, new_tab=False):
        """A Chrome page row as the page reader saves it."""
        origin = "https://" + host
        stored_title, link = title, (origin + path if path and path != "/" else None)
        if host in SITE_ONLY_HOSTS or host.startswith("www.google.") and not path.startswith("/search"):
            stored_title, link = "", None
        if host in EMAIL_HOSTS:
            link = None   # webmail keeps only the cleaned subject (or "Inbox")
            stored_title = clean_webmail_title(stored_title)
        if host in SEARCH_HOSTS and path.startswith("/search"):
            link = None   # results page: the search words are the title, no link
        tab = self.tab_for(tab_key or host + path, new_tab)
        # The page reader never saves the same window/tab/origin/title twice in a row (switching back to Chrome on the
        # same page writes nothing new).
        if self.last_page == (tab, host, stored_title):
            self.front = ("chrome", (host, stored_title))
            return None
        self.last_page = (tab, host, stored_title)
        t = t + dt.timedelta(milliseconds=self.rng.randint(200, 900))
        e = self.base(t, "window.changed", "chrome", stored_title, origin, precise=True)
        if link:
            e["page"] = link
        chk = t - dt.timedelta(milliseconds=self.rng.randint(100, 800))
        e["browserVerification"] = {"provider": "chrome-appleevents-page-v1", "mode": "normal", "windowID": "1520",
                                    "tabID": tab, "focusedRole": "",
                                    "checkedAt": chk.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{chk.microsecond // 1000:03d}Z",
                                    "policyRevision": "set-by-loader"}
        self.rows.append(e)
        self.front = ("chrome", (host, stored_title))
        return e

    # --- typing -----------------------------------------------------------------------------------------------------

    def unit(self, t_end: dt.datetime, text: str, seal: str):
        secs = max(3, len(text) / self.rng.uniform(3.5, 6.0))
        started = t_end - dt.timedelta(seconds=secs)
        edits = self.rng.randint(0, max(1, len(text) // 40))
        return {"version": "typed-unit/v2", "runID": self.uuid_for("run", self.seq, text[:20]), "part": 1,
                "sealReason": seal, "startedAt": iso_utc(started), "keys": len(text) + edits * 2, "edits": edits,
                "withheld": 0}

    def typed_native(self, t: dt.datetime, app: str, title: str, text: str, field: str, seal: str, sent=False, to=None,
                     terminal_tool=None, subject=None):
        e = self.base(t, "keyboard.text_input", app, title, text=text)
        e["captureProvenance"] = {"policyRevision": "set-by-loader", "classifierVersion": "set-by-loader",
                                  "windowID": self.uuid_for("win", app, title), "focusID": self.uuid_for("focus", app, title, field),
                                  "checkedAt": iso_utc(t), "generation": 1, "unit": self.unit(t, text, seal)}
        e["_typing"] = {"field": field, "seal": seal, "sent": sent, "recipient": to, "terminalTool": terminal_tool,
                        "subject": subject}
        self.rows.append(e)
        if seal == "submit" and app in TYPING_APPS:
            self.rows.append(self.base(t + dt.timedelta(milliseconds=300), "keyboard.submit", app, title))
        return e

    def typed_web(self, t: dt.datetime, host: str, place: str, text: str, field: str, seal: str, sent=False, to=None,
                  subject=None, tab_key=None):
        origin = "https://" + host
        title = place
        if host in SITE_ONLY_HOSTS or host in SEARCH_HOSTS:
            title = host
        tab = self.tab_for(tab_key or host, False)
        e = self.base(t, "keyboard.text_input", "chrome", title, origin, text=text)
        e["browserVerification"] = {"provider": "chrome-typing-join-v1", "mode": "normal", "windowID": "1520", "tabID": tab,
                                    "focusedRole": "AXTextArea" if field in ("body", "message", "textArea") else "AXTextField",
                                    "checkedAt": iso_utc(t), "documentID": self.uuid_for("doc", host, tab),
                                    "focusID": self.uuid_for("focus", host, tab, field), "policyRevision": "set-by-loader"}
        e["captureProvenance"] = {"policyRevision": "set-by-loader", "classifierVersion": "set-by-loader", "windowID": "1520",
                                  "focusID": e["browserVerification"]["focusID"], "checkedAt": iso_utc(t), "generation": 1,
                                  "unit": self.unit(t, text, seal)}
        e["_typing"] = {"field": field, "seal": seal, "sent": sent, "recipient": to, "host": host, "subject": subject}
        self.rows.append(e)
        return e

    def search_row(self, t: dt.datetime, query: str, tab_key: str):
        host = "www.google.com"
        tab = self.tab_for(tab_key, False)
        e = self.base(t, "keyboard.text_input", "chrome", host, "https://" + host, text=query)
        e["browserVerification"] = {"provider": "chrome-search-url-v1", "mode": "normal", "windowID": "1520", "tabID": tab,
                                    "focusedRole": "searchResultsPage", "checkedAt": iso_utc(t),
                                    "documentID": self.uuid_for("sdoc", query), "focusID": self.uuid_for("sfocus", query),
                                    "policyRevision": "set-by-loader"}
        e["captureProvenance"] = {"policyRevision": "set-by-loader", "classifierVersion": "set-by-loader", "windowID": "1520",
                                  "focusID": e["browserVerification"]["focusID"], "checkedAt": iso_utc(t), "generation": 1,
                                  "unit": {"version": "typed-unit/v2", "runID": self.uuid_for("srun", query), "part": 0,
                                           "sealReason": "submit", "startedAt": iso_utc(t), "keys": None, "edits": None,
                                           "withheld": 0}}
        e["_typing"] = {"field": "search", "seal": "submit", "host": host}
        self.rows.append(e)

    # --- mess -------------------------------------------------------------------------------------------------------

    def typo(self, text: str, protect: list[str]) -> str:
        words = [m for m in re.finditer(r"[A-Za-z]{5,}", text)]
        self.rng.shuffle(words)
        for m in words:
            w = m.group(0)
            if any(w.lower() in p.lower() for p in protect):
                continue
            i = self.rng.randint(1, len(w) - 2)
            bad = w[:i] + w[i + 1] + w[i] + w[i + 2:]
            return text[:m.start()] + bad + text[m.end():]
        return text

    def glance(self, t: dt.datetime, back: tuple, kinds=("slack",)) -> dt.datetime:
        """A notification pulls the person away for a few seconds to a minute, then they come back."""
        noise = self.w.get("noise", {})
        k = self.rng.choice(kinds)
        if k == "slack" and noise.get("slack_titles"):
            title = self.rng.choice(noise["slack_titles"])
            self.focus(t, "slack", title)
            dur = self.rng.randint(12, 75)
            self.seg(t, t + dt.timedelta(seconds=dur), "slack", title, None, "notification glance")
            t = t + dt.timedelta(seconds=dur)
        self.resume(t, back)
        return t

    def distraction(self, t: dt.datetime, back: tuple) -> dt.datetime:
        noise = self.w.get("noise", {})
        pages = noise.get("pages") or []
        if not pages:
            return t
        p = self.rng.choice(pages)
        dur = self.rng.randint(90, 420)
        self.page(t, p["host"], p["title"], p.get("path", ""), tab_key="distraction:" + p["host"])
        self.seg(t, t + dt.timedelta(seconds=dur), "chrome", p["title"], None, "distraction", host=p["host"])
        t = t + dt.timedelta(seconds=dur)
        self.resume(t, back)
        return t

    def resume(self, t: dt.datetime, back: tuple):
        if not back:
            return
        app, title = back
        if app == "chrome":
            host, ptitle, path = title
            self.page(t, host, ptitle, path, tab_key=host + path)
        else:
            self.focus(t, app, title)

    def seg(self, t0, t1, app, title, thread, what, host=None, step=None):
        self.segments.append({"day": self.day_index, "start": t0.isoformat(), "end": t1.isoformat(),
                              "minutes": round((t1 - t0).total_seconds() / 60, 2), "app": app, "title": title,
                              "host": host, "thread": thread, "what": what, "step": step})

    # --- steps ------------------------------------------------------------------------------------------------------

    def run_step(self, t: dt.datetime, s: dict, thread, block_noise: bool, sid: str) -> dt.datetime:
        kind = s["do"]
        minutes = float(s.get("min", 1))
        end = t + dt.timedelta(seconds=minutes * 60 * self.rng.uniform(0.85, 1.15))
        if kind == "app":
            app, title = s["app"], s["title"]
            self.focus(t, app, title)
            cpm = s.get("clicks", {"vscode": 0.8, "zoom": 0.15, "slack": 1.2, "xcode": 0.9, "figma": 2.0,
                                   "excel": 1.5}.get(app, 0.6))
            # Long native stretches get interrupted by notifications now and then.
            cur = t
            titles = s.get("titles") or [title]
            chunks = max(1, int(minutes // 9))
            for c in range(chunks):
                c_end = t + (end - t) * (c + 1) / chunks
                ttl = titles[c % len(titles)]
                self.focus(cur, app, ttl)
                self.dwell_native(cur, c_end, app, ttl, cpm)
                self.seg(cur, c_end, app, ttl, thread, s.get("what", ""), step=sid)
                cur = c_end
                if block_noise and c < chunks - 1 and self.rng.random() < 0.35:
                    cur = self.glance(cur, (app, ttl))
            return cur
        if kind == "page":
            host, title, path = s["host"], s["title"], s.get("path", "")
            self.page(t, host, title, path, tab_key=s.get("tab") or host + path, new_tab=s.get("new_tab", False))
            self.seg(t, end, "chrome", title, thread, s.get("what", ""), host=host, step=sid)
            if block_noise and minutes > 8 and self.rng.random() < 0.4:
                mid = t + (end - t) / 2
                self.glance(mid, ("chrome", (host, title, path)))
            return end
        if kind == "search":
            q = s["query"]
            key = "search:" + q
            self.page(t, "www.google.com", q, "/search", tab_key=key)
            self.search_row(t + dt.timedelta(milliseconds=600), q, key)
            self.seg(t, end, "chrome", q, thread, "searched", host="www.google.com", step=sid)
            return end
        if kind == "type":
            where = s["where"]
            text = s["text"]
            if s.get("typo"):
                text = self.typo(text, s.get("protect", []))
            secs = max(4, len(text) / self.rng.uniform(3.5, 6.0)) + self.rng.uniform(2, 20)
            t_end = t + dt.timedelta(seconds=secs)
            visible = True
            if where.startswith("web:"):
                host = where[4:]
                place = s.get("place", "")
                if s.get("page_title") is not None:
                    self.page(t, host, s["page_title"], s.get("path", ""), tab_key=s.get("tab") or host + s.get("path", ""))
                if host in NO_TYPING_HOSTS:
                    visible = False
                else:
                    self.typed_web(t_end, host, place, text, s.get("field", "textArea"), s.get("seal", "idle"),
                                   sent=s.get("sent", False), to=s.get("to"), subject=s.get("subject"),
                                   tab_key=s.get("tab") or host + s.get("path", ""))
                app_for_seg = "chrome"
            else:
                app = where
                title = s["title"]
                self.focus(t, app, title)
                if app in TYPING_APPS:
                    self.typed_native(t_end, app, title, text, s.get("field", "textArea"), s.get("seal", "idle"),
                                      sent=s.get("sent", False), to=s.get("to"), terminal_tool=s.get("tool"),
                                      subject=s.get("subject"))
                else:
                    visible = False   # e.g. Slack desktop: DayDream records the window, never the words
                app_for_seg = app
            self.typed.append({"day": self.day_index, "at": t_end.isoformat(), "where": where, "text": text,
                               "visible_to_daydream": visible, "thread": thread, "step": sid, "sent": s.get("sent", False),
                               "place": s.get("title") or s.get("place"), "to": s.get("to")})
            end = max(end, t_end)
            self.seg(t, end, app_for_seg, s.get("title") or s.get("place") or where, thread, s.get("what", "typed"),
                     host=where[4:] if where.startswith("web:") else None, step=sid)
            return end
        if kind == "idle":
            self.seg(t, end, None, None, thread, s.get("what", "away"), step=sid)
            self.front = self.front  # nothing is written while away
            return end
        raise ValueError(f"unknown step kind {kind}")

    def run(self) -> dict:
        days = self.w["days"]
        dates = workdays_before(self.ask_day, len(days))
        for di, (day, date) in enumerate(zip(days, dates), 1):
            self.day_index = di
            self.session = self.uuid_for("session", date.isoformat())
            self.seq = 0
            self.front = None
            self.last_page = None
            self.tabs = {}
            prev_end = None
            for bi, block in enumerate(day["blocks"]):
                jitter = self.rng.randint(-3, 4) * 60 + self.rng.randint(0, 59)
                t = local_dt(date, block["start"]) + dt.timedelta(seconds=jitter)
                if prev_end and t < prev_end:
                    t = prev_end + dt.timedelta(seconds=self.rng.randint(5, 40))
                noise_ok = block.get("noise", True)
                for si, step in enumerate(block["steps"]):
                    sid = step.get("id") or f"d{di}b{bi}s{si}"
                    t = self.run_step(t, step, block.get("thread"), noise_ok, sid)
                    t += dt.timedelta(seconds=self.rng.randint(2, 25))
                    # Context switching: now and then a short distraction between steps of long blocks.
                    if noise_ok and step["do"] in ("app", "page") and self.rng.random() < 0.12:
                        back = (self.front[0], self.front[1]) if self.front and self.front[0] != "chrome" else None
                        t = self.distraction(t, back)
                prev_end = t
        return {"dates": [d.isoformat() for d in dates]}


def segment_metrics(segments: list[dict]) -> dict:
    """True minutes per day x (thread | app | host), first/last times per thread."""
    out = {"by_thread": {}, "by_app": {}, "by_host": {}, "thread_last": {}, "thread_first": {}}
    for s in segments:
        d = str(s["day"])
        if s["app"] is None:
            continue
        if s["thread"]:
            out["by_thread"].setdefault(d, {}).setdefault(s["thread"], 0)
            out["by_thread"][d][s["thread"]] += s["minutes"]
            out["thread_last"][s["thread"]] = s
            out["thread_first"].setdefault(s["thread"], s)
        out["by_app"].setdefault(d, {}).setdefault(s["app"], 0)
        out["by_app"][d][s["app"]] += s["minutes"]
        if s.get("host"):
            out["by_host"].setdefault(d, {}).setdefault(s["host"], 0)
            out["by_host"][d][s["host"]] += s["minutes"]
    for k in ("by_thread", "by_app", "by_host"):
        for d in out[k]:
            out[k][d] = {a: round(m, 1) for a, m in out[k][d].items()}
    return out


def simulate(persona: str, ask_day: dt.date, seed: int = 11) -> dict:
    pdir = PERSONAS / persona
    world = read_json(pdir / "world.json")
    sim = Sim(world, ask_day, seed)
    info = sim.run()
    rows = sorted(sim.rows, key=lambda r: (r["at"], r["id"]))
    write_jsonl(pdir / "events.jsonl", rows)
    metrics = segment_metrics(sim.segments)
    ledger = {"persona": persona, "ask_day": ask_day.isoformat(), "seed": seed, "dates": info["dates"],
              "segments": sim.segments, "typed": sim.typed, "metrics": metrics, "row_count": len(rows)}
    write_json(pdir / "ledger-sim.json", ledger)
    return ledger


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--ask-day", default=dt.datetime.now(TZ).date().isoformat())
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    led = simulate(a.persona, dt.date.fromisoformat(a.ask_day), a.seed)
    print(f"{led['row_count']} rows over {led['dates']}")
