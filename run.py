"""Run the Report -> Fix engine on all reports.

    export GEMINI_API_KEY=...        # free key from aistudio.google.com
    python run.py                    # writes out/results.json, out/console.json, out/bonus_findings.json
    python run.py --provider none    # rules-only dry run (no AI); answer claims go to faculty
"""
from __future__ import annotations

import argparse
import json
import time
from collections import OrderedDict
from pathlib import Path

from engine.llm import LLM
from engine.normalize import load_all
from engine.pipeline import Engine
from engine.rules import question_findings
from engine.edits import locate

ROOT = Path(__file__).resolve().parent
RESULT_KEYS = ["reportId", "questionId", "verdict", "repair", "edit", "route", "confidence", "evidence", "student_reply"]


def bonus_findings(engine: Engine, questions, decisions_by_q: dict, scan_all: bool = True) -> list[dict]:
    """Defects nobody reported (or that a report didn't cover)."""
    out = []
    for q in questions.values():
        covered = {d["repair"] for d in decisions_by_q.get(q.id, []) if d["verdict"] == "defect"}
        covered_edits = json.dumps([d.get("edit") for d in decisions_by_q.get(q.id, [])])
        def add(kind, repair, evidence, edit=None, conf=0.8):
            out.append({"questionId": q.id, "subject": q.subject, "chapter": q.chapter, "kind": kind, "repair": repair,
                        "evidence": evidence, "edit": edit, "confidence": conf,
                        "reported": q.id in decisions_by_q})
        for f in question_findings(q):
            if f.repair not in covered:
                add(f.code, f.repair, f.evidence, f.edit, f.confidence)
        if scan_all is False and q.id not in decisions_by_q:
            continue
        a = engine.audit(q)
        if not a:
            continue
        for s in a.get("spelling") or []:
            fld = s.get("field") if s.get("field") in ("question", "solution") else "question"
            raw = locate(q.question_html if fld == "question" else q.solution_html, s.get("find", ""))
            if raw and json.dumps(raw)[1:-1] not in covered_edits and s.get("find") != s.get("replace"):
                add("spelling", "spelling", f"'{s['find']}' → '{s['replace']}' ({fld})",
                    {"field": fld, "find": raw, "replace": raw.replace(s["find"], s["replace"])}, 0.85)
        for e in a.get("solution_factual") or []:
            add("solution_factual", "solution_fix", f"Solution states '{e.get('find')}' — {e.get('why')} Correct: '{e.get('replace')}'.", None, 0.8)
        for e in a.get("solution_errors") or []:
            if "solution_fix" not in covered:
                add("solution_error", "solution_fix", f"Solution step '{e.get('find')}' should be '{e.get('replace')}'. {e.get('why', '')}", None, 0.75)
        for n in a.get("noise") or []:
            add("noise", "noise", f"Stray text '{n.get('find')}' in {n.get('field')}: {n.get('why', '')}", None, 0.8)
        snd = a.get("soundness") or {}
        if snd.get("status") in ("no_correct_option", "multiple_correct") and "unsound" not in covered:
            add("unsound", "unsound", snd.get("why", ""), None, 0.6)
        cf = a.get("chapter_fit") or {}
        if cf.get("fits") is False and "wrong_chapter" not in covered:
            add("wrong_chapter", "wrong_chapter", f"Filed as {q.chapter}; looks like {cf.get('suggested_chapter')}. {cf.get('why', '')}", None, 0.7)
        md = a.get("missing_data") or {}
        if md.get("present") and "stem_data" not in covered:
            add("missing_data", "stem_data", f"Question does not state {md.get('what')} (solution uses {md.get('value')}).", None, 0.7)
    return out


def main():
    import sys
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None, help="gemini | openai | none")
    ap.add_argument("--model", default=None)
    ap.add_argument("--only", default=None, help="comma-separated reportIds to run (debug)")
    args = ap.parse_args()

    questions = load_all(ROOT / "data/questions.json")
    reports = json.loads((ROOT / "data/reports.json").read_text(encoding="utf-8"))
    taxonomy = json.loads((ROOT / "data/taxonomy.json").read_text(encoding="utf-8"))
    if args.only:
        keep = set(args.only.split(","))
        reports = [r for r in reports if r["reportId"] in keep]

    llm = LLM(provider=args.provider, model=args.model)
    print(f"provider={llm.provider} model={llm.model} strong={llm.model_strong} rpm/model={llm.rpm} reports={len(reports)} questions={len(questions)}")
    engine = Engine(questions, taxonomy, llm)

    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in sorted(reports, key=lambda r: r["reportedAt"]):
        groups.setdefault(r["questionId"], []).append(r)

    t0 = time.time()
    all_dec, cases, by_q = {}, [], {}
    for i, (qid, rs) in enumerate(groups.items(), 1):
        q = questions[qid]
        print(f"[{i:2}/{len(groups)}] {qid}  {q.chapter[:34]:34}  {', '.join(r['reportId'] for r in rs)}")
        decs, case = engine.run_question(q, rs)
        for d in decs:
            all_dec[d["reportId"]] = d
            print(f"      {d['reportId']}: {d['verdict']:<14} {str(d['repair']):<17} {d['route']:<15} {d['confidence']}")
        by_q[qid] = decs
        cases.append(case)

    bonus = bonus_findings(engine, questions, by_q, scan_all=not args.only)
    ordered = sorted(json.loads((ROOT / "data/reports.json").read_text(encoding="utf-8")), key=lambda r: r["reportId"])
    results = [{k: all_dec[r["reportId"]].get(k) for k in RESULT_KEYS} for r in ordered if r["reportId"] in all_dec]
    cases.sort(key=lambda c: -c["urgency"])

    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    if args.only:  # debug runs never overwrite the real deliverables
        (out / "results_partial.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[--only] wrote out/results_partial.json ({len(results)} results); results.json and web/data.js untouched")
        for r in results:
            print(f"  {r['reportId']}: {r['verdict']} / {r['repair']} / {r['route']} / {r['confidence']}  {r['evidence'][:160]}")
        return
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "bonus_findings.json").write_text(json.dumps(bonus, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "provider": llm.provider,
            "model": llm.model if llm.model_strong == llm.model else f"{llm.model} + {llm.model_strong} (solves)",
            "llm_calls": llm.calls, "seconds": round(time.time() - t0, 1)}
    (out / "console.json").write_text(json.dumps({"meta": meta, "taxonomy": taxonomy, "cases": cases, "bonus": bonus},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    web = ROOT / "web"
    web.mkdir(exist_ok=True)
    payload = {"meta": meta, "taxonomy": taxonomy, "cases": cases, "bonus": bonus}
    # data.js (not fetch) so the console also opens straight from disk, no server needed
    (web / "data.js").write_text("window.CONSOLE_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")
    (ROOT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {len(results)} results, {len(cases)} cases, {len(bonus)} bonus findings  |  {meta}")
    if llm.calls["failed"] or llm.dead:
        print(f"\n!! {llm.calls['failed']} AI call(s) failed{' (daily quota exhausted)' if llm.dead else ''}. "
              "Those reports were routed to faculty_review. Run python run.py again to fill the gaps (finished calls are cached).")


if __name__ == "__main__":
    main()
