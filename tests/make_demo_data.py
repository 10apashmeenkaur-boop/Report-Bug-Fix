"""DEV ONLY: builds web/demo-data.js from the fake-LLM test fixtures so the UI can be designed
before the real Gemini run exists. Never deployed; the real console reads web/data.js."""
import json, time
from pathlib import Path
from tests.test_decisions import CASES, FakeEngine, Q, R, TAX
from engine.guard import urgency

ROOT = Path(__file__).resolve().parent.parent
by_q = {}
for rid in CASES:
    by_q.setdefault(R[rid]["questionId"], []).append(rid)
cases = []
for qid, rids in by_q.items():
    eng = FakeEngine(CASES[rids[0]])
    _, case = eng.run_question(Q[qid], [R[r] for r in rids])
    cases.append(case)
cases.sort(key=lambda c: -c["urgency"])
bonus = json.loads((ROOT / "out/bonus_findings.json").read_text(encoding="utf-8"))
meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "provider": "DEMO (fake test evidence)", "model": "-", "llm_calls": {}}
(ROOT / "web/demo-data.js").write_text("window.CONSOLE_DATA = " + json.dumps({"meta": meta, "taxonomy": TAX, "cases": cases, "bonus": bonus}, ensure_ascii=False) + ";\n", encoding="utf-8")
print(len(cases), "demo cases")
