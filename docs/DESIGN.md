# Lost Thread: design notes

How the benchmark is built, what each contestant sees, and what it can and cannot measure. Results and how to run it are in the [README](../README.md).

## Lost Thread at a glance

| | |
|---|---|
| People | 3 synthetic personas, generated neutrally by DeepSeek V4 Flash: `developer`, `student`, `founder` |
| Week | 5 workdays each (Tue Sep 29 – Mon Oct 5, 2026), asked on Tue Oct 6 |
| Questions | 9 per persona, **27** in all, over 8 types, frozen and hashed before any contestant run |
| Arms | `native` (memory only), `native+dd` (memory + DayDream, **the headline vs `native`**), `dd` (DayDream only) |
| Contestants | ChatGPT (`gpt-6.1-sol` through the Codex CLI 0.159.2 bundled in ChatGPT.app) and Claude (`claude-opus-5-5` through Claude Code 2.1.289), both on regular consumer subscriptions |
| Runs | 27 × 3 × 2 = **162** |
| Prompt | Symmetric: the same neutral consumer-assistant system prompt for both (below) |
| Grading | Blinded pack, 3 Claude Sonnet 5.5 judges per answer (median), pairwise judged twice with sides swapped (`judge/`) |

**Question types** (each has a reference answer and key facts; a fact is code-checked where possible):

| Type | What it tests | Items |
|---|---|---|
| `exact_recall` | An identifier, number or phrase from the week | 4 |
| `where_left_off` | Where a thread stood at the end of a day | 3 |
| `cross_app` | An answer that needs two places together | 3 |
| `time_spent` | Minutes on an app, site or thread on one day (truth from the ledger) | 3 |
| `decision` | A number or choice that moved across days, and where it landed | 3 |
| `vague` | A fuzzy description sharing **no content word** with its target's title (`bench.build` checks this) | 4 |
| `negative` | Something that did not happen; the right answer says so | 4 |
| `control` | Answerable from chat memory alone (memory-only controls: DayDream must not hurt) | 3 |

Each persona gets one of every type plus one extra, rotated: `exact_recall` (developer), `vague` (student),
`negative` (founder) (`generate.quota_for`).

## Symmetric prompt

**Both contestants get the identical system prompt**, differing only in the assistant's name:

```
You are ChatGPT, a helpful assistant.          (Claude: "You are Claude, a helpful assistant.")

Current date and time: Tuesday, October 6, 2026, 3:12 PM (America/Chicago).

<the native memory block, in the memory arms: ChatGPT style for ChatGPT, Claude style for Claude>
```

The user's message is the question alone. DayDream's MCP tools are attached **only** in the DayDream arms, and
nothing nudges the model to use them: DayDream's own `instructions` and tool descriptions are all it gets, as in real
use.

**Why not the CLIs' own prompts.** Codex and Claude Code are agent harnesses whose built-in system prompts are
coding-agent prompts that promise a shell, files and a memory folder. With those tools removed (they must be: with Bash
or Read the model starts exploring the real Mac), a coding prompt makes models emit fake tool calls. In the pilot,
Claude under Claude Code's default prompt wrote fake `<invoke name="Bash">` blocks, failed to parse its own tool calls
and stalled in the arms without DayDream: 8 of 24 first attempts failed, and about 45 of the pilot's 62 minutes were
stalls. That would unfairly sink the no-tool arm and overstate DayDream's uplift. A consumer chat app doesn't have a
coding prompt, so Lost Thread replaces both harnesses' prompts with the plain one above.

**How each harness is overridden**
- **Codex**: `-c model_instructions_file=<file>` replaces Codex's base instructions (the coding-agent prompt) with the
  symmetric prompt, written per job to `runs/<run>/raw/<job>/instructions.md`. No `developer_instructions`. Also
  `include_permissions_instructions=false`, `include_environment_context=false`, `include_apps_instructions=false`,
  `include_collaboration_mode_instructions=false`. Verified: the model introduces itself as ChatGPT, follows a rule put
  only in that file, and doesn't know its working folder.
- **Claude Code**: `--system-prompt <same text>` replaces the coding-agent prompt.

**What remains that we cannot remove** (documented, not hidden):
- **Codex** still sends its **tool definitions**, which `--disable` doesn't drop in 0.159.2: the code-mode host
  `functions.exec` (inside it `tools.apply_patch` and `tools.clock__curr_time`), `functions.wait`,
  `functions.request_user_input` / `request_user_input_async`, and the multi-agent `collaboration.*` tools
  (`followup_task`, `interrupt_agent`, `list_agents`, `send_message`, `spawn_agent`, `wait_agent`); in the DayDream
  arms also the MCP resource helpers (`list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource`). The
  sandbox is read-only and the shell is off, so nothing can run; `check_isolation` flags any call to them, and DayDream's
  tools are reached through `functions.exec`, which is how the real app does it. Disabling code mode
  (`--disable code_mode_host`) breaks MCP entirely, so it stays on. Codex receives DayDream's server `instructions` in
  the MCP handshake (seen in its trace log); whether it forwards them to the model could not be confirmed, because the
  CLI doesn't log request bodies.
- **Claude Code** still adds, around our prompt: the prefix line "You are a Claude agent, built on Anthropic's Claude
  Agent SDK."; an Environment block (working folder, "not a git repository", platform, shell, OS version, the model
  name and knowledge cutoff, and "Today's date is 2026-10-06"); and a context reminder with the signed-in account's
  email address (Claude Code adds it whenever an account is signed in; there is no switch). It forwards DayDream's
  server `instructions` to the model verbatim (the model quoted them in a probe). To keep the Environment block from
  cueing the model, both contestants run from an empty folder with a neutral path, `/private/tmp/assistant-chat`
  (the pilot's folder path contained "DayDream"). `bench.pack` redacts any email address the persona's world doesn't
  contain before judging.

Every trace records `system_variant: "symmetric-v1"` and the exact `system_prompt_extra` text it got.

## Freeze, lock and changelog

The question set was frozen **before any contestant run**: `python3 -m bench.freeze` wrote
`lost-thread/questions.lock.json` with every item exactly as graded (question, need, can_know, reference, facts,
negative, hallucination note) and sha256 hashes of each persona's `questions.jsonl`, `native_memory.json`,
`events.jsonl` (the rows DayDream's store is loaded from) and the ledger's facts. The lock's own hash is in
`lost-thread/questions.lock.sha256`.

- `bench.harness` refuses to run a locked persona whose files no longer match, and stamps every trace and `run.json`
  with the lock hash. `bench.pack make` refuses runs made under a different lock.
- **No item may be dropped or edited after results are seen.** A fixture fix (world or events) is allowed only with
  `python3 -m bench.freeze --amend "<why>" --rerun <ids>`: the question, need and can_know texts must stay identical,
  the reason goes into `lost-thread/CHANGELOG.md`, and the listed items are rerun for every arm and contestant.

## Need and can_know

Every frozen item carries two grading fields :
- `need`: one line on what the person actually wants to accomplish by asking (for example "wants to know whether
  Marcus already has the results, so they don't double-send or forget"). Judges grade helpfulness against it
  (`need_met`). Written by the generator with the question.
- `can_know`: what DayDream can and cannot know for that question. Judges penalize answers that claim more
  (`overclaims`). Drafted by DeepSeek from a capability sheet taken from the 0.1.6 code (`bench/annotate.py`), then
  reviewed line by line by the harness author against the ledger before the freeze; hand edits are marked in
  `personas/<p>/generated/annotations.json`.

**What 0.1.6 records as a send, per app** (`PrivacyPolicy/SendRules.swift`). DayDream stores a *send gesture*
("detected" / "none" / "unknown", never "sent") on each typed unit:
- detected: Return in AI apps and AI sites, chat apps and chat sites (Slack, Discord, WhatsApp, Telegram, Signal,
  Teams, Messenger), terminals and search boxes; Mail.app's own Send; Command-Return or a proven Post click on social
  sites; Messages only when the composer empties after Return; webmail (Gmail, Outlook web) only when the compose
  window closes after Command-Return;
- none: notes, documents and code editors;
- unknown: everything else (forms, a Return in Mail, Command-Return in webmail before the window closes).

**But AI apps never see it.** The v2 tools never show a send state (`AgentSharePolicy.sendStateLine`: "Never shown:
whether a message went out; DayDream can't tell."; the server instructions say "DayDream can't tell whether a message
went out: never tell the person it was sent."). So for every Lost Thread question, whether a message or email went
out is outside `can_know`; DayDream can show that words were typed, where, and when. Typed words in apps the 0.1.6
typing table doesn't cover (Slack desktop, VS Code, Zoom, Figma, Notion desktop, Word, Outlook) are not recorded at
all: only the window is.

## Design

| Piece | What it is | Where |
|---|---|---|
| Personas | 3, each with 5 consecutive workdays and 4–6 threads that run across days | `personas/<id>/` |
| Generator | DeepSeek V4 Flash (`deepseek/deepseek-v4-flash-0731`, OpenRouter with zero data retention, through a local OpenRouter proxy). Neutral steps: persona, day plans, exact text, past chats, memory, questions with needs. Every call is cached with the model id the server reported | `bench/generate.py`, `bench/llm.py`, `personas/<id>/llm-cache/` |
| Simulator | A deterministic, seeded program that turns the plan into the raw DayDream 0.1.6 rows the recorder would have written, with real-world mess added | `bench/simulate.py` |
| Store | An isolated DayDream store, built by DayDream's **own** `MemoryCore` ingest (the 0.1.6 source compiled with the release typing flags) | `swift/bench-store.swift`, `stores/<id>/` |
| Ledger | The ground truth: every segment of activity, every typed unit (including those DayDream can't see, and whether it was really sent), true minutes per app, site and thread, chats with dates, and each question's facts with where they can be found | `personas/<id>/ledger.json` |
| Questions | 9 per persona with need, can_know, reference answer and key facts. Each fact is marked as present in DayDream and/or in native memory, which is computed and checked, not assumed | `personas/<id>/questions.jsonl`, frozen in `lost-thread/questions.lock.json` |
| Native memory | A ChatGPT-style block (saved memories plus recent chat history) and a Claude-style block (the memory summary), derived from the persona's past chats only | `personas/<id>/native_memory.json` |
| Harness | Both contestants × the arms, symmetric prompt, lock check. Each run produces a full JSONL trace | `bench/harness.py`, `runs/<run>/` |
| Grading | A blinded pack with need/can_know, then 3 judges per answer and 2 per swapped pair | `bench/pack.py`, `bench/lt_judge.py`, `judge/`, `regrade.py` |

### Capture rules the simulator follows (DayDream 0.1.6, release flags)

**Native apps**
- An app switch writes `app.activated` then `window.changed`. A title change writes `window.changed`.
- Clicks and focus moves inside a window write rows.
- Idle time, meetings without input and notification banners write **nothing**, so they show up as gaps.

**Chrome**
- Chrome never writes `app.activated`.
- Each page or title change writes one page row. Its url is the origin only, and the link is kept separately.
- Proofs come from the page reader (`chrome-appleevents-page-v1`).
- Search, chat and AI hosts are stored site-only.
- Gmail titles are reduced to the email subject or "Inbox".
- Google result pages are titled with the search words.

**Typing**
- One unit per field: the final text, with any uncorrected typos kept.
- Recorded only where the 0.1.6 typing table allows: Notes, TextEdit, Pages, Obsidian, Terminal, Ghostty, Xcode,
  Cursor, Messages, Mail, WhatsApp, the Claude and ChatGPT apps, and Chrome websites except Google Docs and
  accounts.google.com.
- **Not** recorded in Slack desktop, VS Code, Zoom, Figma, Notion desktop, Word or Outlook. The simulator still logs
  those words in the ledger as `visible_to_daydream: false`.
- Send facts (`surface/field/send/sendBy/to`) are decided by DayDream's own `SendRules` inside the loader. The
  simulator only states what happened.

**Mess, seeded and reproducible**: block start jitter of −3 to +4 minutes and step durations of ±15%; Slack
notification glances in the middle of tasks; short distraction browsing between steps (12%); focus re-emits and
clicks; typos in casual messages; abandoned tabs, emails read but never answered, and work left mid-way, all from the
plan. `load-report.json` lists any row DayDream's ingest refuses (none, or the build stops).

### Typed-words retention

Lost Thread stores are loaded with typed-words retention **30 days** (a user setting: 1 day, 7 days, 30 days or
forever; the default is 7). DayDream's cutoff is rolling (`now − days × 86400`), and the week starts on Tue Sep 29: with
7 days, that day's typed words would start expiring during the ask day itself, mid-run. 30 days keeps the whole week
stable for every run. A person with the default 7 days asking on a Tuesday about the previous Tuesday would see the
same thing until early afternoon.

### Native memory (an approximation)

Neither app's real memory can be loaded into a headless run, so Lost Thread approximates it with a clearly labelled
block written from the persona's **past chats** (the older ones, plus the AI prompts typed during the week):
- **ChatGPT style.** Saved memories, each with the date it was saved, plus "recent conversation history": the
  user's own messages, newest first, each with "N days ago".
- **Claude style.** The memory summary: Work context, Personal context, Top of mind and Brief history.

The memory is assumed to be **fully caught up** with every chat, including yesterday's. Real memory lags by up to a
day and is selective, so the assumption is generous to native memory and conservative for DayDream's uplift. Each
contestant gets its own app's style, and both assume the persona's chats happened in that app.

## Isolation and safety

- **The operator's real DayDream data is never touched.**
  - Every store lives under `stores/`.
  - `bench/common.guard_path` refuses `~/Library/Application Support/DayDream`, `…/Mac Mem` and DayDream containers,
    and `swift/bench-store` refuses any `--home` outside `stores/`.
  - The shipped `mac-mem` (`/Applications/DayDream.app/Contents/MacOS/mac-mem`) always runs with an explicit
    `--home <bench store>`.
  - `HOME` points at `work/agent-home`, so the shipped `mac-mem` can't reach the real store or the real keychain items.
  - `DAYDREAM_TEST_FRESHEN_REQUEST` is set to a bench-only name, so it never wakes the real app.
  - `TZ` is `America/Chicago` and `DAYDREAM_MCP_TOOLSET` is `v2`.
- **No real people.** Every person, company and product in the personas is invented by the generator.
- **Typed words** are sealed with per-day keys exactly as the app seals them. The key ring lives in
  `stores/<id>/typed.key` (mode 0600), never in your Keychain. `bench-store bridge` stands in for the running
  app: it serves the store's typed words over the app's own socket code with "Let AI apps see your typed words" on
  (the shipped default), and writes the recorder heartbeat so `status` says "recording". It writes no activity rows.
- **Access grants.** Each contestant gets its own grant (`mac-mem grant`, client ids `chatgpt` and `claude-code`, as
  Settings › Connections makes them). Capability files are mode 0600 next to the store; the per-run MCP config is
  deleted after each Claude run.
- **Codex isolation.** `--ignore-user-config` (your own `~/.codex/config.toml`, which may connect your real DayDream,
  is never loaded), `--ephemeral`, `-s read-only`, the neutral empty folder, and shell, unified exec, browser,
  computer use, plugins, apps, memories, hooks, image tools and multi-agent off. `default_tools_approval_mode="approve"`
  stands in for the person clicking Approve.
- **Claude isolation.** `--setting-sources project` (no user settings, MCP servers, hooks or plugins),
  `--strict-mcp-config` with only the bench server, `--tools ""` (no built-in tools), `--allowedTools mcp__daydream__*`,
  `--no-session-persistence`, the neutral empty folder.
- **Every trace is checked.** `check_isolation` flags any tool call outside the bench server, tool calls in an arm
  without DayDream, and any non-DayDream tool or MCP server in Claude's init event.
- **No repo changes.** The DayDream repo is unchanged and no `bench/…` branch was needed. The loader compiles a copy
  of the 0.1.6 source (`swift/daydream-0.1.6-src`, commit in `swift/SOURCE_COMMIT`).
- **DeepSeek.** Calls go to the endpoint in `config.json` (override with `LT_GENERATOR_BASE_URL`); the key is read from
  the environment and never written to disk. The frozen lock records the generator as `… @ http://127.0.0.1:8788/v1`, a
  proxy on the machine that generated the data, kept as-is so the lock's hash stays valid; other files say
  `<local OpenRouter proxy>`.

## Caveats

1. **Synthetic events bypass real capture.** The rows go through DayDream's own ingest, sealing, retention and
   MCP-serving code, not through the recorder: no Accessibility or event taps, no page reader, no recorder-side typing
   join, and no failure modes from real apps. Real capture can differ, for example by missing a title or splitting a
   typing unit differently.
2. **Native memory is approximated** from the persona's chats, and assumed caught up (generous to native memory).
   Neither app's real memory, chat search or cross-chat retrieval runs here.
3. **Results date from the model versions used**: `gpt-6.1-sol` (Codex CLI 0.159.2, effort medium) and
   `claude-opus-5-5` (Claude Code 2.1.289), 2026-10-06. Every trace records them.
4. **Notes are paused.** DayDream's moment notes come from a local writer that runs only in the GUI app, so the
   stores have no notes. DayDream's results are a lower bound.
5. **Time is DayDream's estimate.** The 0.1.6 recorder writes rows only on focus, title, click and typing events,
   with no rows for time spent in a window, so long reads and calls without input are undercounted (pilot: 43 true
   Zoom minutes shown as about 18–25). `time_spent` truth comes from the ledger, so this costs DayDream points, as in
   real use.
6. **Send state is never told to AI apps (0.1.6 rule).** DayDream can show that a reply was typed, not that it was
   sent (section "Need and can_know").
7. **The prompts are symmetric, the harnesses are not entirely** (section "Symmetric prompt"): Codex keeps unusable
   tool definitions, Claude Code keeps its SDK prefix line, Environment block and the account's email.
8. **Tool use is the model's own choice.** In the pilot, Codex skipped DayDream in 3 of 12 `native+dd` runs and
   answered from memory alone; that is real behavior and part of the result.
9. **Grading uses Claude (Sonnet 5.5) judges.** The headline therefore compares each model with itself (`native` →
   `native+dd`), and Lost Thread makes no ChatGPT-vs-Claude claims.
10. **The generator writes the questions and needs from the ground truth.** `bench.build` checks every fact against
    the stored rows and memory blocks, and that vague questions share no content word with their target, but it can't
    detect a negative that something else in the week half-answers (the pilot's q09). The can_know review looked for
    this; any later fixture fix goes through `--amend` and the CHANGELOG.

## Raw counts (not scores)

From `python3 -m bench.summarize` (all three runs). "Code hits" are regex/number checks of checkable required facts,
a rough signal before the blinded grading, not a result.

| Arm | Contestant | Runs | Used DayDream | DayDream calls | Mean seconds | Code hits |
|---|---|---|---|---|---|---|
| native | ChatGPT (Codex CLI) | 27 | – | – | 6.0 | 5/31 |
| native | Claude (Claude Code) | 27 | – | – | 5.9 | 4/31 |
| native+dd | ChatGPT (Codex CLI) | 27 | 16 | 84 | 12.0 | 14/31 |
| native+dd | Claude (Claude Code) | 27 | 26 | 119 | 11.3 | 21/31 |
| dd | ChatGPT (Codex CLI) | 27 | 27 | 148 | 17.4 | 18/31 |
| dd | Claude (Claude Code) | 27 | 27 | 143 | 12.6 | 21/31 |

No answer contains a fake tool-call block. Three Claude `native` answers (founder q02, q04, q09) mention the working
folder from Claude Code's Environment block ("my current directory … isn't the project"); the pack replaces the path
with `<folder>`, but the sentence remains.

## Generation notes

- DeepSeek named all three personas "Maya Chen" (a developer at Lumenary Systems, an information-science grad
  student, the founder of Loomly Labs). They are independent people; the name collision is the model's.
  Everyone else is invented too; the student's university is a real one (University of Minnesota).
- DeepSeek often broke the output format (1-based step keys, websites named as apps, bare lists); `bench/generate.py`
  normalizes those shapes and retries with the validator's feedback.
- The build now treats a fact that happened but that DayDream didn't record (typed in Slack desktop, Google Docs,
  VS Code, Figma, Notion; a page DayDream's own ingest refused) as a **coverage gap** (warning, `in_daydream: false`),
  not an error. Only an evidence regex that matches nothing that happened is an error.
- Before the freeze, 5 items were repaired by DeepSeek and 9 hand-fixed by the harness author (wrong day placeholders,
  evidence regexes that matched nothing, a `{min:}` placeholder that named a thread as an app, two references that
  contradicted the ledger, two "negatives" that weren't, one control the week contradicted). Each change, with the item
  as it was before, is in `personas/<p>/generated/repairs.json`. Annotation drafts by DeepSeek overstated what was
  recorded for 3 founder items; those `can_know` lines were rewritten (`edited: true` in `annotations.json`).

