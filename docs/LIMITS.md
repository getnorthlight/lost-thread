# Limits

- **Synthetic weeks.** The events go through DayDream's own ingest and serving code, not through real capture on a
  real Mac. This measures answering from a recorded history, not how well anything records.
- **Claude judging Claude.** See [Re-grade with your own judge](REPRODUCE.md#re-grade-with-your-own-judge). No claims are made
  about ChatGPT versus Claude.
- **More made-up facts with DayDream.** 2 of 27 answers in each DayDream setup contained one, against 0–1 without.
  Answers that decline can't make anything up.
- **Memory is approximated.** Neither app's real memory can run headless, so each gets a saved-memory block written
  from the persona's earlier chats, assumed fully up to date, which is generous to the no-DayDream setups.
- **Small sample.** 27 questions per setup; the intervals above are wide.

More in [docs/DESIGN.md](DESIGN.md#caveats).
- **Head-to-head reasons are thin.** The pairwise judges mostly wrote boilerplate reasons. Their picks agree with the
  per-answer scores in 146 of the 152 verdicts where those scores differed.
- **Same name, real school.** The generator named all three personas "Maya Chen". The student's university is real,
  and so is the domain of her invented email address; both are inside the hashed questions, so we left them as-is.
- **Reruns on another Mac are untested.** Scoring and re-grading need only Python. Rerunning the contestants needs a
  Mac with DayDream, Codex CLI and Claude Code, and nobody has done that outside our setup yet.
