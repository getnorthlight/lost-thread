"""Lost Thread: run contestants x arms over the frozen items and save full traces (JSONL).

Symmetric setup (design decision, 2026-10-06): both contestants get the IDENTICAL neutral consumer-assistant system prompt,
"You are <ChatGPT|Claude>, a helpful assistant." + the current date and time + (memory arms) the app-style native memory
block, and DayDream's MCP tools only in the DayDream arms. Neither harness's coding-agent prompt is used: with its tools
removed, a coding prompt makes models emit fake tool calls, which would unfairly sink the no-tool arm (pilot, README).

Contestants (consumer subscriptions, no API keys):
- codex  : the Codex CLI bundled with the ChatGPT app (`codex exec`), ChatGPT account auth. --ignore-user-config (the
           user's own config.toml is never loaded), --ephemeral, read-only sandbox, shell/exec/browser/plugins/memories
           off. The symmetric prompt REPLACES Codex's base instructions via `model_instructions_file`, and the
           permissions, environment-context, apps and collaboration-mode sections are switched off. What remains is
           listed in README "Symmetric prompt" (tool definitions only).
- claude : Claude Code headless (`claude -p`) with --system-prompt (replaces the coding-agent prompt), --setting-sources
           project (no user settings, MCP servers, hooks or plugins), --strict-mcp-config with only the benchmark's
           DayDream server, --tools "" (no built-in tools), --no-session-persistence, run from an empty folder.

Arms
  native   : the native memory block + the question.
  native+dd: the native memory block + the question, with DayDream's MCP tools available.   <- headline vs native
  dd       : the question, with DayDream's MCP tools available.
  (none    : the question only; pilot only.)

The DayDream MCP server is the SHIPPED 0.1.6 `mac-mem` binary, always with an explicit --home inside stores/, HOME set
to a scratch folder, and a benchmark-only freshen notification name, so it can never read or wake the operator's real
DayDream. A typed-words bridge for the benchmark store (bench/bridge) stands in for the running app.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import freeze
from .store import Bridge
from .common import (PERSONAS, RUNS, SHIPPED_MAC_MEM, STORES, TZ, TZ_NAME, WORK, append_jsonl, guard_path, guard_store,
                     read_json, read_jsonl, write_json)

CODEX_BIN = Path(os.environ.get("CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"))
CLAUDE_BIN = Path(os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude")))

# The memory tool under test. Default: DayDream (the shipped mac-mem). To test another tool, set LT_MCP_COMMAND to a
# JSON object {"command": ..., "args": [...], "env": {...}} for its stdio MCP server, and LT_MCP_NAME to its server name.
SERVER = os.environ.get("LT_MCP_NAME", "daydream")
CUSTOM_MCP = os.environ.get("LT_MCP_COMMAND")

ARMS = {
    "none": {"memory": False, "daydream": False},
    "native": {"memory": True, "daydream": False},
    "native+dd": {"memory": True, "daydream": True},
    "dd": {"memory": False, "daydream": True},
}

# DayDream connection identities, exactly as Settings > Connections writes them (AIAppConnect.swift).
CLIENT_ID = {"codex": "chatgpt", "claude": "claude-code"}
RECIPIENT = "daydream-connect"

ASSISTANT_NAME = {"codex": "ChatGPT", "claude": "Claude"}
NEUTRAL_CWD = Path("/private/tmp/assistant-chat")
SYSTEM_VARIANT = "symmetric-v1"

# Codex prompt sections beyond the base instructions; all off so only the symmetric prompt (+ tool specs) remains.
CODEX_INCLUDE_OFF = ["include_permissions_instructions", "include_environment_context", "include_apps_instructions",
                     "include_collaboration_mode_instructions"]

CODEX_DISABLE = ["shell_tool", "unified_exec", "browser_use", "browser_use_external", "computer_use", "plugins",
                 "remote_plugin", "image_generation", "multi_agent", "apps", "memories", "goals", "hooks",
                 "skill_search", "tool_suggest", "in_app_browser", "view_image", "sleep_tool"]


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


# ----------------------------------------------------------------------------------------------------------------
# Prompt


def build_prompt(item: dict, arm: str, contestant: str, persona: dict, ask_at: dt.datetime) -> dict:
    """The SYMMETRIC Lost Thread prompt (design decision, 2026-10-06). Both contestants get the identical neutral
    consumer-assistant system prompt, differing only in the assistant's name: "You are <ChatGPT|Claude>, a helpful
    assistant." + the current date and time + (memory arms) that app's native memory block. It REPLACES each harness's
    coding-agent prompt (Codex: model_instructions_file; Claude Code: --system-prompt). The user's message is the
    question alone. DayDream's MCP tools are attached only in the DayDream arms."""
    system = [f"You are {ASSISTANT_NAME[contestant]}, a helpful assistant.", "",
              f"Current date and time: {ask_at.strftime('%A, %B %-d, %Y, %-I:%M %p')} ({TZ_NAME})."]
    if ARMS[arm]["memory"]:
        system += ["", persona["native_memory"][contestant].strip()]
    return {"system": "\n".join(system), "user": item["question"].strip()}


# ----------------------------------------------------------------------------------------------------------------
# DayDream MCP server for one contestant + store


def grant(store_home: Path, client: str) -> str:
    store_home = guard_store(store_home)
    cap_file = store_home.parent / f"capability-{client}.txt"
    if cap_file.exists():
        return cap_file.read_text().strip()
    env = safe_env(store_home)
    out = subprocess.run([str(SHIPPED_MAC_MEM), "--home", str(store_home), "--client", client, "--recipient", RECIPIENT,
                          "grant"], env=env, capture_output=True, text=True, check=True)
    cap = json.loads(out.stdout)["capability"]
    cap_file.write_text(cap)
    os.chmod(cap_file, 0o600)
    return cap


def safe_env(store_home: Path) -> dict:
    agent_home = WORK / "agent-home"
    agent_home.mkdir(parents=True, exist_ok=True)
    return {"PATH": "/usr/bin:/bin", "HOME": str(agent_home), "MAC_MEM_HOME": str(guard_store(store_home)),
            "TZ": TZ_NAME, "DAYDREAM_TEST_FRESHEN_REQUEST": "com.example.daydream-bench.freshen",
            "DAYDREAM_MCP_TOOLSET": "v2", "LANG": "en_US.UTF-8"}


def mcp_server_spec(store_home: Path, contestant: str) -> dict:
    if CUSTOM_MCP:
        spec = json.loads(CUSTOM_MCP)
        return {"command": spec["command"], "args": spec.get("args", []), "env": spec.get("env", {})}
    client = CLIENT_ID[contestant]
    cap = grant(store_home, client)
    env = safe_env(store_home)
    env["MAC_MEM_CAPABILITY"] = cap
    return {"command": str(SHIPPED_MAC_MEM),
            "args": ["--home", str(guard_store(store_home)), "--client", client, "--recipient", RECIPIENT, "mcp"],
            "env": env}


# ----------------------------------------------------------------------------------------------------------------
# Contestant runners. Each returns a dict with answer, tool_calls, model, version, usage, timings, raw paths.


def _toml_str(s: str) -> str:
    return json.dumps(s)


def neutral_cwd() -> Path:
    """An empty folder with a neutral path. Claude Code puts its working directory in the prompt even with
    --system-prompt (its Environment block); a path naming DayDream or a benchmark would cue the model. Same for both."""
    NEUTRAL_CWD.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(NEUTRAL_CWD.iterdir()):
        sys.exit(f"{NEUTRAL_CWD} must stay empty")
    return NEUTRAL_CWD


def run_codex(prompt: dict, server: dict | None, raw_dir: Path, cfg: dict) -> dict:
    cwd = neutral_cwd()
    instr = (raw_dir / "instructions.md").resolve()
    instr.write_text(prompt["system"])
    cmd = [str(CODEX_BIN), "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--json",
           "-s", "read-only", "-C", str(cwd), "-m", cfg["codex_model"],
           "-c", f"model_reasoning_effort={_toml_str(cfg['codex_effort'])}", "-c", 'web_search="disabled"',
           # The symmetric prompt REPLACES Codex's coding-agent base instructions (model_instructions_file); no
           # developer_instructions, no environment context, no sandbox/permissions text. See README "Symmetric prompt".
           "-c", f"model_instructions_file={_toml_str(str(instr))}"]
    for k in CODEX_INCLUDE_OFF:
        cmd += ["-c", f"{k}=false"]
    for f in CODEX_DISABLE:
        cmd += ["--disable", f]
    if server:
        cmd += ["-c", f"mcp_servers.{SERVER}.command={_toml_str(server['command'])}",
                "-c", f"mcp_servers.{SERVER}.args=[" + ",".join(_toml_str(a) for a in server["args"]) + "]",
                "-c", f"mcp_servers.{SERVER}.env={{" + ",".join(f"{k}={_toml_str(v)}" for k, v in server["env"].items()) + "}",
                "-c", f'mcp_servers.{SERVER}.default_tools_approval_mode="approve"',
                "-c", f"mcp_servers.{SERVER}.startup_timeout_sec=30"]
    cmd.append(prompt["user"])
    env = {**os.environ, "RUST_LOG": "info", "NO_COLOR": "1"}
    t0 = time.time()
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=cfg["timeout"], cwd=cwd)
    secs = time.time() - t0
    (raw_dir / "events.jsonl").write_text(p.stdout)
    # The info log names the model actually used and the app version; keep only those lines, not the whole log.
    log = p.stderr
    model = sorted(set(re.findall(r"\bmodel=\"?([A-Za-z0-9._-]+)", log)))
    version = sorted(set(re.findall(r"app\.version=([0-9][0-9.]*)", log)))
    answer_parts, tool_calls, usage, errors = [], [], None, []
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "item.completed":
            it = e["item"]
            if it.get("type") == "agent_message":
                answer_parts.append(it.get("text", ""))
            elif it.get("type") == "mcp_tool_call":
                res = it.get("result")
                text = None
                if isinstance(res, dict):
                    text = "\n".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
                tool_calls.append({"server": it.get("server"), "tool": it.get("tool"), "arguments": it.get("arguments"),
                                   "status": it.get("status"), "error": it.get("error"), "result_text": text})
            elif it.get("type") == "error":
                errors.append(it.get("message"))
            elif it.get("type") in ("command_execution", "web_search", "file_change", "todo_list", "collab_tool_call"):
                # Never expected (shell, web and file tools are off); recorded so the isolation check flags them.
                tool_calls.append({"server": "codex-builtin", "tool": it.get("type"), "arguments": it.get("command") or it.get("query"),
                                   "status": it.get("status"), "error": None, "result_text": None})
        elif e.get("type") == "turn.completed":
            usage = e.get("usage")
        elif e.get("type") in ("turn.failed", "error"):
            errors.append(json.dumps(e)[:500])
    # The final answer is the last agent message; earlier ones are progress notes ("I'll look that up").
    answer = answer_parts[-1] if answer_parts else ""
    return {"answer": answer, "interim_messages": answer_parts[:-1], "tool_calls": tool_calls, "usage": usage,
            "model": model[0] if len(model) == 1 else model, "cli": "codex-cli " + (version[0] if version else "?"),
            "effort": cfg["codex_effort"], "system": SYSTEM_VARIANT, "seconds": round(secs, 1), "exit": p.returncode,
            "errors": errors + ([f"exit {p.returncode}: " + p.stderr[-400:]] if p.returncode else []), "cost_usd": None}


def run_claude(prompt: dict, server: dict | None, raw_dir: Path, cfg: dict) -> dict:
    cwd = neutral_cwd()
    mcp = {"mcpServers": {}}
    if server:
        mcp["mcpServers"][SERVER] = {"type": "stdio", **server}
    mcp_path = raw_dir / "mcp.json"
    mcp_path.write_text(json.dumps(mcp))
    os.chmod(mcp_path, 0o600)
    cmd = [str(CLAUDE_BIN), "-p", "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", str(mcp_path),
           "--tools", "", "--no-session-persistence", "--output-format", "stream-json", "--verbose",
           "--model", cfg["claude_model"]]
    # The symmetric prompt REPLACES Claude Code's coding-agent prompt (which promises Bash, files and a memory folder).
    cmd += ["--system-prompt", prompt["system"]]
    if cfg.get("claude_effort"):
        cmd += ["--effort", cfg["claude_effort"]]
    if server:
        cmd += ["--allowedTools", f"mcp__{SERVER}__*"]
    # The question goes on stdin: --allowedTools takes several values and would swallow a trailing argument.
    t0 = time.time()
    p = subprocess.run(cmd, input=prompt["user"], capture_output=True, text=True, timeout=cfg["timeout"], cwd=cwd)
    secs = time.time() - t0
    (raw_dir / "events.jsonl").write_text(p.stdout)
    mcp_path.unlink(missing_ok=True)  # holds the benchmark store's access key
    answer, tool_calls, usage, model, version, errors, cost, init = "", [], None, None, None, [], None, {}
    pending = {}
    texts = []
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = e.get("type")
        if t == "system" and e.get("subtype") == "init":
            model, version = e.get("model"), e.get("claude_code_version")
            init = {"tools": e.get("tools"), "mcp_servers": e.get("mcp_servers")}
        elif t == "assistant":
            for c in e["message"]["content"]:
                if c["type"] == "text":
                    texts.append(c["text"])
                elif c["type"] == "tool_use":
                    rec = {"server": SERVER if c["name"].startswith(f"mcp__{SERVER}__") else None,
                           "tool": c["name"].removeprefix(f"mcp__{SERVER}__"), "arguments": c.get("input"),
                           "status": None, "error": None, "result_text": None}
                    pending[c["id"]] = rec
                    tool_calls.append(rec)
        elif t == "user":
            for c in e["message"]["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                    rec = pending[c["tool_use_id"]]
                    content = c.get("content")
                    if isinstance(content, list):
                        content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    rec["result_text"] = content
                    rec["status"] = "failed" if c.get("is_error") else "completed"
        elif t == "result":
            answer = e.get("result") or ""
            usage = e.get("usage")
            cost = e.get("total_cost_usd")
            if e.get("subtype") != "success":
                errors.append(e.get("subtype"))
    return {"answer": answer or (texts[-1] if texts else ""), "interim_messages": texts[:-1], "tool_calls": tool_calls,
            "usage": usage, "model": model, "cli": f"claude-code {version}", "effort": cfg.get("claude_effort") or "default",
            "system": SYSTEM_VARIANT,
            "seconds": round(secs, 1), "exit": p.returncode, "errors": errors + ([p.stderr[-400:]] if p.returncode else []),
            "cost_usd": cost, "init": init}


RUNNERS = {"codex": run_codex, "claude": run_claude}


# ----------------------------------------------------------------------------------------------------------------
# Main loop


def check_isolation(result: dict, contestant: str, arm: str) -> list[str]:
    """Flags any sign that the contestant saw something other than the benchmark's tools."""
    problems = []
    for c in result["tool_calls"]:
        if c.get("server") not in (SERVER,):
            problems.append(f"unexpected tool {c.get('server')}/{c.get('tool')}")
    if not ARMS[arm]["daydream"] and result["tool_calls"]:
        problems.append("tool calls in an arm without DayDream")
    if contestant == "claude":
        tools = (result.get("init") or {}).get("tools") or []
        extra = [t for t in tools if not t.startswith(f"mcp__{SERVER}__")]
        if extra:
            problems.append(f"claude had non-DayDream tools: {extra[:6]}")
        servers = [(s or {}).get("name") for s in (result.get("init") or {}).get("mcp_servers") or []]
        if any(s != SERVER for s in servers):
            problems.append(f"claude had other MCP servers: {servers}")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True)
    ap.add_argument("--items", help="comma-separated item ids (default: all)")
    # Full set: native, native+dd, dd. The pilot also ran arm 1: --arms none,native,native+dd,dd
    ap.add_argument("--arms", default="native,native+dd,dd")
    ap.add_argument("--contestants", default="codex,claude")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--codex-model", default="gpt-6.1-sol")
    ap.add_argument("--codex-effort", default="medium")
    ap.add_argument("--claude-model", default="opus")
    ap.add_argument("--claude-effort", default=None)
    ap.add_argument("--timeout", type=int, default=180)   # per attempt; one retry after a timeout
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo-errors", action="store_true", help="re-run jobs whose last trace has errors (timeouts)")
    ap.add_argument("--new-day", action="store_true",
                    help="rerun on a later day: the week's dates shift, so event/ledger hashes and ask_day differ from the "
                         "lock; the question, need and can_know texts must still match it exactly")
    ap.add_argument("--unlocked", action="store_true",
                    help="allow a persona that is not in lost-thread/questions.lock.json (pilot/probes only)")
    args = ap.parse_args(argv)

    pdir = PERSONAS / args.persona
    # Lost Thread: the question set is frozen before any contestant run; refuse to run if anything drifted.
    lock_sha = None
    if freeze.LOCK.exists() and args.persona in read_json(freeze.LOCK)["personas"]:
        probs = freeze.verify(personas=[args.persona])
        if probs and args.new_day:
            probs = freeze.verify_texts(personas=[args.persona])
        if probs:
            sys.exit("questions.lock.json check failed: " + "; ".join(probs))
        lock_sha = read_json(freeze.LOCK)["sha256"]
    elif not args.unlocked:
        sys.exit(f"{args.persona} is not in {freeze.LOCK}; freeze first (python3 -m bench.freeze) or pass --unlocked")
    persona = read_json(pdir / "persona.json")
    persona["native_memory"] = read_json(pdir / "native_memory.json")["blocks"]
    items = read_jsonl(pdir / "questions.jsonl")
    if args.items:
        wanted = set(args.items.split(","))
        items = [i for i in items if i["id"] in wanted]
    if args.limit:
        items = items[: args.limit]
    anchor = read_json(pdir / "anchor.json")
    today = now_local().date().isoformat()
    if anchor["ask_day"] != today:
        sys.exit(f"store is anchored to ask day {anchor['ask_day']} but today is {today}: re-materialize "
                 f"(python3 -m bench.build --persona {args.persona} --ask-day {today}) so DayDream's 'today' matches")
    store_home = guard_store(STORES / args.persona / "home")

    run_id = args.run_id or now_local().strftime("%Y%m%d-%H%M%S") + "-" + args.persona
    rdir = RUNS / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    cfg = {"codex_model": args.codex_model, "codex_effort": args.codex_effort, "claude_model": args.claude_model,
           "claude_effort": args.claude_effort, "system_variant": SYSTEM_VARIANT, "timeout": args.timeout}
    run_info = {"run_id": run_id, "persona": args.persona, "started": now_local().isoformat(),
                                    "config": cfg, "arms": args.arms.split(","), "contestants": args.contestants.split(","),
                                    "items": [i["id"] for i in items], "ask_at": anchor["ask_at"],
                                    "store_sha256": anchor.get("store_sha256"), "mac_mem": str(SHIPPED_MAC_MEM),
                                    "generator": anchor.get("generator"), "lock_sha256": lock_sha}
    if (rdir / "run.json").exists():   # a resumed run keeps its first start and lists every pass
        prev = read_json(rdir / "run.json")
        run_info = {**prev, "passes": prev.get("passes", [{k: prev.get(k) for k in ("started", "config", "arms")}])
                    + [{k: run_info[k] for k in ("started", "config", "arms")}]}
    write_json(rdir / "run.json", run_info)
    # Snapshot the inputs next to the traces, so a later rebuild of the persona can't change what this run is graded on.
    snap = rdir / "inputs"
    if not snap.exists():
        snap.mkdir()
        for f in ("questions.jsonl", "native_memory.json", "anchor.json", "persona.json", "ledger.json", "build-check.json"):
            if (pdir / f).exists():
                shutil.copy2(pdir / f, snap / f)
        shutil.copy2(STORES / args.persona / "load-report.json", snap / "load-report.json")
    jobs = [(i, a, c) for i in items for a in args.arms.split(",") for c in args.contestants.split(",")]
    random.Random(args.seed).shuffle(jobs)  # interleave so drift (rate limits, time of day) spreads across arms
    traces = rdir / "traces.jsonl"
    done = set()
    if traces.exists():
        last = {}
        for t in read_jsonl(traces):
            last[(t["item_id"], t["arm"], t["contestant"])] = t   # a later trace for the same job replaces earlier ones
        done = {k for k, t in last.items() if not (args.redo_errors and t.get("errors"))}
    # The typed-words bridge + recorder heartbeat stand in for the running DayDream app for the whole run.
    args.lock_sha = lock_sha
    with Bridge(args.persona):
        _loop(args, jobs, done, persona, store_home, rdir, run_id, cfg, traces, anchor)
    write_json(rdir / "run.json", {**read_json(rdir / "run.json"), "finished": now_local().isoformat()})
    print(f"traces: {traces}")


def _loop(args, jobs, done, persona, store_home, rdir, run_id, cfg, traces, anchor):
    for n, (item, arm, contestant) in enumerate(jobs, 1):
        if (item["id"], arm, contestant) in done:
            continue
        # The prompt's "now" is the real time of the job: DayDream's tools read the real clock too.
        ask_at = now_local()
        if ask_at.date().isoformat() != anchor["ask_day"]:
            sys.exit("the ask day ended during the run; re-materialize and resume with --run-id " + run_id)
        prompt = build_prompt(item, arm, contestant, persona, ask_at)
        server = mcp_server_spec(store_home, contestant) if ARMS[arm]["daydream"] else None
        raw = rdir / "raw" / f"{item['id']}__{arm}__{contestant}"
        raw.mkdir(parents=True, exist_ok=True)
        started = now_local().isoformat()
        res, attempts = None, 0
        while res is None and attempts < 2:   # one retry after a timeout (a stalled API stream, seen in the pilot)
            attempts += 1
            try:
                res = RUNNERS[contestant](prompt, server, raw, cfg)
            except subprocess.TimeoutExpired:
                if attempts == 2:
                    res = {"answer": "", "tool_calls": [], "errors": ["timeout"], "seconds": args.timeout, "model": None,
                           "cli": None, "usage": None, "exit": None, "cost_usd": None}
        res["attempts"] = attempts
        iso = check_isolation(res, contestant, arm)
        trace = {"run_id": run_id, "trace_id": uuid.uuid4().hex[:12], "item_id": item["id"], "persona": args.persona,
                 "type": item["type"], "arm": arm, "contestant": contestant, "model": res.get("model"),
                 "cli": res.get("cli"), "effort": res.get("effort"), "system_variant": res.get("system"), "started": started, "seconds": res.get("seconds"),
                 "system_prompt_extra": prompt["system"], "prompt": prompt["user"], "question": item["question"], "answer": res.get("answer"),
                 "interim_messages": res.get("interim_messages"), "tool_calls": res.get("tool_calls"),
                 "n_tool_calls": len(res.get("tool_calls") or []), "usage": res.get("usage"), "cost_usd": res.get("cost_usd"),
                 "exit": res.get("exit"), "errors": res.get("errors"), "isolation_problems": iso, "ask_at": ask_at.isoformat(), "attempts": res.get("attempts"),
                 "daydream_server": (("custom MCP server " + SERVER) if CUSTOM_MCP else "mac-mem 0.1.6 (shipped) --home <bench store>") if server else None,
                 "lock_sha256": args.lock_sha,
                 "memory_block": "native_memory.json:" + contestant if ARMS[arm]["memory"] else None}
        append_jsonl(traces, trace)
        print(f"[{n}/{len(jobs)}] {item['id']} {arm:9} {contestant:6} {res.get('seconds')}s tools={trace['n_tool_calls']}"
              f" {'ISOLATION ' + str(iso) if iso else ''} {('ERR ' + str(res.get('errors'))[:120]) if res.get('errors') else ''}",
              flush=True)


if __name__ == "__main__":
    main()
