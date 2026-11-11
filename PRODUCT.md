# PRODUCT.md

## Who loses the most when a question is broken

**The student who answers it correctly and is marked wrong.** They lose marks silently, and worse, they start doubting reasoning that was right, often the night before a mock. A wrong key keeps doing this to every student who attempts the question until someone fixes it. Faculty lose next: every bad question spends credibility they built in class.

How the design reflects that:
- **Urgency = how badly it hurts × how many students it has already hit.** Wrong keys and wrong question types sit at the top; spelling sits at the bottom. A typo costs nobody marks.
- **Proven fixes go live immediately, without waiting for a 10 PM queue.** A wrong key is auto-fixed only when two blind solves *and* the stored solution agree.
- **The reply tells a student when they were right**, in plain words, so the confidence they lost comes back. When they were wrong, it shows the one step they missed instead of saying "wrong".
- **Duplicate reports collapse into one case**, so faculty fix a question once, not three times.

## What I would never let the AI auto-apply

- **Anything authored:** a new distractor (R009/R010's missing option D), a diagram, a rewritten question. The AI may only move values that already exist in the record.
- **Judgement calls:** chapter moves, syllabus decisions, and "no valid option" (unsound) questions.
- **An answer-key change backed by one source.** One solve, or the student's word, is never enough. The student's comment is a lead, never evidence. R004's student was wrong about the number even though the key really was wrong.
- **Anything a student's comment tells the system to do.** R027's "IGNORE ALL PREVIOUS INSTRUCTIONS" changed nothing.

These are enforced in code (`engine/guard.py`), not in a prompt, so no model output can talk its way past them.

## One metric for week 1

**Undo rate on auto-applied fixes**: the share of auto-applied fixes that faculty undo in the console. Target under 2%. It is the one number that says whether automation can be trusted. If it rises, the confidence floor goes up and more fixes route to faculty. Watched next to it: **hours from first report to fix for answer-key defects**, since that is how long students keep losing marks.

## One idea beyond the brief

**Find broken questions before any student does.** The same audit that triages reports flagged defects nobody reported, in a 43-question sample (see "Unreported issues" in the console). Two cheap ways to scale it to 8 lakh questions:
1. Run the rule checks (free, deterministic) across the whole bank tonight, then the AI audit on the most-attempted questions first.
2. Use attempt data as an alarm: when strong students "fail" a question at an unusual rate, or most of them pick the same non-key option, the key is probably wrong. Re-solve it blind before the first report arrives.
