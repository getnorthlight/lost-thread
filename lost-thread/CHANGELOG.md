# Lost Thread changelog

Every change to the frozen set after the freeze, with the items rerun for every arm and contestant.

- 2026-10-06T15:34:34.511959-05:00: v1 frozen, 27 items, sha256 204b00dd8a3963b35c4ef7ad2b5aecd5fad95fe8653eaeb6fd21dc49a113f33c. No contestant run before this.

Before the freeze (no contestant had run): DeepSeek repaired developer q03, q08 and founder q07, q08, q09; the harness
author hand-fixed date placeholders, evidence regexes, optional flags and two references (developer q08, founder q09)
to match the ground truth. Every change, with the item as it was before, is in
`personas/<persona>/generated/repairs.json`; the reviewed can_know lines are in `generated/annotations.json`.

## Public release

- 2026-10-06: published as v1. Nothing in the frozen set changed (the lock still verifies: `python3 -m bench.freeze
  --verify`). For the public copy, the run logs had two local paths replaced: the home folder in Claude Code's
  `memory_paths` (`/Users/<user>/`) and the store path in `runs/*/inputs/load-report.json` (`<lost-thread>/`). Outside the lock, the generator's local
  proxy address is written as `<local OpenRouter proxy>`. Raw
  answers, tool calls and grades are unchanged.
