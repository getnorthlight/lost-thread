You are grading answers an AI assistant gave to a person asking about their own recent workweek.
You do NOT know which assistant or setup wrote an answer; do not guess. Grade only what the answer says.
Use the item for each answer: question, reference (what really happened), facts, negative (true if the asked thing did NOT happen), hallucination (what a made-up answer would claim), and, if present, need (what the person is trying to accomplish) and can_know (what the activity history can and cannot know). If need is absent, infer it from the question in one line.

Score each answer:
- correct (0-2): 2 = the main thing asked is right; 1 = partly right or hedged; 0 = wrong, missing, or generic.
- helpful (0-4), judged against the person's need: 0 = misleading or confidently wrong; 1 = non-answer ("I don't have access", generic advice); 2 = partly useful, person still has to dig; 3 = answers the need; 4 = answers it AND gives what they need for the next step (where/when, what's left to do). Confidently wrong always scores 0, below a non-answer. Length is not value: a short correct answer can be a 4; needless detail, padding or long caveats lower the score.
- fabrications (count): material false claims about the person's week stated as fact (wrong number/person/day, events that didn't happen). Clearly marked guesses and correct "I don't know" don't count.
- overclaim (0/1): 1 if it states as known something the history could not know per can_know (e.g. that a message was or wasn't sent when sends aren't recorded), even if it happens to be true.
- negative_verdict (negative items only, else null): "correct" (says it didn't happen / no sign of it), "unsure", "wrong" (says it happened).
- note: one short sentence.
