"""Stage F: the safety guard. Pure code, runs last, has the final say over every decision.

The LLM can suggest anything; nothing reaches students unless it passes here.
"""
from __future__ import annotations

from .edits import as_list, validate

AUTO_CONFIDENCE_FLOOR = 0.85


def enforce(decision: dict, rec: dict, taxonomy: dict) -> dict:
    d = dict(decision)
    notes: list[str] = list(d.get("guard_notes", []))
    verdicts, repairs = taxonomy["verdicts"], taxonomy["repairs"]

    # 1. closed lists
    if d["verdict"] not in verdicts:
        notes.append(f"unknown verdict '{d['verdict']}' -> sent to faculty")
        d["verdict"], d["route"] = "defect", "faculty_review"
    if d["verdict"] != "defect":
        if d.get("repair") or d.get("edit"):
            notes.append("non-defect verdict cannot carry a repair/edit -> removed")
        d["repair"], d["edit"], d["route"] = None, None, "no_change"
        d["guard_notes"] = notes
        return d
    if d.get("repair") not in repairs:
        notes.append(f"unknown repair '{d.get('repair')}' -> faculty")
        d["repair"], d["edit"], d["route"] = None, None, "faculty_review"
        d["guard_notes"] = notes
        return d

    spec = repairs[d["repair"]]
    # 2. scope: fields [] means no edit may be written, even a suggestion
    if not spec["fields"] and d.get("edit"):
        d.setdefault("suggestion", d["edit"])
        d["edit"] = None
        notes.append(f"'{d['repair']}' allows no field edits -> edit kept only as a suggestion for faculty")
    # 3. edit validation (scope, applies exactly once, words-only, stem untouched, additive...)
    problems = validate(rec, d.get("edit"), d["repair"], taxonomy) if d.get("edit") else []
    if problems:
        notes.extend(f"edit rejected: {p}" for p in problems)
        d.setdefault("suggestion", d["edit"])
        d["edit"] = None
    # 4. route rules
    route = d.get("route", "faculty_review")
    if not spec["auto"] and route == "auto_apply":
        notes.append(f"'{d['repair']}' is auto:false in the taxonomy -> faculty must approve")
        route = "faculty_review"
    if route == "auto_apply" and not d.get("edit"):
        notes.append("nothing safe to apply automatically -> faculty")
        route = "faculty_review"
    if route == "auto_apply" and d.get("confidence", 0) < AUTO_CONFIDENCE_FLOOR:
        notes.append(f"confidence {d.get('confidence')} < {AUTO_CONFIDENCE_FLOOR} -> faculty")
        route = "faculty_review"
    if route == "auto_apply" and d["repair"] in ("answer_key", "type_mismatch") and not d.get("independently_verified"):
        notes.append("answer-key change without two agreeing blind solves -> faculty")
        route = "faculty_review"
    if route == "no_change":
        route = "faculty_review"  # a defect never silently changes nothing
    d["route"] = route
    if d["route"] != "auto_apply" and d.get("edit"):
        pass  # edit stays as the proposal faculty will approve
    for e in as_list(d.get("edit")):
        if e.get("field") not in spec["fields"]:  # belt and braces
            d["edit"], d["route"] = None, "faculty_review"
            notes.append("scope violation caught at final check")
    d["guard_notes"] = notes
    return d


SEVERITY = {
    "answer_key": 100, "type_mismatch": 95, "unsound": 85, "wrong_question": 85, "stem_data": 80,
    "option_repair": 80, "solution_fix": 60, "solution_missing": 60, "diagram": 70, "identical_options": 60,
    "answer_form": 35, "notation": 30, "markup_leak": 30, "wrong_chapter": 20, "syllabus": 20,
    "spelling": 10, "noise": 10, "exam_reference": 10,
}


def urgency(results_for_question: list[dict]) -> float:
    """Queue order. A wrong key silently costs marks to EVERY student who attempts the question,
    so severity dominates; more independent reports = more students already hurt."""
    import math
    defects = [r for r in results_for_question if r["verdict"] == "defect"]
    if not defects:
        return 0.0
    sev = max(SEVERITY.get(r["repair"], 40) for r in defects)
    n = len({r["studentId"] for r in results_for_question})
    pending = any(r["route"] == "faculty_review" for r in defects)
    return round(sev * (1 + math.log(n)) * (1.25 if pending else 1.0), 1)
