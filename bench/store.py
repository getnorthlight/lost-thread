"""Isolated DayDream stores for the benchmark.

- `load(persona)`: builds stores/<persona>/home from personas/<persona>/events.jsonl with `swift/bench-store load`
  (DayDream 0.1.6's own MemoryCore: ingest rules, sealing of typed words, retention), saving the typed-words key next
  to the store (never inside the operator's Keychain).
- `Bridge`: runs `swift/bench-store bridge`, the stand-in for the DayDream app's typed-words socket (<home>/ai-read.sock),
  using the app's own answer code with "Let AI apps see your typed words" on (the shipped default).
- `MCPClient`: talks JSON-RPC to the SHIPPED `mac-mem mcp` for probes and checks.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .common import ROOT, SHIPPED_MAC_MEM, STORES, guard_store, read_json, write_json

BENCH_STORE = ROOT / "swift" / "bench-store"


def store_dir(persona: str) -> Path:
    return STORES / persona


def home_of(persona: str) -> Path:
    return guard_store(store_dir(persona) / "home")


def load(persona: str, events: Path, typed_retention: str | None = None) -> dict:
    sdir = store_dir(persona)
    home = home_of(persona)
    if sdir.exists():
        # Rebuilt from events every time; only ever inside bench-context/stores.
        guard_store(sdir / "home")
        shutil.rmtree(sdir)
    sdir.mkdir(parents=True)
    cmd = [str(BENCH_STORE), "load", "--home", str(home), "--events", str(events), "--key", str(sdir / "typed.key"),
           "--report", str(sdir / "load-report.json")]
    if typed_retention:
        cmd += ["--typed-retention", typed_retention]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    report = read_json(sdir / "load-report.json")
    h = hashlib.sha256((home / "memory.sqlite").read_bytes()).hexdigest()
    report["store_sha256"] = h
    write_json(sdir / "load-report.json", report)
    return report


class Bridge:
    """Context manager: the typed-words socket for one benchmark store."""

    def __init__(self, persona: str):
        self.persona = persona
        self.home = home_of(persona)
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen([str(BENCH_STORE), "bridge", "--home", str(self.home), "--key",
                                      str(store_dir(self.persona) / "typed.key")],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline()
        if not line.startswith("listening"):
            raise RuntimeError("bridge failed: " + line + self.proc.stderr.read()[-500:])
        self.socket = line.split(" ", 1)[1].strip()
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class MCPClient:
    def __init__(self, persona: str, client: str = "chatgpt"):
        from .harness import mcp_server_spec, CLIENT_ID
        contestant = {v: k for k, v in CLIENT_ID.items()}.get(client, "codex")
        spec = mcp_server_spec(home_of(persona), contestant)
        self.proc = subprocess.Popen([spec["command"], *spec["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=spec["env"])
        self.n = 0
        self.init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "bench-probe", "version": "0"}})

    def request(self, method, params=None):
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("mac-mem mcp closed: " + self.proc.stderr.read()[-1000:])
            msg = json.loads(line)
            if msg.get("id") == self.n:
                return msg

    def call(self, name, arguments=None) -> str:
        msg = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in msg:
            return "ERROR " + json.dumps(msg["error"])
        return "\n".join(c.get("text", "") for c in msg["result"].get("content", []) if c.get("type") == "text")

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()
