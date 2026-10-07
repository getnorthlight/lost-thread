"""The neutral generator model (DeepSeek), behind one small interface.

Backends
- "openai": any OpenAI-compatible /v1/chat/completions endpoint (DeepSeek V4.x Flash served by ollama / LM Studio /
  llama.cpp / a proxy). Configured in config.json -> "generator": {"base_url", "model", "api_key_env"?}.
- "fixture": replays hand-authored JSON from personas/<id>/fixture/<step>.json. Only for plumbing tests; anything
  produced this way is marked `"generator": "fixture"` and must never be published as a result.

Every live call is cached (prompt hash -> response) under personas/<id>/llm-cache/, with the exact model id the server
reported, so a run is reproducible and auditable without calling the model again.
"""
from __future__ import annotations

import json
import sys
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import load_config, read_json, sha, write_json


class GeneratorUnavailable(RuntimeError):
    pass


def _extract_json(text: str):
    text = text.strip()
    # Reasoning models sometimes wrap output; take the first JSON object/array in the text.
    text = re.sub(r"^<think>.*?</think>", "", text, flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        j = text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(text)


class Generator:
    def __init__(self, persona_dir: Path, backend: str | None = None):
        cfg = load_config().get("generator", {})
        self.backend = backend or cfg.get("backend") or "openai"
        # Environment overrides, so no endpoint or key lives in the repo.
        if os.environ.get("LT_GENERATOR_BASE_URL"):
            cfg = {**cfg, "base_url": os.environ["LT_GENERATOR_BASE_URL"]}
        if os.environ.get("LT_GENERATOR_MODEL"):
            cfg = {**cfg, "model": os.environ["LT_GENERATOR_MODEL"]}
        self.cfg = cfg
        self.persona_dir = Path(persona_dir)
        self.cache_dir = self.persona_dir / "llm-cache"
        self.fixture_dir = self.persona_dir / "fixture"
        self.calls = []

    @property
    def label(self) -> str:
        if self.backend == "fixture":
            return "fixture (hand-authored, plumbing only)"
        return f"{self.cfg.get('model')} @ {self.cfg.get('base_url')}"

    def json(self, step: str, system: str, user: str, temperature: float = 0.7, max_tokens: int = 8000,
             validate=None, retries: int = 5):
        """Returns parsed JSON for one generation step. `validate(obj)` returns a list of problems ([] = ok)."""
        if self.backend == "fixture":
            path = self.fixture_dir / f"{step}.json"
            if not path.exists():
                raise GeneratorUnavailable(f"fixture backend: missing {path}")
            obj = read_json(path)
            try:
                problems = validate(obj) if validate else []
            except Exception as e:  # noqa: BLE001  (a reply in an unexpected shape)
                problems = [f"reply has the wrong shape ({type(e).__name__}: {e}); follow the JSON format exactly"]
            if problems:
                raise ValueError(f"fixture {step} fails validation: {problems[:5]}")
            self.calls.append({"step": step, "backend": "fixture"})
            return obj
        base = self.cfg.get("base_url")
        model = self.cfg.get("model")
        if not base or not model:
            raise GeneratorUnavailable("config.json generator.base_url / generator.model are not set (see README: DeepSeek endpoint)")
        key = sha(json.dumps([model, system, user, temperature], ensure_ascii=False), 24)
        cpath = self.cache_dir / f"{step}-{key}.json"
        if cpath.exists():
            rec = read_json(cpath)
            self.calls.append({"step": step, "cached": True, "model": rec.get("model")})
            return rec["parsed"]
        feedback = ""
        last_err = None
        for attempt in range(retries):
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user + feedback}]
            t0 = time.time()
            resp = self._post(base, model, msgs, temperature, max_tokens)
            text = resp["choices"][0]["message"].get("content") or ""
            try:
                obj = _extract_json(text)
            except Exception as e:  # noqa: BLE001
                last_err = f"not JSON: {e}"
                feedback = "\n\nYour previous reply was not valid JSON. Reply with JSON only."
                continue
            try:
                problems = validate(obj) if validate else []
            except Exception as e:  # noqa: BLE001  (a reply in an unexpected shape)
                problems = [f"reply has the wrong shape ({type(e).__name__}: {e}); follow the JSON format exactly"]
            if problems:
                last_err = f"validation: {problems[:8]}"
                print(f"[{step}] attempt {attempt + 1}: {len(problems)} problems, e.g. {problems[:3]}", file=sys.stderr, flush=True)
                feedback = ("\n\nYour previous reply had these problems; fix them and reply with the full JSON again:\n- "
                            + "\n- ".join(problems[:15]))
                continue
            rec = {"step": step, "model": resp.get("model") or model, "requested_model": model, "base_url": base,
                   "temperature": temperature, "seconds": round(time.time() - t0, 2), "usage": resp.get("usage"),
                   "attempt": attempt + 1, "system": system, "user": user + feedback, "raw": text, "parsed": obj}
            write_json(cpath, rec)
            self.calls.append({"step": step, "cached": False, "model": rec["model"], "usage": rec["usage"]})
            return obj
        raise RuntimeError(f"{step}: generator failed after {retries} attempts ({last_err})")

    def _post(self, base, model, msgs, temperature, max_tokens):
        body = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens, "stream": False}
        if self.cfg.get("json_mode", True):
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", **(self.cfg.get("headers") or {})}
        env = self.cfg.get("api_key_env")
        if env and os.environ.get(env):
            headers["Authorization"] = "Bearer " + os.environ[env]
        req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.get("timeout", 600)) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise GeneratorUnavailable(f"cannot reach generator at {base}")
