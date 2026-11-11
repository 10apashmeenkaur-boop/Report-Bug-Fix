# Acadza Report → Fix

Turns messy student error reports ("ans galat hai", "option D missing") into safe, surgical fixes, and gives faculty a console to approve them in one click.

## Run

```bash
pip install beautifulsoup4 lxml          # only dependencies
export GEMINI_API_KEY=your_key           # free: aistudio.google.com → Get API key
python run.py                            # → results.json, out/console.json, out/bonus_findings.json
python eval/eval.py                      # score vs our hand-solved labels
python -m tests.test_decisions           # decision logic (fake LLM evidence)
python -m tests.test_guard               # safety guard
```

`python run.py --provider none` runs rules-only (no AI). Every LLM call is cached in `cache/llm/`, so reruns are free and reproducible.

## How it works

```
reports → group by question → normalise HTML/MathML
  → A. rules (code)          exam tags, raw LaTeX, blank/missing/duplicate options, missing figure,
                             malformed keys, missing unit, already-fixed history, injection, gibberish
  → B. claim (LLM)           what is the student claiming? comment fenced as untrusted data
  → C. evidence (LLM)        audit of the full record + TWO BLIND SOLVES (no key, no solution, no comment)
  → D. decision (code)       verdict / repair / route from a decision table — the LLM never picks the route
  → E. edit (code + LLM)     smallest find/replace; values must come from the record, never invented
  → F. guard (code)          scope, auto:false, confidence floor, edit applies exactly once, words-only checks
  → G. reply (LLM)           1–3 kind sentences, matched to what actually happened
```

| Folder | |
|---|---|
| `engine/` | normalize, rules, llm, prompts, pipeline, edits, guard |
| `tests/` | decision tests (29) and guard tests (11) |
| `eval/` | our hand-solved labels for all 44 reports + scorer |
| `out/` | console.json (UI data), bonus_findings.json, llm_trace.jsonl |
| `web/` | faculty console |

Note: the live console loads precomputed `out/console.json`; it does not call an LLM at view time.

## Faculty console (`web/`)

Static page, no build step: `index.html`, `styles.css`, `app.js`, and `data.js` (written by `run.py`).
Open `web/index.html` directly, or deploy the `web/` folder to Vercel (Framework: Other, Root directory: `web`).
`index.html?demo` shows design fixtures built from the test doubles, used only for designing before the real run.

- The queue is grouped by question and route (Needs your decision → Fixed automatically → No change), ordered by urgency inside each group.
- Answer-key changes are shown as OMR bubbles; text changes as a before/after on the printed question with the changed words marked.
- Evidence shows both blind solves, the stored solution and the key, with ✓/✗ against the key, plus any safety-rule downgrade.
- `A` approve, `E` edit, `R` reject, `J`/`K` next/previous. Every action is written to an audit log (localStorage, downloadable as JSON).
