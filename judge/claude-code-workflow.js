export const meta = {
  name: 'lost-thread-judge',
  description: 'Grade Lost Thread benchmark answers: blinded per-answer rubric + swapped-order pairwise, Sonnet judges',
  phases: [{ title: 'Grade answers', model: 'sonnet' }, { title: 'Pairwise', model: 'sonnet' }],
}
const PACK = args.pack
const SHARDS = args.shards || 4
const DONE = { type: 'object', properties: { written: { type: 'integer' }, file: { type: 'string' } }, required: ['written', 'file'] }

const RUBRIC = `You are grading answers an AI assistant gave to a person asking about their own recent workweek.
You do NOT know which assistant or setup wrote an answer; do not guess. Grade only what the answer says.
Read ${PACK}/items.json for each item: question, reference (what really happened), facts, negative (true if the asked thing did NOT happen), hallucination (what a made-up answer would claim), and, if present, need (what the person is trying to accomplish) and can_know (what the activity history can and cannot know). If need is absent, infer it from the question in one line.

Score each answer:
- correct (0-2): 2 = the main thing asked is right; 1 = partly right or hedged; 0 = wrong, missing, or generic.
- helpful (0-4), judged against the person's need: 0 = misleading or confidently wrong; 1 = non-answer ("I don't have access", generic advice); 2 = partly useful, person still has to dig; 3 = answers the need; 4 = answers it AND gives what they need for the next step (where/when, what's left to do). Confidently wrong always scores 0, below a non-answer. Length is not value: a short correct answer can be a 4; needless detail, padding or long caveats lower the score.
- fabrications (count): material false claims about the person's week stated as fact (wrong number/person/day, events that didn't happen). Clearly marked guesses and correct "I don't know" don't count.
- overclaim (0/1): 1 if it states as known something the history could not know per can_know (e.g. that a message was or wasn't sent when sends aren't recorded), even if it happens to be true.
- negative_verdict (negative items only, else null): "correct" (says it didn't happen / no sign of it), "unsure", "wrong" (says it happened).
- note: one short sentence.
Output one JSON object per line, nothing else per line.`

const PAIR = `You are comparing two answers an AI assistant gave to the same question from a person about their own recent workweek. You don't know which setup wrote which; do not guess.
Read ${PACK}/items.json for the item's question, reference (what really happened), and need if present (else infer it in one line).
For each pair pick the answer the PERSON would rather receive, given what really happened and their need: correctness first (a confidently wrong answer loses to an honest "I don't know"), then how directly it lets them act. Do not prefer length or detail for its own sake; prefer the shorter one when they are equally useful. Answer "tie" only if they are genuinely equal.
Output one JSON object per line: {"pair_id": "...", "winner": "1" | "2" | "tie", "reason": "one short sentence"}.`

phase('Grade answers')
const shards = Array.from({ length: SHARDS }, (_, i) => i + 1)
const gradeJobs = []
for (const k of shards) for (const j of [1, 2, 3]) gradeJobs.push({ k, j })
const pairJobs = []
for (const k of shards) for (const j of [1, 2]) pairJobs.push({ k, j })

const [g, p] = await Promise.all([
  parallel(gradeJobs.map(({ k, j }) => () => agent(
    `${RUBRIC}\n\nGrade EVERY answer in ${PACK}/judge/answers-${k}.jsonl (one JSON per line: blind_id, item_id, answer). Do not read any other file in ${PACK} except items.json; never open mapping.json, pairs_key.json, code_checks.json or other judges' outputs.\nWrite your grades to ${PACK}/judge/out/grade-j${j}-s${k}.jsonl, one line per answer: {"blind_id":"...","item_id":"...","judge":"j${j}","correct":0,"helpful":0,"fabrications":0,"overclaim":0,"negative_verdict":null,"note":"..."}. Then return how many lines you wrote.`,
    { label: `grade s${k} j${j}`, phase: 'Grade answers', model: 'sonnet', effort: 'low', schema: DONE }))),
  parallel(pairJobs.map(({ k, j }) => () => agent(
    `${PAIR}\n\nJudge EVERY pair in ${PACK}/judge/pairs-${k}.jsonl (fields: pair_id, item_id, answer_1, answer_2). Do not read any other file in ${PACK} except items.json; never open mapping.json, pairs_key.json or other judges' outputs.\nWrite to ${PACK}/judge/out/pair-j${j}-s${k}.jsonl, adding "judge":"p${j}" to each line. Then return how many lines you wrote.`,
    { label: `pairs s${k} p${j}`, phase: 'Pairwise', model: 'sonnet', effort: 'low', schema: DONE }))),
])
return { grades: g.filter(Boolean), pairs: p.filter(Boolean) }
