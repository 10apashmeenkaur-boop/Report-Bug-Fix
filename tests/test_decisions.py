"""Decision-logic tests with HAND-WRITTEN fake LLM outputs (test doubles, not real model output).

They check that, GIVEN certain evidence, code + guard produce the right verdict/repair/route/edit,
and that every edit applies cleanly. Real model behaviour is measured separately by eval/eval.py.

    python -m tests.test_decisions
"""
import json
from pathlib import Path

from engine.edits import apply
from engine.llm import LLM
from engine.normalize import load_all
from engine.pipeline import Engine, solve_says, same_answer, agrees_with_key

ROOT = Path(__file__).resolve().parent.parent
Q = load_all(ROOT / "data/questions.json")
R = {r["reportId"]: r for r in json.loads((ROOT / "data/reports.json").read_text(encoding="utf-8"))}
TAX = json.loads((ROOT / "data/taxonomy.json").read_text(encoding="utf-8"))


def S(choice=None, value=None, choices=None):
    return {"choice": choice, "final_value": value, "choices": choices or []}


AUDIT_EMPTY = {"spelling": [], "solution_final": {}, "solution_foreign": {"present": False}, "solution_errors": [],
               "solution_factual": [], "missing_data": {"present": False}, "chapter_fit": {"fits": True},
               "soundness": {"status": "ok"}, "noise": []}


def A(**kw):
    a = json.loads(json.dumps(AUDIT_EMPTY))
    a.update(kw)
    return a


# reportId -> (claim, solve1, solve2, audit, expected (verdict, repair, route), expected edit replace substring)
CASES = {
    "R001": ({"claim_type": "answer_wrong", "student_value": "50V"}, S("A", "50 V"), S("A", "50"),
             A(solution_final={"value": "50 V", "option": "A"}), ("defect", "answer_key", "auto_apply"), "A"),
    "R004": ({"claim_type": "answer_wrong", "student_value": "8225"}, S("D", "4800 N/m^2"), S("D", "4800"),
             A(solution_final={"value": "4800 N/m^2", "option": "D"}), ("defect", "answer_key", "auto_apply"), "D"),
    "R005": ({"claim_type": "answer_wrong", "student_value": "1"}, S(None, "1"), S(None, "1"),
             A(solution_final={"value": "1"}), ("defect", "answer_key", "auto_apply"), "1"),
    "R029": ({"claim_type": "answer_wrong", "student_value": "0.4"}, S("A", "0.8"), S("A", "0.8"),
             A(solution_final={"value": "0.8", "option": "A"}), ("no_error", None, "no_change"), None),
    "R033": ({"claim_type": "answer_wrong", "student_value": "3112.99"}, S(None, "3113"), S(None, "3112.99"),
             A(solution_final={"value": "311.3"}), ("no_error", None, "no_change"), None),
    "R027": ({"claim_type": "answer_wrong", "student_value": "C", "instruction_attempt": True}, S("A", "5.7e-5"), S("A", "5.7"),
             A(solution_final={"value": "5.7 x 10^-5", "option": "A"}), ("no_error", None, "no_change"), None),
    "R034": ({"claim_type": "answer_wrong"}, S("NONE", "2I0"), S("NONE", "2I0"),
             A(soundness={"status": "no_correct_option", "why": "A false (I=2I0) and R false"}), ("defect", "unsound", "faculty_review"), None),
    "R011": ({"claim_type": "option_missing_or_blank", "target_option": "C"}, S("NONE", "1 : 1"), S("C", "1:1"),
             A(), ("defect", "option_repair", "auto_apply"), "1 : 1"),
    "R009": ({"claim_type": "option_missing_or_blank", "target_option": "D"}, S("C", "10"), S("C", "10"),
             A(), ("defect", "option_repair", "faculty_review"), None),
    "R016": ({"claim_type": "solution_mismatch"}, S(None, "20"), S(None, "20"),
             A(solution_final={"value": "40"}, solution_errors=[{"find": "a = 40 cm", "replace": "a = 20 cm", "why": "800 = 2a^2"}]),
             ("defect", "solution_fix", "auto_apply"), "20 cm"),
    "R018": ({"claim_type": "solution_mismatch"}, S("B", "16.8 eV"), S("B", "16.8"),
             A(solution_final={"value": "16.8 eV", "option": "B"},
               solution_foreign={"present": True, "fragment": "= 5.469×10^(−10)m = 5.47\\AA", "why": "from another question"}),
             ("defect", "solution_fix", "auto_apply"), ""),
    "R006": ({"claim_type": "data_missing"}, S(None, "50"), S(None, "50"),
             A(missing_data={"present": True, "what": "edge length of the cube", "value": "10 cm",
                             "anchor": "a solid copper cube", "insert": "of side 10 cm"}), ("defect", "stem_data", "auto_apply"), "of side 10 cm"),
    "R007": ({"claim_type": "data_missing"}, S("C", "50"), S("C", "50"),
             A(missing_data={"present": True, "what": "the inductance", "value": "95.5 mH",
                             "anchor": "an inductance", "insert": "of 95.5 mH"}), ("defect", "stem_data", "auto_apply"), "95.5 mH"),
    "R012": ({"claim_type": "spelling", "target_option": "A"}, None, None,
             A(spelling=[{"field": "question", "find": "Epithellial tisue", "replace": "Epithelial tissue"}]),
             ("defect", "spelling", "auto_apply"), "Epithelial tissue"),
    "R022": ({"claim_type": "spelling", "target_text": "plexus"}, None, None,
             A(spelling=[{"field": "solution", "find": "prominant", "replace": "prominent"},
                         {"field": "question", "find": "plexux", "replace": "plexus"}]), ("defect", "spelling", "auto_apply"), "plexus"),
    "R041": ({"claim_type": "gibberish"}, S("A", "Sachs"), S("A", "Sachs"), A(), ("not_actionable", None, "no_change"), None),
    "R040": ({"claim_type": "answer_wrong"}, S("C", "Golden rice"), S("C", "Golden rice"), A(), ("no_error", None, "no_change"), None),
    "R021": ({"claim_type": "type_or_marking"}, S("B", "1.44 min"), S("B", "1.44"), A(), ("defect", "type_mismatch", "auto_apply"), "scq"),
    "R020": ({"claim_type": "type_or_marking"}, S(None, None, ["A", "B", "D"]), S(None, None, ["A", "B", "D"]), A(),
             ("defect", "answer_key", "auto_apply"), '\\"B\\"'),
    "R042": ({"claim_type": "wrong_chapter"}, None, None,
             A(chapter_fit={"fits": False, "suggested_subject": "Physics", "suggested_chapter": "Electromagnetic Waves", "why": "displacement current"}),
             ("defect", "wrong_chapter", "faculty_review"), None),
    "R025": ({"claim_type": "figure_missing"}, None, None, A(), ("defect", "diagram", "faculty_review"), None),
    "R015": ({"claim_type": "duplicate_options"}, None, None, A(), ("defect", "identical_options", "faculty_review"), None),
    "R017": ({"claim_type": "spelling", "target_text": "mere"}, None, None, A(), ("already_fixed", None, "no_change"), None),
    "R038": ({"claim_type": "app"}, None, None, A(), ("app_issue", None, "no_change"), None),
    "R039": ({"claim_type": "language"}, None, None, A(), ("language_mode", None, "no_change"), None),
    "R026": ({"claim_type": "syllabus"}, None, None, A(), ("defect", "syllabus", "faculty_review"), None),
    "R013": ({"claim_type": "exam_tag"}, None, None, A(), ("defect", "exam_reference", "auto_apply"), None),
    "R014": ({"claim_type": "markup"}, None, None, A(), ("defect", "markup_leak", "auto_apply"), "mfrac"),
    "R024": ({"claim_type": "unit_or_form"}, None, None, A(), ("defect", "answer_form", "auto_apply"), "(in N)"),
}


class FakeEngine(Engine):
    def __init__(self, case):
        super().__init__(Q, TAX, LLM(provider="none"), log=lambda *a: None)
        self.case = case

    def audit(self, q):
        return self.case[3]

    def claims(self, q, reports):
        return {r["reportId"]: {**{"instruction_attempt": False, "target_option": None, "target_text": None,
                                   "student_value": None}, **self.case[0]} for r in reports}

    def solves(self, q):
        c = self.case
        if c[1] is None:
            return None
        a, b = solve_says(q, c[1]), solve_says(q, c[2])
        return {"s1": c[1], "s2": c[2], "a": a, "b": b, "agree": same_answer(q, a, b),
                "a_key": agrees_with_key(q, a), "b_key": agrees_with_key(q, b)}


def main():
    fails = 0
    for rid, case in CASES.items():
        r = R[rid]
        q = Q[r["questionId"]]
        eng = FakeEngine(case)
        decs, _ = eng.run_question(q, [r])
        d = decs[0]
        got = (d["verdict"], d["repair"], d["route"])
        ok = got == case[4]
        if ok and case[5] is not None:
            ok = d["edit"] is not None and case[5] in json.dumps(d["edit"], ensure_ascii=False)
        if ok and d["edit"]:
            try:
                apply(q.raw, d["edit"])
            except Exception as e:
                ok = False
                print("   edit does not apply:", e)
        fails += not ok
        print(("PASS" if ok else "FAIL"), rid, got, "| edit:", json.dumps(d["edit"], ensure_ascii=False)[:110],
              "" if ok else f"\n     expected {case[4]}  notes={d['guard_notes']}  ev={d['evidence'][:200]}")
    print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
