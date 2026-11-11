"""Orchestration: reports -> grouped cases -> evidence -> decision (code) -> guard -> reply."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from . import prompts as P
from .edits import as_list, before_after, locate
from .guard import enforce, urgency
from .llm import LLM, LLMUnavailable
from .normalize import Question, html_to_text, to_number
from .rules import Finding, detect_already_fixed, is_gibberish, is_injection, question_findings

ANSWER_CLAIMS = {"answer_wrong", "solution_mismatch", "type_or_marking", "other", "gibberish"}


# ============================================================== fallback claim classifier (no LLM)
KEYWORDS = [
    ("language", r"\bhindi\b|\benglish me\b|\blanguage\b"),
    ("app", r"\bclicked\b|\bsaved\b|\bcrash|\bkeyboard\b|\bsubmit"),
    ("display", r"\bload|\bslow\b|\bscreen\b|\bcut\b|\blayout\b|\bnot visible\b"),
    ("markup", r"\\frac|\bcode\b|\blatex\b|\bhtml\b"),
    ("exam_tag", r"\b(neet|jee|aieee)\s*\d{4}"),
    ("syllabus", r"syllabus"),
    ("wrong_chapter", r"\bchapter\b|why is it in"),
    ("figure_missing", r"\bfig(ure)?\b|\bdiagram\b"),
    ("duplicate_options", r"option.*\b(same|identical|repeat)|\b(same|identical)\b.*option|two options"),
    ("option_missing_or_blank", r"option\s*[a-d]\s*(is\s*)?(missing|blank)|\bblank\b|only \d options"),
    ("spelling", r"spell"),
    ("unit_or_form", r"\bunit\b|\bround"),
    ("type_or_marking", r"multiple correct|marked .* (still )?(got )?(it )?wrong|negative"),
    ("solution_mismatch", r"\bsolution\b"),
    ("data_missing", r"not given|nahi di|missing|data"),
    ("answer_wrong", r"\bans|\banswer|\bwrong|galat|should be|hona chahiye|aana chahiye|\d"),
]


def fallback_claim(comment: str) -> dict:
    c = (comment or "").lower()
    if is_gibberish(comment):
        ct = "gibberish"
    else:
        ct = next((t for t, pat in KEYWORDS if re.search(pat, c)), "other")
    m = re.search(r"option\s*([a-d])\b", c)
    return {"claim_type": ct, "student_value": None, "target_option": m.group(1).upper() if m else None,
            "target_text": None, "instruction_attempt": is_injection(comment), "summary": comment[:60], "source": "keywords"}


# ============================================================== helpers
def opt(q: Question, letter: str | None) -> str:
    if not letter:
        return "?"
    t = q.options.get(letter)
    return f"({letter}) {t}" if t else f"({letter})"


def key_text(q: Question) -> str:
    if q.qtype == "scq":
        return opt(q, q.answer_info["canonical"])
    return str(q.answer_info["display"])


def nums_equal(a, b, tol=0.01) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs(b) * tol, 1e-9)


def solve_says(q: Question, s: dict | None) -> dict:
    """Normalise one solver output into {choice, choices, value}."""
    if not s:
        return {}
    choice = (s.get("choice") or "").strip().upper() or None
    choices = sorted({c.strip().upper() for c in (s.get("choices") or []) if c})
    if s.get("per_option") and q.qtype == "mcq" and not choices:
        choices = sorted(k for k, v in s["per_option"].items() if isinstance(v, dict) and v.get("correct"))
    return {"choice": choice, "choices": choices, "value": to_number(s.get("final_value")), "raw_value": s.get("final_value")}


def agrees_with_key(q: Question, ss: dict) -> bool | None:
    if not ss:
        return None
    if q.qtype == "integerQuestion":
        k = q.answer_info.get("value")
        return nums_equal(ss.get("value"), k) if ss.get("value") is not None else None
    if q.qtype == "mcq":
        letters = q.answer_info.get("letters", [])
        if ss.get("choices"):
            return ss["choices"] == letters
        return [ss["choice"]] == letters if ss.get("choice") else None
    return ss.get("choice") == q.answer_info["canonical"] if ss.get("choice") else None


def same_answer(q: Question, a: dict, b: dict) -> bool:
    if not a or not b:
        return False
    if q.qtype == "integerQuestion":
        return nums_equal(a.get("value"), b.get("value"))
    if q.qtype == "mcq" and (a.get("choices") or b.get("choices")):
        return (a.get("choices") or [a.get("choice")]) == (b.get("choices") or [b.get("choice")])
    return a.get("choice") == b.get("choice") and a.get("choice") is not None


def scale_only(a: float | None, b: float | None) -> bool:
    """311.3 vs 3113, or 0.08 m vs 8 cm: same quantity, different unit/scale step."""
    import math
    if not a or not b:
        return False
    r = math.log10(abs(b / a))
    return abs(r - round(r)) < 0.01 and round(r) != 0


def describe(q: Question, ss: dict) -> str:
    if not ss:
        return "n/a"
    if q.qtype == "integerQuestion":
        return str(ss.get("raw_value"))
    if q.qtype == "mcq" and ss.get("choices"):
        return ", ".join(ss["choices"])
    c = ss.get("choice")
    return opt(q, c) if c in q.options else (c or str(ss.get("raw_value")))


# ============================================================== engine
class Engine:
    def __init__(self, questions: dict[str, Question], taxonomy: dict, llm: LLM, log=print):
        self.q = questions
        self.tax = taxonomy
        self.llm = llm
        self.log = log
        self._audit: dict[str, dict | None] = {}
        self._solve: dict[str, dict] = {}

    # ---------------------------------------------------------- LLM stages (cached per question)
    def _llm(self, pid, ver, system, user, temperature=0.0):
        try:
            return self.llm.json(pid, ver, system, user, temperature)
        except LLMUnavailable:
            return None
        except Exception as e:  # never let one bad call kill the run
            self.log(f"  ! {pid} failed: {e}")
            return None

    def audit(self, q: Question) -> dict | None:
        if q.id not in self._audit:
            self._audit[q.id] = self._llm("AUDIT", P.AUDIT_VERSION, P.AUDIT_SYSTEM,
                                          P.AUDIT_USER.format(question=q.brief(with_key=True, with_solution=True)))
        return self._audit[q.id]

    def solves(self, q: Question) -> dict:
        if q.id not in self._solve:
            blind = q.brief(with_key=False, with_solution=False)
            s1 = self._llm("SOLVE", P.SOLVE_VERSION, P.SOLVE_SYSTEM, P.SOLVE_USER.format(question=blind))
            s2 = self._llm("VERIFY", P.VERIFY_VERSION, P.VERIFY_SYSTEM, P.VERIFY_USER.format(question=blind))
            a, b = solve_says(q, s1), solve_says(q, s2)
            self._solve[q.id] = {"s1": s1, "s2": s2, "a": a, "b": b,
                                 "agree": same_answer(q, a, b),
                                 "a_key": agrees_with_key(q, a), "b_key": agrees_with_key(q, b)}
        return self._solve[q.id]

    def claims(self, q: Question, reports: list[dict]) -> dict[str, dict]:
        out = {r["reportId"]: fallback_claim(r["comment"]) for r in reports}
        block = "\n".join(f'- reportId: {r["reportId"]}\n  <student_comment>{r["comment"]}</student_comment>' for r in reports)
        res = self._llm("CLAIM", P.CLAIM_VERSION, P.CLAIM_SYSTEM,
                        P.CLAIM_USER.format(question=q.brief(with_key=False, with_solution=False), reports=block))
        for c in (res or {}).get("reports", []):
            rid = c.get("reportId")
            if rid in out and c.get("claim_type"):
                c["source"] = "llm"
                c["instruction_attempt"] = bool(c.get("instruction_attempt")) or is_injection(
                    next(r["comment"] for r in reports if r["reportId"] == rid))
                out[rid] = c
        return out

    # ---------------------------------------------------------- per question
    def run_question(self, q: Question, reports: list[dict]) -> tuple[list[dict], dict]:
        rec = q.raw
        findings = question_findings(q)
        audit = self.audit(q)
        claims = self.claims(q, reports)
        need_solve = any(c["claim_type"] in ANSWER_CLAIMS | {"option_missing_or_blank", "data_missing"}
                         for c in claims.values()) or any(f.repair in ("type_mismatch", "answer_key") for f in findings)
        solves = self.solves(q) if need_solve else None
        decisions = []
        for r in reports:
            d = self.decide(q, r, claims[r["reportId"]], findings, audit, solves)
            d = enforce(d, rec, self.tax)
            d["confidence"] = round(min(d.get("confidence", 0.5), 0.99), 2)
            decisions.append(d)
        self.write_replies(q, reports, decisions)
        case = self.case_view(q, reports, decisions, findings, audit, solves, claims)
        return decisions, case

    # ---------------------------------------------------------- decision table (code, not LLM)
    def decide(self, q: Question, r: dict, claim: dict, findings: list[Finding], audit: dict | None, sv: dict | None) -> dict:
        ct = claim["claim_type"]
        inj = claim.get("instruction_attempt") or is_injection(r["comment"])
        base = {"reportId": r["reportId"], "questionId": q.id, "studentId": r["studentId"], "claim": claim,
                "injection": inj, "guard_notes": []}
        by = defaultdict(list)
        for f in findings:
            by[f.code].append(f)

        def D(verdict, repair=None, edit=None, route="no_change", conf=0.9, evidence="", **kw):
            return {**base, "verdict": verdict, "repair": repair, "edit": edit, "route": route,
                    "confidence": conf, "evidence": evidence, **kw}

        # 0. already fixed by faculty after the report
        af = detect_already_fixed(q, r)
        if af:
            return D("already_fixed", conf=af[0].confidence, evidence=af[0].evidence)

        # 1. non-content complaints
        if ct == "display":
            return D("display_issue", conf=0.9, evidence="Complaint is about rendering/loading; stored content checked and is intact"
                     + (" (all options parse correctly)." if q.options else "."))
        if ct == "app":
            return D("app_issue", conf=0.9, evidence="Complaint is about how the app recorded the attempt, not about this question's content.")
        if ct == "language":
            return D("language_mode", conf=0.95, evidence="Student wants the question in another language; this record is English-only.")
        if ct == "syllabus":
            return D("defect", "syllabus", route="faculty_review", conf=0.6,
                     evidence="Student says this is outside the syllabus. Syllabus calls are faculty-only; the question stays live meanwhile.")
        if ct == "wrong_chapter":
            cf = (audit or {}).get("chapter_fit") or {}
            if cf.get("fits") is False:
                return D("defect", "wrong_chapter", route="faculty_review", conf=0.9,
                         evidence=f"Filed under {q.subject} › {q.chapter}, but it tests {cf.get('suggested_chapter')} "
                                  f"({cf.get('suggested_subject')}). {cf.get('why', '')}".strip(),
                         suggestion={"subject": cf.get("suggested_subject"), "chapter": cf.get("suggested_chapter")})
            if audit is None:
                return D("defect", "wrong_chapter", route="faculty_review", conf=0.5, evidence="Chapter complaint; AI check unavailable.")
            return D("no_error", conf=0.75, evidence=f"Chapter {q.chapter} matches the concept tested. {cf.get('why', '')}")

        # 2. deterministic content detectors
        if ct == "markup" and by["markup_leak"]:
            f = next((f for f in by["markup_leak"] if f.edit), by["markup_leak"][0])
            return D("defect", "markup_leak", f.edit, "auto_apply", f.confidence, f.evidence)
        if ct == "exam_tag" and by["exam_reference"]:
            f = by["exam_reference"][0]
            return D("defect", "exam_reference", f.edit, "auto_apply", f.confidence, f.evidence)
        if ct == "duplicate_options":
            if by["identical_options"]:
                return D("defect", "identical_options", route="faculty_review", conf=0.97,
                         evidence=by["identical_options"][0].evidence + " A new distractor is authored content, so faculty writes it.")
            return D("no_error", conf=0.85, evidence="All stored options are distinct.")
        if ct == "figure_missing":
            if by["figure_missing"]:
                return D("defect", "diagram", route="faculty_review", conf=0.95, evidence=by["figure_missing"][0].evidence)
            if q.ques_images:
                return D("display_issue", conf=0.8, evidence="The record has a figure attached; it is likely failing to load on the device.")
            return D("no_error", conf=0.7, evidence="The question does not depend on a figure.")
        if ct == "unit_or_form":
            if by["unit_missing"]:
                f = by["unit_missing"][0]
                return D("defect", "answer_form", self.unit_edit(q, f.data["unit"]), "auto_apply", f.confidence, f.evidence)
            return D("no_error", conf=0.75, evidence="The question already states the unit / form of the answer.")
        if ct == "option_missing_or_blank" or by["option_blank"] or by["option_missing"]:
            res = self.option_decision(q, by, sv)
            if res:
                return D(*res[:2], edit=res[2], route=res[3], conf=res[4], evidence=res[5],
                         independently_verified=res[6] if len(res) > 6 else False)
            if ct == "option_missing_or_blank":
                return D("display_issue", conf=0.8, evidence="All four options are stored with text; likely a display problem on the device.")

        if ct == "spelling":
            res = self.spelling_decision(q, claim, audit)
            if res:
                return D("defect", "spelling", res["edit"], "auto_apply", res["conf"], res["evidence"])
            return D("no_error", conf=0.7, evidence="No spelling error found in the live record.")

        if ct == "data_missing":
            res = self.missing_data_decision(q, audit)
            if res:
                return D("defect", "stem_data", res["edit"], "auto_apply", res["conf"], res["evidence"])
            if audit is None:
                return D("defect", "stem_data", route="faculty_review", conf=0.4, evidence="Missing-data claim; AI check unavailable.")
            # fall through to answer checks: maybe the student just could not solve it

        # 3. answer-key family (malformed keys are provable by code)
        if by["answer_malformed"] and ct in ANSWER_CLAIMS | {"type_or_marking"}:
            f = by["answer_malformed"][0]
            ok = sv and sv["a"] and agrees_with_key(q, sv["a"]) and agrees_with_key(q, sv["b"])
            contra = sv and sv["a"] and sv["agree"] and agrees_with_key(q, sv["a"]) is False
            # Format-only repair: the SET of letters is unchanged, so code can prove it doesn't change meaning.
            same_set = f.edit and sorted(json.loads(f.edit["replace"])) == q.answer_info["letters"]
            if contra:
                return D("defect", "answer_key", f.edit, "faculty_review", 0.7,
                         f.evidence + f" Also, both blind solves pick {describe(q, sv['a'])}, so the letters themselves may be wrong.")
            return D("defect", "answer_key", f.edit, "auto_apply", 0.97 if ok else 0.93,
                     f.evidence + " Format-only fix: the set of letters is unchanged."
                     + (f" Blind solves also confirm {', '.join(q.answer_info['letters'])} are the correct statements." if ok else ""),
                     independently_verified=bool(same_set))
        if by["mcq_single_answer"] and ct in ANSWER_CLAIMS | {"type_or_marking"}:
            f = by["mcq_single_answer"][0]
            letter = f.data["letters"][0]
            single = bool(sv and sv["a"] and sv["a"].get("choice") == letter and sv["agree"])
            edit = [{"field": "questionType", "find": "mcq", "replace": "scq"},
                    {"field": "answer", "find": json.dumps(q.answer_info["raw"]), "replace": letter}]
            return D("defect", "type_mismatch", edit, "auto_apply", 0.95 if single else 0.75,
                     f.evidence + (f" Both blind solves find exactly one correct option, {opt(q, letter)}." if single else ""),
                     independently_verified=single)

        # 4. answer / solution claims -> blind double-solve
        if ct in ANSWER_CLAIMS | {"data_missing"}:
            return self.answer_decision(q, r, claim, audit, sv, D, inj)

        return D("not_actionable", conf=0.6, evidence="Could not map the comment to a checkable claim; checks found nothing.")

    # ---------------------------------------------------------- sub-decisions
    def unit_edit(self, q: Question, unit: str) -> dict | None:
        words = q.stem.split()
        for n in (6, 5, 4, 3):
            tail = " ".join(words[-n:])
            raw = locate(q.question_html, tail)
            if raw:
                return {"field": "question", "find": raw, "replace": f"{raw} (in {unit})"}
        return None

    def option_decision(self, q: Question, by, sv):
        key = q.answer_info.get("canonical")
        if by["option_blank"]:
            blank = by["option_blank"][0].data["blank"]
            if blank == key and sv and sv.get("a") and sv.get("b"):
                va, vb = str(sv["a"].get("raw_value") or "").strip(), str(sv["b"].get("raw_value") or "").strip()
                norm = lambda t: re.sub(r"\s+", "", t).lower()
                same_val = va and norm(va) == norm(vb)
                # the filled value must not duplicate another option (else it's not 'the missing one')
                clash = any(norm(t) == norm(va) for l, t in q.options.items() if l != blank)
                m = re.search(rf"\({blank}\)\s*(?:&nbsp;)?\s*(?=<br\s*/?>)", q.question_html)
                if same_val and not clash and m and q.question_html.count(m.group()) == 1:
                    edit = {"field": "question", "find": m.group(), "replace": f"({blank}) {va}"}
                    return ("defect", "option_repair", edit, "auto_apply", 0.9,
                            f"Option ({blank}) is blank and it is the keyed answer. Two blind solves both give {va}, "
                            f"which matches no other option, so ({blank}) must read '{va}'.", True)
            return ("defect", "option_repair", None, "faculty_review", 0.9,
                    f"Option ({blank}) is stored blank" + ("" if blank == key else " and it is a distractor; writing a distractor is authored content")
                    + ". Faculty must supply the text.")
        if by["option_missing"]:
            miss = by["option_missing"][0].data["missing"]
            is_key = key in miss
            return ("defect", "option_repair", None, "faculty_review", 0.95,
                    by["option_missing"][0].evidence + (" The key points to the missing option!" if is_key else
                    f" The key {opt(q, key)} is present, so the missing option is a distractor: faculty must author it, AI must not invent one."))
        return None

    def spelling_decision(self, q: Question, claim: dict, audit: dict | None):
        if not audit:
            return None
        target = (claim.get("target_text") or "").lower()
        topt = claim.get("target_option")
        cands = []
        for s in audit.get("spelling") or []:
            fld = s.get("field") if s.get("field") in ("question", "solution") else "question"
            html = q.question_html if fld == "question" else q.solution_html
            raw = locate(html, s.get("find", ""))
            if not raw or not s.get("replace") or s["find"] == s["replace"]:
                continue
            score = 0
            f = s["find"].lower()
            if target and (target in f or f in target or any(w in f for w in target.split() if len(w) > 3)):
                score += 3
            if topt and q.options.get(topt) and s["find"] in q.options[topt]:
                score += 2
            if fld == "question":
                score += 1
            cands.append((score, fld, raw, s))
        if not cands:
            return None
        cands.sort(key=lambda c: -c[0])
        _, fld, raw, s = cands[0]
        replace = raw.replace(s["find"], s["replace"]) if s["find"] in raw else s["replace"]
        return {"edit": {"field": fld, "find": raw, "replace": replace}, "conf": 0.92 if cands[0][0] >= 3 else 0.88,
                "evidence": f"'{s['find']}' is misspelt in the {fld}; correct spelling is '{s['replace']}'. Words only, no values touched."}

    def missing_data_decision(self, q: Question, audit: dict | None):
        md = (audit or {}).get("missing_data") or {}
        if not md.get("present"):
            return None
        anchor = locate(q.question_html, md.get("anchor") or "")
        ins = (md.get("insert") or "").strip()
        if not anchor or not ins:
            return None
        nums = re.findall(r"\d+(?:\.\d+)?", ins)
        sol = q.solution_text.replace(" ", "")
        if nums and not all(n in sol for n in nums):
            return None  # value must come from the solution, never invented
        sep = "" if ins[:1] in ",.;)" else " "
        return {"edit": {"field": "question", "find": anchor, "replace": f"{anchor}{sep}{ins}"}, "conf": 0.9,
                "evidence": f"The question never gives {md.get('what')}, but the stored solution uses {md.get('value')}. "
                            f"Inserting it (value taken from the solution, not invented)."}

    def solution_fix_decision(self, q: Question, audit: dict | None):
        if not audit:
            return None
        # (a) a fragment that belongs to another question
        sf = audit.get("solution_foreign") or {}
        if sf.get("present") and sf.get("fragment"):
            frag_nums = re.findall(r"\d+(?:\.\d+)?", sf["fragment"])
            alien = [n for n in frag_nums if n not in q.stem.replace(" ", "") and n not in "".join(q.options.values())]
            blocks = re.findall(r"<math\b.*?</math>", q.solution_html, re.S)
            hit = [b for b in blocks if alien and all(n in html_to_text(b).replace(" ", "") for n in alien)]
            if len(hit) == 1 and q.solution_html.count(hit[0]) == 1:
                return {"edit": {"field": "solution", "find": hit[0], "replace": ""}, "conf": 0.9 if alien else 0.8,
                        "evidence": f"The solution contains a step from a different question ('{sf['fragment'].strip()}'): "
                                    f"{', '.join(alien)} appear nowhere in this question. {sf.get('why', '')} Removing that step; "
                                    f"the rest still ends at the keyed answer."}
        # (b) a wrong step/number
        for e in audit.get("solution_errors") or []:
            raw = locate(q.solution_html, e.get("find", ""))
            if raw and e.get("replace"):
                rep = raw.replace(e["find"], e["replace"]) if e["find"] in raw else e["replace"]
                return {"edit": {"field": "solution", "find": raw, "replace": rep}, "conf": 0.9,
                        "evidence": f"Solution step '{e['find']}' is wrong; it should be '{e['replace']}'. {e.get('why', '')}"}
        return None

    def answer_decision(self, q: Question, r: dict, claim: dict, audit: dict | None, sv: dict | None, D, inj: bool):
        ct = claim["claim_type"]
        inj_note = " The comment contained instructions to the system; they were ignored and only the content was checked." if inj else ""
        if not sv or not sv.get("a"):
            if ct == "gibberish":
                return D("not_actionable", conf=0.6, evidence="Comment is unintelligible; no automatic check could run." + inj_note)
            return D("defect", "answer_key", route="faculty_review", conf=0.3,
                     evidence="AI solver unavailable; a teacher must check the key." + inj_note)
        a, b = sv["a"], sv["b"]
        sol = (audit or {}).get("solution_final") or {}
        sol_ss = {"choice": (sol.get("option") or "").upper() or None, "choices": [], "value": to_number(sol.get("value")),
                  "raw_value": sol.get("value")}
        sol_key = agrees_with_key(q, sol_ss) if (sol.get("value") or sol.get("option")) else None
        ev = (f"Blind solve 1 → {describe(q, a)}; blind solve 2 → {describe(q, b)}; "
              f"stored solution ends at {sol.get('value') or 'n/a'}; key says {key_text(q)}.")

        # unsound: solvers agree there is no single valid option
        solved_ok = sv["agree"] and ((q.qtype == "integerQuestion" and a.get("value") is not None)
                                     or a.get("choice") in ("A", "B", "C", "D") or bool(a.get("choices")))
        audit_unsound = ((audit or {}).get("soundness") or {}).get("status") in ("no_correct_option", "multiple_correct")
        if (a.get("choice") in ("NONE", "MULTIPLE") and b.get("choice") in ("NONE", "MULTIPLE")) or \
           (audit_unsound and not solved_ok and not sv["a_key"]):
            why = ((audit or {}).get("soundness") or {}).get("why", "")
            return D("defect", "unsound", route="faculty_review", conf=0.85,
                     evidence=f"No option is valid as written. {why} {ev}".strip() + inj_note)

        if sv["a_key"] and sv["b_key"] and ct == "gibberish":
            return D("not_actionable", conf=0.9, evidence=f"Comment is unintelligible. Checks found nothing: {ev}" + inj_note,
                     correct=key_text(q))
        if sv["a_key"] and sv["b_key"]:
            # key confirmed twice. Is the SOLUTION the broken part?
            foreign = ((audit or {}).get("solution_foreign") or {}).get("present")
            scaled = sol_key is False and scale_only(sol_ss.get("value"), q.answer_info.get("value"))
            if ct == "solution_mismatch" or foreign:
                fx = self.solution_fix_decision(q, audit)
                if fx:
                    return D("defect", "solution_fix", fx["edit"], "auto_apply", fx["conf"], fx["evidence"] + " " + ev)
                if sol_key is False and not scaled:
                    return D("defect", "solution_fix", route="faculty_review", conf=0.75,
                             evidence="The key is right but the stored solution reaches a different answer. " + ev)
            if scaled:
                ev += " (The solution stops before the final unit/scale step; same quantity.)"
            sv_txt = claim.get("student_value")
            return D("no_error", conf=0.95 if sol_key is not False else 0.85,
                     evidence=f"Key verified independently twice. {ev}"
                              + (f" Student's value ({sv_txt}) does not match." if sv_txt else "") + inj_note,
                     correct=key_text(q))
        if sv["agree"] and sv["a_key"] is False:
            new = self.new_key(q, a)
            sol_agrees = same_answer(q, a, sol_ss) if sol_ss.get("value") is not None or sol_ss.get("choice") else False
            if sol_agrees and new is not None:
                return D("defect", "answer_key", {"field": "answer", "find": q.answer_info["canonical"] if q.qtype != "mcq"
                                                   else json.dumps(q.answer_info["raw"]), "replace": new},
                         "auto_apply", 0.95, f"Key is wrong. {ev} Two blind solves and the stored solution all agree." + inj_note,
                         independently_verified=True, correct=describe(q, a))
            return D("defect", "answer_key", {"field": "answer", "find": q.answer_info["canonical"], "replace": new} if new else None,
                     "faculty_review", 0.7, f"Both blind solves disagree with the key, but the stored solution does not back them. {ev}" + inj_note,
                     correct=describe(q, a))
        # solvers split: trust nothing automatically
        if sol_key and (sv["a_key"] or sv["b_key"]):
            return D("no_error", conf=0.7, evidence=f"One blind solve and the stored solution both match the key. {ev}" + inj_note,
                     correct=key_text(q))
        return D("defect", "answer_key", route="faculty_review", conf=0.5,
                 evidence=f"Evidence conflicts; a teacher should decide. {ev}" + inj_note)

    def new_key(self, q: Question, a: dict) -> str | None:
        if q.qtype == "integerQuestion":
            v = a.get("value")
            if v is None:
                return None
            return str(int(round(v))) if abs(v - round(v)) < 1e-6 else str(v)
        if q.qtype == "mcq":
            return json.dumps(a.get("choices") or [a.get("choice")])
        c = a.get("choice")
        return c if c in ("A", "B", "C", "D") else None

    # ---------------------------------------------------------- replies
    def write_replies(self, q: Question, reports: list[dict], decisions: list[dict]):
        status = {"auto_apply": "fixed", "faculty_review": "teacher_review", "no_change": "no_change"}
        cases = []
        for r, d in zip(reports, decisions):
            cases.append({"reportId": r["reportId"], "student_comment": r["comment"],
                          "status": status[d["route"]] if d["verdict"] == "defect" else d["verdict"],
                          "what_we_found": d["evidence"][:400], "correct_answer": d.get("correct"),
                          "instruction_attempt": d.get("injection")})
        res = self._llm("REPLY", P.REPLY_VERSION, P.REPLY_SYSTEM,
                        P.REPLY_USER.format(question=q.stem[:300], cases=json.dumps(cases, ensure_ascii=False, indent=1)),
                        temperature=0.3)
        replies = (res or {}).get("replies") or {}
        for r, d in zip(reports, decisions):
            txt = replies.get(r["reportId"])
            d["student_reply"] = txt.strip() if isinstance(txt, str) and txt.strip() else template_reply(d)
            d["reply_source"] = "llm" if txt else "template"

    # ---------------------------------------------------------- console payload
    def case_view(self, q, reports, decisions, findings, audit, sv, claims) -> dict:
        main = max(decisions, key=lambda d: (d["verdict"] == "defect", d["route"] == "faculty_review", d["confidence"]))
        sugg = main.get("suggestion")
        sugg = sugg if sugg and all(isinstance(e, dict) and "field" in e for e in as_list(sugg)) else None
        diffs = before_after(q.raw, main.get("edit") or sugg)
        return {
            "questionId": q.id, "subject": q.subject, "chapter": q.chapter, "qtype": q.qtype, "level": q.raw.get("level"),
            "question_html": q.question_html, "solution_html": q.solution_html, "answer": q.answer,
            "answer_display": key_text(q), "options": q.options, "stem": q.stem,
            "urgency": urgency(decisions), "headline": headline(q, main),
            "primary": {k: main.get(k) for k in ("reportId", "verdict", "repair", "edit", "route", "confidence", "evidence", "guard_notes", "suggestion", "correct")},
            "diff": diffs,
            "reports": [{**r, **{k: d.get(k) for k in ("verdict", "repair", "edit", "route", "confidence", "evidence", "student_reply",
                                                       "guard_notes", "injection", "reply_source")},
                         "claim": claims.get(r["reportId"])} for r, d in zip(reports, decisions)],
            "findings": [f.to_dict() for f in findings],
            "solves": None if not sv else {"s1": sv["s1"], "s2": sv["s2"], "agree": sv["agree"],
                                           "s1_matches_key": sv["a_key"], "s2_matches_key": sv["b_key"]},
            "audit": audit, "error_history": q.error_history,
        }


REPAIR_PLAIN = {
    "answer_key": "Answer key wrong", "type_mismatch": "Wrong question type", "unsound": "No valid answer",
    "wrong_question": "Solution belongs elsewhere", "stem_data": "Missing value in question", "option_repair": "Option missing/blank",
    "solution_fix": "Solution has an error", "solution_missing": "Solution missing", "diagram": "Figure missing",
    "identical_options": "Duplicate options", "answer_form": "Unit not stated", "notation": "Symbol lost",
    "markup_leak": "Raw code visible", "wrong_chapter": "Wrong chapter", "syllabus": "Syllabus query",
    "spelling": "Spelling", "noise": "Stray text", "exam_reference": "Exam tag in text",
}
VERDICT_PLAIN = {"no_error": "Key checked, student mistaken", "display_issue": "Display issue", "app_issue": "App issue",
                 "language_mode": "Language request", "already_fixed": "Already fixed", "not_actionable": "Not actionable"}


def headline(q: Question, d: dict) -> str:
    if d["verdict"] != "defect":
        return VERDICT_PLAIN.get(d["verdict"], d["verdict"])
    return REPAIR_PLAIN.get(d["repair"], d["repair"])


def template_reply(d: dict) -> str:
    v, route = d["verdict"], d["route"]
    if v == "defect" and route == "auto_apply":
        return "You were right, thank you! We've fixed this question, so it's correct now."
    if v == "defect":
        return "Thanks for flagging this. You've spotted a real problem and a teacher is fixing it now."
    if v == "no_error":
        return ("Thanks for checking so carefully! We re-solved this independently and the given answer "
                f"{d.get('correct') or ''} is correct. Have a look at the solution once more; the key step is explained there.").replace("  ", " ")
    if v == "already_fixed":
        return "Good catch, and it's already been corrected. Refresh the question to see the fixed version."
    if v == "display_issue":
        return "Thanks! The question itself is fine; this looks like a display problem on your device. Try refreshing or updating the app. We've logged it for our app team."
    if v == "app_issue":
        return "Sorry about that! This is an app issue, not a problem with the question. We've passed it to our app team."
    if v == "language_mode":
        return "This question is only available in English right now. We've noted your request for a Hindi version."
    return "Thanks for reporting! Could you tell us a little more about what looks wrong so we can check it?"
