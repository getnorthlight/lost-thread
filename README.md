<!-- TITLE AND INTRO: placeholder, final text to come. Replace this whole block. -->
# Lost Thread

> **[Intro goes here: one or two paragraphs on what Lost Thread measures and why.]**
<!-- END PLACEHOLDER -->

![Helpful answer rate on Lost Thread, with and without DayDream](results/lost-thread-chart.png)

## Results

v1, run on Tuesday, October 6, 2026. 27 questions about three synthetic workweeks, each asked of each setup.

| | ChatGPT | ChatGPT + DayDream | Claude | Claude + DayDream |
|---|---|---|---|---|
| Helpful answer rate¹ (n = 27) | 18.5%<br><sub>8–37</sub> | **51.9%**<br><sub>34–69</sub> | 3.7%<br><sub>1–18</sub> | **66.7%**<br><sub>48–81</sub> |
| Accuracy² (n = 27) | 27.8% | **61.1%** | 13.0% | **74.1%** |
| Head-to-head win rate³ (n = 27) | 14.8% | **59.3%** | 11.1% | **88.9%** |
| Answers with no made-up facts⁴ (n = 27) | **96.3%** | 92.6% | **100.0%** | 92.6% |
| Answers with no overclaims⁵ (n = 27) | **100.0%** | 92.6% | **100.0%** | 96.3% |

The better number in each pair is bold. "ChatGPT" and "Claude" here mean each model with its own saved memories;
"+ DayDream" adds DayDream's tools to the same setup. With DayDream as the only source (no saved memories), the helpful
answer rate was 63.0% for ChatGPT and 66.7% for Claude.

1. Share of answers whose median judge score was 3 or 4 out of 4 ("answers the need"). Small numbers are 95% Wilson
   intervals.
2. Mean "correct" score (0–2) as a percentage.
3. Each question's two answers (with and without DayDream) were compared twice with the sides swapped. ChatGPT: 16
   wins, 4 losses, 7 ties (25.9%). Claude: 24 wins, 3 losses, 0 ties.
4. A made-up fact is a false claim about the person's week stated as fact. An answer that says "I don't know" can't
   make one up.
5. Stating as known something the history can't know, for example that a message was sent.

- **Models:** ChatGPT is `gpt-6.1-sol` through the Codex CLI 0.159.2 (effort medium). Claude is `claude-opus-5-5`
  through Claude Code 2.1.289. Both get the same plain system prompt (see [design notes](docs/DESIGN.md#symmetric-prompt)).
- **Judges:** 3 Claude Sonnet 5.5 judges (low effort) per answer, blind to the model and setup; the median score
  counts. 2 judges per swapped pair.
- **Length check:** answer length did not predict the helpful score (r = −0.08).
- **Tool use is the model's choice:** with DayDream available, ChatGPT used it on 16 of 27 questions and Claude on 26.

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

## Add your memory tool

Lost Thread works for any memory tool that exposes an MCP server.

1. **Ingest the week.** After `bench.build`, `personas/<p>/events.jsonl` holds the week as one JSON row per event,
   oldest first: `id`, `at` (UTC), `kind` (`app.activated`, `window.changed`, `mouse.click`, `keyboard.text_input`,
   `keyboard.submit`), `app`, `bundle`, `title`, `url` (Chrome: origin only), `page` (the full link, Chrome only),
   `text` (typed words, `keyboard.text_input` only) and capture details. Load it into your tool however it stores
   history. Don't read `ledger.json` or `questions.jsonl`: those are the answers.
2. **Plug it in as the memory server.**
   ```bash
   export LT_MCP_NAME=mytool
   export LT_MCP_COMMAND='{"command": "/path/to/mytool", "args": ["mcp", "--data", "/path/to/week"], "env": {}}'
   python3 -m bench.harness --persona developer --run-id mytool-developer --arms native,native+dd,dd --new-day
   ```
   In the `+dd` arms the contestant gets your server's tools (and its MCP `instructions`) and nothing else; the
   system prompt and question are identical to the DayDream runs. Each answer and tool call lands in
   `runs/<run-id>/traces.jsonl`, and the pack, judges and scorer work unchanged.
3. **Report it** with the version of your tool and the day you ran it.

## Limits

- **Synthetic weeks.** The events go through DayDream's own ingest and serving code, not through real capture on a
  real Mac. This measures answering from a recorded history, not how well anything records.
- **Claude judging Claude.** See [Re-grade with your own judge](#re-grade-with-your-own-judge). No claims are made
  about ChatGPT versus Claude.
- **More made-up facts with DayDream.** 2 of 27 answers in each DayDream setup contained one, against 0–1 without.
  Answers that decline can't make anything up.
- **Memory is approximated.** Neither app's real memory can run headless, so each gets a saved-memory block written
  from the persona's earlier chats, assumed fully up to date, which is generous to the no-DayDream setups.
- **Small sample.** 27 questions per setup; the intervals above are wide.

More in [docs/DESIGN.md](docs/DESIGN.md#caveats).

## Versioning

v1 is the run above (lock `204b00dd…`, Oct 6, 2026). A rerun after a change to DayDream, the models, the CLIs or the
judges is a new version, published with what changed. Questions are never edited after a run; fixes go through
`bench.freeze --amend` and the changelog.
