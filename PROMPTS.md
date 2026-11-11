# PROMPTS.md

> Draft, being written as we build. Final version adds the real-model eval numbers and verbatim prompt history.

## 1. Prompts inside the product

All live in `engine/prompts.py`, verbatim and versioned. Summary:

| Prompt | Job | Sees key? | Sees solution? | Sees student comment? |
|---|---|---|---|---|
| `CLAIM_v2` | Classify what each student claims (18 claim types), detect instruction attempts | no | no | yes, fenced as untrusted data |
| `SOLVE_v2` | Solve from scratch | **no** | **no** | **no** |
| `VERIFY_v2` | Second examiner, judges each option separately (assertion and reason separately) | **no** | **no** | **no** |
| `AUDIT_v3` | Find content defects; quote exact text so code can locate it | yes | yes | no |
| `REPLY_v2` | Student reply, matched to the final route | outcome only | no | yes |

Version history:
- `CLAIM_v1 → v2`: added the Hinglish glossary and the security paragraph after deciding comments must never be instructions (R027 "IGNORE ALL PREVIOUS INSTRUCTIONS").
- `SOLVE_v1 → v2`: added `NONE`/`MULTIPLE` choices. Without them a solver is forced to pick an option even when none fits (R034).
- `AUDIT_v1 → v3`: v1 asked "is anything wrong?" and got essays. v2 split it into fixed checks. v3 added "quote EXACTLY", because code must find the quote in the stored HTML or the edit is dropped.

## 2. Prompts I gave the coding assistant (key ones, verbatim)

1. *"Read the README, taxonomy, all 44 reports and all 43 questions. Before writing code, solve every report by hand and tell me the right verdict for each, so we have something to check the AI against."*
2. *"Build Option 4: rules first, AI only where needed, two blind solves for answer keys, and a code guard with the final say. The LLM gathers evidence; code decides the route."*
3. *"Write a BUILD_SPEC.md precise enough that another coding assistant could build this without asking questions: architecture, exact results.json schema, guard rules, console design, expected answers."*
4. *"Write tests with fake LLM outputs for every tricky report so we can test the decision logic without spending API calls."*

## 3. Three moments the AI was wrong, and how I caught it

1. **"Assertion (A)" read as option A.** The first option parser split on every `(A)`, so for the assertion-reason question the stem became just "Assertion" and option A became the assertion text. I caught it by printing every parsed question before going further. Fix: options start at the *last* `(A)` that is followed by a `(B)`.
2. **The guard blocked a correct fix.** Removing "(NEET 2019)" was rejected by our own "words-only edits may not change numbers" rule, because 2019 is a number. The rules-only dry run showed R013 going to faculty for no reason. Fix: digits inside the removed tag are allowed to disappear; any other digit change is still blocked.
3. **R033 (3113 vs 3112.99) flagged as a broken solution.** The stored solution stops at A = 311.3 while the key is 10A = 3113. The first decision logic called that "solution reaches a different answer". The decision test caught it. Fix: a pure unit/scale difference (×10ᵏ) is treated as the same quantity. The same applies to R032 (0.08 m vs 8 cm).

## 4. Design decisions log

| Decision | Tried / dropped | Why |
|---|---|---|
| Rules before AI | One-big-prompt baseline dropped | Blank options, LaTeX leaks, malformed keys and exam tags are provable by code: free, deterministic, 100% repeatable. The AI only handles what needs judgement. |
| Code decides the route, not the LLM | Letting the LLM output `route` | A model can be talked into "auto_apply" by a student comment. A decision table can't. |
| Two *blind* solves for answer claims | Showing the solver the student's value | The student's number anchors the model. R004's student says 8225, which is wrong, yet the key is also wrong (4800 = D). A blind solver gets 4800 either way. |
| Answer key auto-applies only if both solves AND the stored solution agree | Auto on one solve | The highest-stakes edit gets three independent signals. Otherwise a teacher decides. |
| Values in fixes must come from the record | Letting the AI "fill in" missing data | R007's inductance (95.5 mH) is taken from the stored solution and code checks the number is there. A missing *distractor* (R009/R010) is never invented; faculty writes it. |
| Format-only key fix auto-applies | Requiring solves for R020 | `["A, B, D"]` → `["A","B","D"]` keeps the same set of letters; code proves the meaning is unchanged. |
| Confidence computed in code | Asking the LLM for a confidence number | Self-reported LLM confidence is poorly calibrated. Ours comes from which evidence agreed. |
| Cache every LLM call | — | Reruns are free, reviewers can reproduce results, and it stays within the free Gemini tier (~10 requests/min). |

Rules-only dry run (no AI): 17/44 fully correct, **0 unsafe auto-applies**. Everything the rules can't prove goes to a teacher.
