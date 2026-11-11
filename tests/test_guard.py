"""The guard must hold even if the LLM (or a student) produces something malicious or sloppy.

    python -m tests.test_guard
"""
import json
from pathlib import Path

from engine.guard import enforce
from engine.normalize import load_all

ROOT = Path(__file__).resolve().parent.parent
Q = load_all(ROOT / "data/questions.json")
TAX = json.loads((ROOT / "data/taxonomy.json").read_text(encoding="utf-8"))
LCR = Q["60f744b3f1b9bc7cc7b4a949"].raw          # key C
EPI = Q["61dffbec08489156b684bd22"].raw          # 'Epithellial tisue'


def base(**kw):
    d = {"verdict": "defect", "repair": "spelling", "edit": None, "route": "auto_apply", "confidence": 0.95, "guard_notes": []}
    d.update(kw)
    return d


CHECKS = [
    ("spelling repair may not touch the answer field",
     base(edit={"field": "answer", "find": "C", "replace": "A"}), LCR, lambda d: d["route"] == "faculty_review" and d["edit"] is None),
    ("spelling edit may not change numbers",
     base(edit={"field": "question", "find": "60 V", "replace": "70 V"}), LCR, lambda d: d["edit"] is None),
    ("auto:false repair can never auto-apply",
     base(repair="identical_options", edit=None), LCR, lambda d: d["route"] == "faculty_review"),
    ("fields:[] repair cannot carry an edit",
     base(repair="wrong_chapter", edit={"field": "question", "find": "LCR", "replace": "LCR"}), LCR,
     lambda d: d["edit"] is None and d["route"] == "faculty_review"),
    ("answer_key without two agreeing blind solves goes to faculty",
     base(repair="answer_key", edit={"field": "answer", "find": "C", "replace": "A"}), LCR, lambda d: d["route"] == "faculty_review"),
    ("answer_key with verification may auto-apply",
     base(repair="answer_key", edit={"field": "answer", "find": "C", "replace": "A"}, independently_verified=True), LCR,
     lambda d: d["route"] == "auto_apply"),
    ("edit whose 'find' is not in the record is dropped",
     base(edit={"field": "question", "find": "Epithelial tissue", "replace": "x"}), EPI, lambda d: d["edit"] is None),
    ("low confidence never auto-applies",
     base(edit={"field": "question", "find": "Epithellial tisue", "replace": "Epithelial tissue"}, confidence=0.6), EPI,
     lambda d: d["route"] == "faculty_review"),
    ("unknown verdict from the model is contained",
     base(verdict="admin_override"), LCR, lambda d: d["route"] == "faculty_review"),
    ("non-defect verdicts carry no edit",
     {"verdict": "no_error", "repair": "answer_key", "edit": {"field": "answer", "find": "C", "replace": "C"}, "route": "auto_apply",
      "confidence": 0.99}, LCR, lambda d: d["edit"] is None and d["route"] == "no_change"),
    ("valid spelling fix passes",
     base(edit={"field": "question", "find": "Epithellial tisue", "replace": "Epithelial tissue"}), EPI, lambda d: d["route"] == "auto_apply"),
]

if __name__ == "__main__":
    bad = 0
    for name, d, rec, ok in CHECKS:
        out = enforce(d, rec, TAX)
        good = ok(out)
        bad += not good
        print("PASS" if good else "FAIL", name, "" if good else out)
    print(f"\n{len(CHECKS) - bad}/{len(CHECKS)} guard checks passed")
    raise SystemExit(1 if bad else 0)
