"""Shared paths, safety guards and small helpers for the DayDream context benchmark.

Safety rule: nothing in this benchmark may read, write or point at the operator's real DayDream data
(~/Library/Application Support/DayDream or .../Mac Mem). Every path that reaches a store, a CLI argument or an MCP
config goes through `guard_path`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PERSONAS = ROOT / "personas"
STORES = ROOT / "stores"
RUNS = ROOT / "runs"
WORK = ROOT / "work"
CONFIG = ROOT / "config.json"

# The shipped DayDream 0.1.6 CLI/MCP server. Only ever run with an explicit --home inside STORES.
SHIPPED_MAC_MEM = Path(os.environ.get("DAYDREAM_MAC_MEM", "/Applications/DayDream.app/Contents/MacOS/mac-mem"))
# The source the benchmark's loader/bridge is compiled from (DayDream v0.1.6; swift/build.sh fetches it).
DAYDREAM_SRC = Path(os.environ.get("DAYDREAM_SRC", str(ROOT / "swift" / "daydream-0.1.6-src")))

TZ_NAME = "America/Chicago"
TZ = ZoneInfo(TZ_NAME)

_FORBIDDEN = [
    re.compile(r"Library/Application Support/DayDream", re.I),
    re.compile(r"Library/Application Support/Mac Mem", re.I),
    re.compile(r"Library/Containers/.*daydream", re.I),
]


class UnsafePath(RuntimeError):
    pass


def guard_path(p: os.PathLike | str) -> Path:
    """Refuse any path that could be the operator's real DayDream data; require stores to live under STORES."""
    s = str(Path(p).expanduser().resolve())
    for rx in _FORBIDDEN:
        if rx.search(s):
            raise UnsafePath(f"refusing a path that looks like the operator's real DayDream data: {s}")
    return Path(s)


def guard_store(home: os.PathLike | str) -> Path:
    h = guard_path(home)
    if STORES.resolve() not in h.parents:
        raise UnsafePath(f"benchmark stores must live under {STORES}: {h}")
    return h


def read_json(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write_json(p: Path, obj, indent=2):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(p)


def read_jsonl(p: Path):
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_jsonl(p: Path, rows):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def append_jsonl(p: Path, row):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def load_config() -> dict:
    if CONFIG.exists():
        return read_json(CONFIG)
    return {}


# ----------------------------------------------------------------------------------------------------------------
# Calendar: personas live on consecutive WORKDAYS that end the workday before the "ask day" (the day the contestants
# are run). DayDream's tools resolve "today"/"yesterday" from the real clock, so stores are anchored to the real run
# date and re-materialized (cheap, deterministic) when the run date changes.


def workdays_before(ask_day: dt.date, n: int) -> list[dt.date]:
    days, d = [], ask_day
    while len(days) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return list(reversed(days))


def local_dt(day: dt.date, hhmm_or_seconds) -> dt.datetime:
    if isinstance(hhmm_or_seconds, str):
        h, m = hhmm_or_seconds.split(":")[:2]
        secs = int(h) * 3600 + int(m) * 60
    else:
        secs = int(hhmm_or_seconds)
    return dt.datetime(day.year, day.month, day.day, tzinfo=TZ) + dt.timedelta(seconds=secs)


def iso_utc(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_clock(t: dt.datetime) -> str:
    return t.astimezone(TZ).strftime("%-I:%M %p").replace("AM", "am").replace("PM", "pm")


def fmt_day(d: dt.date) -> str:
    return d.strftime("%A, %B %-d")
