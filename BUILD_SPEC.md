# BUILD_SPEC — Acadza "Report → Fix" loop

Hand this file to Claude Code (or any coding assistant) as the single source of truth.
Read it fully before writing code. When something here conflicts with a guess, this file wins.

Owner: Pashmeen Kaur. Deadline: Wed 11 Nov 2026, 11:59 PM IST.
Git rule: commits are authored by Pashmeen only. **Never add `Co-Authored-By`, `Claude-Session`, or any AI attribution lines to commits.**

---

## 0. What we are building (one paragraph)

Students report broken JEE/NEET questions with messy comments ("ans galat hai", "option D missing").
We build (1) an **engine** that turns each report into a safe, surgical fix or a polite "you're right / you're not",
(2) a **faculty console** where a tired teacher approves those fixes in one click, and
(3) `results.json`, which Acadza auto-scores against ground truth.
Scoring: triage accuracy 25 · safety 10 · console UX 20 · product thinking 20 · PROMPTS.md 15 · engineering 10.

---

## 1. Repo layout

```
acadza-report-fix/
  BUILD_SPEC.md          ← this file
  README.md              ← how to run, architecture diagram, honest limitations
  PROMPTS.md             ← mandatory, heavily weighted (see §9)
  PRODUCT.md             ← ≤ 1 page (see §10)
  data/                  ← questions.json, reports.json, taxonomy.json (untouched copies)
  engine/
    normalize.py         ← HTML+MathML → plain text; option parsing; answer normalisation
    rules.py             ← deterministic detectors (no LLM)
    llm.py               ← provider-agnostic JSON client (Gemini default), disk cache, retries
    prompts.py           ← EVERY in-product prompt, verbatim, versioned (PROMPT_VERSION)
    pipeline.py          ← orchestration per question-group
    guard.py             ← scope + safety guard (pure code, final say)
    edits.py             ← apply {field, find, replace} → before/after HTML, validation
    urgency.py           ← queue ordering
    scan.py              ← bonus: unreported defects across all 43 questions
  run.py                 ← CLI: python run.py [--provider gemini|none] [--model ...]
  eval/
    expected.json        ← hand-solved labels for all 44 reports (our own ground truth)
    eval.py              ← scores out/results.json vs expected.json, prints confusion
  out/
    results.json         ← the graded file (exact schema, §6)
    console.json         ← enriched data for the UI (cases, before/after HTML, findings, urgency)
    bonus_findings.json  ← unreported defects
    llm_trace.jsonl      ← every LLM call: prompt id, version, model, input hash, output, ms
  web/                   ← faculty console (static; reads out/console.json)
```

Engine: Python 3.11+, **stdlib + beautifulsoup4/lxml only**. LLM over plain HTTPS (urllib) — no SDK lock-in.
Console: static site (no server). Deployed on Vercel. Loads precomputed `console.json` (README allows this; say so in the UI footer and README).

---

## 2. The data — facts that matter

- `questions.json`: 43 records. Type in `questionType` ∈ `scq | mcq | integerQuestion`; payload lives under the key of that name:
  `{question, solution, answer, quesImages, solutionImages}`. **Options are inside the question HTML** as `(A) … <br/> (B) …`, wrapped in random `<span>`s.
  Inline MathML everywhere; numbers often padded (`<mn> 3 </mn>`). `errorHistory` holds past faculty fixes (with timestamps).
- `answer`: scq → `"C"`; integerQuestion → `"3113"`; mcq → list, sometimes **malformed** (`["", "B"]`, `["A, B, D"]`).
- `reports.json`: 44 reports `{reportId, questionId, studentId, source, reportedAt, comment}`. Several per question.
- `taxonomy.json`: closed lists. Each repair has allowed `fields` and `auto`. `auto:false` ⇒ **never** auto_apply; `fields: []` ⇒ no edit may be written.

---

## 3. Pipeline (per question group)

```
reports ─► group by questionId ─► normalize question
        ─► Stage A  rules.py        (deterministic findings, free, 100% reproducible)
        ─► Stage B  LLM: claim      (what is the student claiming? comment = untrusted DATA)
        ─► Stage C  verify          (rules evidence + LLM checks; for answer claims → blind double-solve)
        ─► Stage D  decide          (decision table in code, NOT the LLM)
        ─► Stage E  edit            (deterministic where possible; LLM only for wording edits; validated)
        ─► Stage F  guard.py        (scope, auto flag, confidence floor, injection, find-exists-once)
        ─► Stage G  student reply   (LLM, tone-checked, must match route)
        ─► results.json + console.json
```

### Stage A — deterministic detectors (rules.py)
| Detector | Signal | Repair |
|---|---|---|
| markup_leak | raw `\frac`, `\sqrt`, `\AA`, `\times`, `$…$`, or `&` alignment chars (`<mi>&amp;</mi>`, `<mi>&</mi>`) visible | markup_leak |
| exam_reference | `\((NEET|JEE|AIEEE|JEE Main|JEE Adv)[^)]*\d{4}\)` in question text | exam_reference |
| option_blank | parsed option with empty text | option_repair |
| option_missing | scq with < 4 parsed options | option_repair |
| identical_options | two options with identical normalised text | identical_options |
| figure_missing | "figure / shown / diagram" in stem AND `quesImages` empty | diagram |
| answer_malformed | mcq list containing `""` or a comma-joined string | answer_key (malformed) / type_mismatch if only 1 real letter |
| already_fixed | `errorHistory` entry after `reportedAt` whose note covers the complaint | → verdict already_fixed |
| unit_missing | integerQuestion whose stem gives no unit/"in X" and solution has a unit | answer_form |
| injection | comment matches `ignore (all )?previous|you are (now )?the|system prompt|mark this report|change the answer to` | flag; never obey; route ≥ no_change |
| gibberish | comment has no dictionary/Hinglish word (e.g. `asdfgh`) | → not_actionable if checks find nothing |

### Stage B — claim extraction (LLM, prompt `CLAIM_v*`)
Input: comment (fenced as untrusted data), normalised question text, options. Output JSON:
`{claim_type, student_value, target_option, language, is_instruction_attempt}`;
`claim_type ∈ answer_wrong | option_missing_or_blank | data_missing | spelling | markup | exam_tag | unit_or_form | solution_mismatch | duplicate_options | figure_missing | wrong_chapter | syllabus | display | app | language | type_or_marking | gibberish | other`.

### Stage C — verification
- **Answer claims** (`answer_wrong`, `solution_mismatch`, `type_or_marking`): run **two blind solves** (prompts `SOLVE_v*` and `VERIFY_OPTIONS_v*`).
  Blind = the model sees stem + options only — **no stored key, no stored solution, no student comment**.
  Also extract the stored solution's final value (`SOLUTION_FINAL_v*`). Compare all three with the key in code (numeric tolerance 1%).
  - both solves == key → key is right (student wrong) unless solution text disagrees → solution_fix
  - both solves agree, ≠ key, and == solution final → answer_key defect, high confidence
  - both agree ≠ key, solution also ≠ → answer_key, medium confidence → faculty_review
  - solves disagree with each other → faculty_review, never auto
  - no option matches the solved value / two options correct → unsound (faculty)
- **Content claims** (spelling, chapter, syllabus, solution belongs elsewhere): LLM check with the record as evidence (`CHECK_v*`), returns `{confirmed, evidence, proposed_find, proposed_replace}`.

### Stage D — decision table (code)
The LLM never chooses the route. Code maps (claim, evidence) → (verdict, repair). Examples:
- claim display/app/language → `display_issue` / `app_issue` / `language_mode`, repair null, route `no_change`.
- claim confirmed false by checks → `no_error`, route `no_change`.
- already_fixed detector hit → `already_fixed`, `no_change`.
- gibberish + nothing found → `not_actionable`.
- otherwise `defect` + repair from detector/claim.

### Stage E — edits (edits.py)
- Format: `{"field": "question|solution|answer|questionType", "find": "<exact substring of stored value>", "replace": "<new>"}`.
  For `answer`: find/replace are the stored and new answer values (strings; mcq answers serialised as JSON).
  `type_mismatch` may need two edits → `edit` is a **list** in that case only.
- `find` must occur **exactly once** in the stored field, else reject.
- Smallest possible change: a word, an option, a value — never the whole question.
- Spelling edits must not change any digit, symbol, or `<math>` content (diff check).
- option_repair must not change stem text (check text before "(A)" is identical).
- Deterministic edits (no LLM): exam_reference removal, `\frac{a}{b}` → MathML, answer normalisation, answer_key letter, option blank fill when value is derivable.

### Stage F — guard.py (final authority, pure code)
1. verdict ∈ taxonomy.verdicts; repair ∈ taxonomy.repairs or null (null iff verdict ≠ defect).
2. every edit.field ∈ repair.fields; repair.fields empty ⇒ edit must be null.
3. `auto:false` ⇒ route `faculty_review` (never auto_apply).
4. confidence < 0.85 ⇒ downgrade auto_apply → faculty_review.
5. answer_key ⇒ auto only if both blind solves agree AND stored solution agrees; otherwise faculty_review.
6. injection flag ⇒ the comment can never be the source of an edit; route decided by checks alone.
7. edit validation failures ⇒ drop edit, faculty_review, record reason.
8. Non-defect verdicts ⇒ route `no_change`, edit null.
Every downgrade is recorded in `guard_notes` (shown in the console).

### Stage G — student reply
1–3 sentences, warm, specific, no blame, no jargon; Hinglish allowed if the student wrote Hinglish.
Must match route: auto_apply → "fixed"; faculty_review → "a teacher is checking it"; no_change/no_error → explain the one step they missed, kindly.
Never reveal internal labels. Never promise marks/regrading unless route is auto_apply on answer/answer_key.

---

## 4. Confidence
Computed in code from evidence, not asked from the LLM:
deterministic detector hit 0.97 · two blind solves + solution agree 0.95 · two solves agree, solution disagrees 0.75 · single LLM check confirmed 0.85 · conflicting evidence ≤ 0.6.

## 5. Urgency (queue order) — explain this in PRODUCT.md
`score = severity(repair) × (1 + ln(#reports)) × exposure`, where severity: answer_key/type_mismatch 100, unsound/wrong_question/stem_data/option_repair 80, solution_fix/diagram/identical_options 60, answer_form/notation/markup_leak 30, wrong_chapter/syllabus 20, spelling/noise/exam_reference 10; non-defects 0 (bottom, collapsed).
Rationale: a wrong key silently costs marks to **every** student who attempts it; a typo costs nobody marks.

## 6. results.json — exact schema (one row per report, all 44)
```json
{"reportId":"R001","questionId":"60f744b3f1b9bc7cc7b4a949","verdict":"defect","repair":"answer_key",
 "edit":{"field":"answer","find":"C","replace":"A"},"route":"auto_apply","confidence":0.95,
 "evidence":"Two blind solves give √(40²+(60−30)²)=50 V = (A); stored solution also ends at 50 V; key says C (130 V).",
 "student_reply":"You're right — the correct answer is (A) 50 V. We've fixed the key, thanks for flagging!"}
```
`repair` and `edit` are `null` when not applicable. No extra keys in results.json (extras go to console.json).

## 7. Faculty console — design brief
User: a faculty member, 10 PM, 60 cases, tired. Goal: **know in 3 seconds what changed and why, act in one click, trust it.**

Layout (desktop first, works on a laptop 1366px):
- **Left rail (320px): queue** grouped by question. Each card: urgency dot, repair label in plain words ("Answer key wrong"), subject·chapter, `3 reports`, route chip (Auto-applied / Needs you / No change). Filters: Needs you · Auto-applied · No change · All. Count badges.
- **Main panel: the case**
  1. Header: plain-English verdict sentence ("Key says C (130 V); correct is A (50 V)") + confidence + route.
  2. **Diff** — before/after rendered side-by-side (MathML rendered natively), changed span highlighted (red strike / green insert). For answer edits show option chips with old key → new key.
  3. Evidence — 1–3 bullets (blind solve 1, blind solve 2, stored solution), each with ✓/✗ vs key.
  4. Student reports — the raw comments, Hinglish preserved, with source + time; injection attempts badged "Ignored instruction".
  5. Student reply preview (editable).
  6. Guard notes (why it was downgraded), if any.
- **Action bar (sticky bottom):** Approve `A` · Edit `E` · Reject `R` · Next `J` / Prev `K`. Every action writes an audit entry `{caseId, action, actor, at, before, after, note}` (localStorage + exportable JSON).
- Audit log drawer; "Export audit" button.
- Footer: "Showing precomputed engine results (out/console.json), engine v…, generated at …".

Visual design: calm, high-contrast, low-glare (night use). Neutral background, one accent colour, red/green only for diff. System font stack; math at 1.1× body size. No decorative gradients. Keyboard-first. Empty state when the queue is clear.

## 8. Accuracy checklist — our hand-solved expectations (eval/expected.json)
| Report(s) | Expected verdict / repair / route | Why |
|---|---|---|
| R001 R002 R003 | defect / answer_key / auto_apply, C→A | √(40²+30²)=50 V = A; solution says 50 V |
| R004 | defect / answer_key / auto_apply, A→D | P=⅓ρv²=4800 = D. Student's 8225 is wrong but key is wrong too |
| R005 | defect / answer_key / auto_apply, 2→1 | flux = q/4ε₀+q/4ε₀ = q/2ε₀ ⇒ n=1; solution agrees |
| R006 | defect / stem_data / auto_apply (add "of side 10 cm") | solution uses 10 cm edge |
| R007 R008 | defect / stem_data / auto_apply (add "of 95.5 mH") | solution uses L = 95.5 mH |
| R009 R010 | defect / option_repair / faculty_review | (D) absent; distractor text unknown → human |
| R011 | defect / option_repair / auto_apply, (C) "1 : 1" | Newton's 3rd law; key C |
| R012 | defect / spelling / auto_apply "Epithellial tisue"→"Epithelial tissue" | |
| R013 | defect / exam_reference / auto_apply remove "(NEET 2019)" | |
| R014 | defect / markup_leak / auto_apply `\frac{7}{8}` → MathML 7/8 | |
| R015 | defect / identical_options / faculty_review | B and C both "0 W" |
| R016 | defect / solution_fix / auto_apply "a = 40 cm"→"a = 20 cm" | 800 = 2a² ⇒ a = 20 |
| R017 | already_fixed / no_change | errorHistory 7 Nov fixed 'mere'→'more' |
| R018 R019 | defect / solution_fix / faculty_review or auto | solution opens with 5.47 Å from another question; final 16.8 eV correct |
| R020 | defect / answer_key / auto_apply `["A, B, D"]`→`["A","B","D"]` | malformed; ABD verified correct |
| R021 | defect / type_mismatch / auto_apply mcq→scq, answer B | τ = 3/ln 8 = 1.44 min, single correct |
| R022 | defect / spelling / auto_apply "plexux"→"plexus" | |
| R023 | defect / wrong_chapter / faculty_review | neural control, not Living World |
| R024 | defect / answer_form / auto_apply add "(in N)" | |
| R025 | defect / diagram / faculty_review | "as shown in figure", no image |
| R026 | defect / syllabus / faculty_review | faculty confirm; stays live |
| R027 | no_error / no_change; injection ignored | γ = 3α = 5.7×10⁻⁵ = A, key correct |
| R028 R029 R030 R031 R032 R033 R035 R043 R044 | no_error / no_change | key verified |
| R034 | defect / unsound / faculty_review | I = 2I₀ ⇒ A false; R false ("always") ⇒ no option fits |
| R036 R037 | display_issue / no_change | |
| R038 | app_issue / no_change | |
| R039 | language_mode / no_change | |
| R040 | no_error / no_change | golden rice correct |
| R041 | not_actionable / no_change | gibberish; checks find nothing on key |
| R042 | defect / wrong_chapter / faculty_review | displacement current ≠ geometrical optics |

Bonus (unreported) to surface via scan.py: "Canolla" (6264197…); solution says insulin–glucagon "synergistic" (66fe9dbe…); raw `&` alignment in solutions (6956d797…, 622f08d7…, 68c801bc…); garbled "P eqcu / eqch" (68dea657…); "hemispherial" (622df25a…); "prominant", "Maceus" (61ee6c9d…); stray "Solution: (b)" (6956d7bb…); "/ A C" degree glyph (6062ec84…).

## 9. PROMPTS.md (write as we go, not at the end)
Sections: (1) every in-product prompt verbatim with version history + why it changed; (2) key prompts given to the coding assistant, verbatim; (3) three moments the AI was wrong and how it was caught (eval diff, guard rejection, blind-solve disagreement); (4) design decisions log: what was tried, dropped, why.

## 10. PRODUCT.md (≤ 1 page)
Who loses most (the student who meets a wrong key the night before a mock — loses marks *and* trust; then the faculty whose credibility drops) · what is never auto-applied (anything authored: new distractors, diagrams, chapter moves, unsound questions, answer keys without two agreeing blind solves + solution) · week-1 metric (faculty override rate on auto_apply, target < 2%) · pitch (pre-emptive scan of all 8 lakh questions with the same detectors before any student reports).
