# Reproduce Lost Thread

## What's in here

| Path | What it is |
|---|---|
| `lost-thread/questions.lock.json` | The 27 frozen questions with the person's need, what the history can know, the reference answer and key facts. Hash in `questions.lock.sha256`; frozen before any contestant ran |
| `lost-thread/CHANGELOG.md` | Every change to the frozen set (none since the freeze) |
| `personas/<p>/` | The three synthetic people: `persona.json`, `world.json`, `events.jsonl` (the week as DayDream rows), `ledger.json` (ground truth), `native_memory.json` (the saved-memory blocks), `questions.jsonl`, and `llm-cache/` (every generator call and reply) |
| `bench/` | Generator, simulator, store loader, harness, blinded pack, scorer (`lt_judge.py`) |
| `swift/` | `bench-store`, which loads events with DayDream 0.1.6's own ingest code |
| `runs/lost-thread-<p>/` | All 162 runs: `traces.jsonl` (prompt, answer, every tool call) and raw CLI logs |
| `grading/lost-thread-20261006/` | The blinded pack (`shard-*.jsonl`, `items.json`), the key (`mapping.json`, `judge/pairs_key.json`), every judge's raw output (`judge/out/`), and the scores (`judge/results.json`, `judge/per_answer.json`) |
| `judge/` | The judge instructions and output schemas, and the Claude Code workflow that ran them |
| `regrade.py` | Re-grade every answer with your own judge |
| `results/` | The chart and `results.json` |
| `docs/DESIGN.md` | How it's built, capture rules, isolation, and caveats |

Everything is MIT licensed, code and data alike.

**About the people.** The generator (DeepSeek V4 Flash) named all three personas "Maya Chen": a developer, a graduate
student and a founder. They are independent people. Everyone and everything in their weeks is invented, except the
student's university, which is a real one. No real person's activity is in this repo.

## Reproduce

Scoring the published grades needs only Python 3.10+ (no packages):

```bash
python3 -m bench.freeze --verify                          # the frozen set matches its hash
python3 bench/lt_judge.py score grading/lost-thread-20261006   # recomputes judge/results.json
```

Rerunning the contestants needs an Apple silicon Mac on macOS 15 or later with:
- DayDream 0.1.6 (the [v0.1.6 release](https://github.com/getnorthlight/daydream/releases/tag/v0.1.6)); point
  `DAYDREAM_MAC_MEM` at its `Contents/MacOS/mac-mem` if it isn't in `/Applications`,
- the Swift toolchain (Xcode or its command line tools), for `swift/build.sh`,
- the Codex CLI (`CODEX_BIN`, default: the copy inside ChatGPT.app) and Claude Code (`CLAUDE_BIN`), each signed in.

```bash
swift/build.sh                                             # fetches DayDream v0.1.6 and builds bench-store
for p in developer student founder; do
  python3 -m bench.build   --persona $p --source generated --ask-day $(date +%F)   # deterministic; no generator calls
  python3 -m bench.harness --persona $p --run-id rerun-$p --new-day
done
python3 -m bench.pack make --runs rerun-developer --runs rerun-student --runs rerun-founder --pack rerun --shards 6
python3 bench/lt_judge.py prep grading/rerun               # then grade (below) and score
```

DayDream's tools read the real clock, so the week is rebuilt to end the workday before the day you run it.
`--new-day` accepts the new dates but requires every question, need and can_know text to match the lock exactly. The
questions use relative days ("yesterday", "Friday"), so run on a Tuesday, as v1 was; on another weekday the check
will likely refuse. We have not yet rerun it on a later day ourselves.

The generator is only called if you generate new personas (`bench.generate`): set `OPENROUTER_API_KEY`, or
`LT_GENERATOR_BASE_URL` and `LT_GENERATOR_MODEL` for another OpenAI-compatible endpoint.

## Re-grade with your own judge

The judges are a Claude model, and one of the contestants is Claude. The headline compares each model with itself,
but you shouldn't have to take our judges' word for it. `regrade.py` sends the same instructions to any
OpenAI-compatible chat API and writes the same format:

```bash
export OPENAI_API_KEY=...                                   # or --api-key-env NAME, --base-url URL
python3 regrade.py --model <judge-model> --name myjudge --dry-run --limit 1   # see one request, send nothing
python3 regrade.py --model <judge-model> --name myjudge     # 162 answers x 3 judges + 108 pairs x 2
python3 bench/lt_judge.py score grading/lost-thread-20261006 --out out-myjudge
```

Results land in `grading/lost-thread-20261006/judge/out-myjudge/results.json`. If your judge disagrees with ours,
please open an issue with the numbers.

## Versioning

v1 is the run above (lock `204b00dd…`, Oct 6, 2026). A rerun after a change to DayDream, the models, the CLIs or the
judges is a new version, published with what changed. Questions are never edited after a run; fixes go through
`bench.freeze --amend` and the changelog.
