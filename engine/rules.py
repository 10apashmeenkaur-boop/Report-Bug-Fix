"""Stage A: deterministic detectors. No LLM, no network, 100% reproducible.

Each detector returns Finding objects. A Finding can carry a ready-made surgical edit.
Rule of thumb: if plain code can prove it, the LLM never gets a vote on it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .normalize import Question


@dataclass
class Finding:
    code: str                 # detector id, e.g. "exam_reference"
    repair: str | None        # taxonomy repair key it implies
    evidence: str             # one human sentence
    edit: Any = None          # dict | list[dict] | None
    confidence: float = 0.97
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "repair": self.repair, "evidence": self.evidence,
                "edit": self.edit, "confidence": self.confidence, "data": self.data}


def _once(haystack: str, needle: str) -> bool:
    return bool(needle) and haystack.count(needle) == 1


# ---------------------------------------------------------------- question-level detectors
EXAM_TAG = re.compile(r"\(\s*(?:NEET|JEE(?:\s*(?:Main|Mains|Advanced|Adv))?|AIEEE|AIPMT|BITSAT)[^()]{0,30}?\d{4}[^()]{0,15}\)\s*", re.I)
LATEX_CMD = re.compile(r"\\(?:frac|dfrac|sqrt|AA|times|cdot|alpha|beta|theta|pi|mu|lambda|Delta|degree|circ|left|right)\b(?:\{[^{}]*\}){0,2}")
FRAC = re.compile(r"\\d?frac\{([^{}]+)\}\{([^{}]+)\}")
ALIGN_AMP = re.compile(r"<mi>\s*(?:&amp;|&)\s*</mi>")
FIGURE_WORDS = re.compile(r"\b(as shown|shown in (?:the )?fig|in the (?:given )?figure|figure|fig\.|diagram|graph shown)\b", re.I)


def detect_exam_reference(q: Question) -> list[Finding]:
    out = []
    for fld, html in (("question", q.question_html), ("solution", q.solution_html)):
        m = EXAM_TAG.search(html)
        if m and _once(html, m.group()):
            out.append(Finding("exam_reference", "exam_reference",
                               f"Exam tag '{m.group().strip()}' sits inside the {fld} text.",
                               {"field": fld, "find": m.group(), "replace": ""}))
    return out


def _frac_mathml(a: str, b: str) -> str:
    def tok(x):
        x = x.strip()
        return f"<mn>{x}</mn>" if re.fullmatch(r"[\d.]+", x) else f"<mi>{x}</mi>"
    return (f'<math xmlns="http://www.w3.org/1998/Math/MathML" display="inline">'
            f"<mfrac>{tok(a)}{tok(b)}</mfrac></math>")


def detect_markup_leak(q: Question) -> list[Finding]:
    out = []
    for fld, html in (("question", q.question_html), ("solution", q.solution_html)):
        # 1) raw LaTeX visible as text
        for m in FRAC.finditer(html):
            if _once(html, m.group()):
                out.append(Finding("markup_leak", "markup_leak",
                                   f"Raw LaTeX '{m.group()}' is shown as text in the {fld}.",
                                   {"field": fld, "find": m.group(), "replace": _frac_mathml(m.group(1), m.group(2))}))
        if "\\AA" in html and _once(html, "\\AA"):
            out.append(Finding("markup_leak", "markup_leak",
                               f"Raw LaTeX '\\AA' (ångström) is shown as text in the {fld}.",
                               {"field": fld, "find": "\\AA", "replace": "Å"}))
        # 2) LaTeX table-alignment '&' leaked into MathML as a visible character
        amps = ALIGN_AMP.findall(html)
        if amps:
            out.append(Finding("markup_leak", "markup_leak",
                               f"{len(amps)} stray '&' alignment character(s) from LaTeX render visibly in the {fld}.",
                               # token repeats, so a single find/replace can't be unique -> faculty applies "replace all"
                               None, confidence=0.9, data={"count": len(amps), "token": amps[0], "field": fld}))
    return out


def detect_options(q: Question) -> list[Finding]:
    if q.qtype not in ("scq", "mcq"):
        return []
    out = []
    letters = sorted(q.options)
    expected = ["A", "B", "C", "D"]
    missing = [l for l in expected if l not in q.options]
    if missing:
        out.append(Finding("option_missing", "option_repair",
                           f"Only {len(letters)} options are stored ({', '.join(letters)}); option {', '.join(missing)} is absent.",
                           data={"missing": missing}))
    for l, t in q.options.items():
        if not t.strip():
            out.append(Finding("option_blank", "option_repair", f"Option ({l}) is stored blank.", data={"blank": l}))
    seen: dict[str, str] = {}
    for l in letters:
        norm = re.sub(r"\s+", "", q.options[l]).lower()
        if norm and norm in seen:
            out.append(Finding("identical_options", "identical_options",
                               f"Options ({seen[norm]}) and ({l}) are identical: '{q.options[l]}'.",
                               data={"pair": [seen[norm], l]}))
        seen.setdefault(norm, l)
    return out


def detect_figure(q: Question) -> list[Finding]:
    m = FIGURE_WORDS.search(q.stem)
    if m and not q.ques_images:
        return [Finding("figure_missing", "diagram",
                        f"Stem refers to a figure ('{m.group()}') but quesImages is empty.")]
    return []


def detect_answer(q: Question) -> list[Finding]:
    info = q.answer_info
    out = []
    if q.qtype == "mcq" and info["malformed"]:
        letters = info.get("letters", [])
        if len(letters) == 1:
            # one real letter stored in a multi-correct shell -> wrong question type
            out.append(Finding("mcq_single_answer", "type_mismatch",
                               f"Stored as multi-correct (mcq) but the key holds a single letter {letters[0]} "
                               f"wrapped in a malformed list {json.dumps(info['raw'])}.",
                               data={"letters": letters}, confidence=0.9))
        else:
            out.append(Finding("answer_malformed", "answer_key",
                               f"Answer key is malformed: {json.dumps(info['raw'])} ({'; '.join(info['problems'])}). "
                               f"Graders compare letter-by-letter, so a correct {'+'.join(letters)} attempt is marked wrong.",
                               {"field": "answer", "find": json.dumps(info["raw"]), "replace": json.dumps(letters)},
                               data={"letters": letters}, confidence=0.95))
    elif info["malformed"]:
        out.append(Finding("answer_malformed", "answer_key", f"Answer key malformed: {info['problems']}", confidence=0.9))
    return out


UNIT_HINT = re.compile(r"\(\s*in\s+[^)]+\)|\bin\s+(?:units? of\s+)?(?:cm|mm|m|km|s|sec|seconds?|N|J|eV|V|volts?|ohms?|dioptres?|diopters?|kg|g|K|W|A|Hz|rad)\b|_{2,}\s*\S|_\s*_", re.I)
UNIT_TOKEN = r"(N|J|eV|V|W|A|Hz|s|sec|cm|mm|m|kg|g|K|D|Ω|ohm)"


def detect_unit(q: Question) -> list[Finding]:
    if q.qtype != "integerQuestion":
        return []
    if UNIT_HINT.search(q.text):
        return []
    ans = q.answer_info.get("canonical")
    m = re.search(rf"(?<![\d.]){re.escape(str(ans))}\s*{UNIT_TOKEN}\b", q.solution_text)
    if not m:
        return []
    unit = m.group(1)
    return [Finding("unit_missing", "answer_form",
                    f"Integer answer {ans} carries the unit '{unit}' in the solution, but the question never says which unit to answer in.",
                    data={"unit": unit}, confidence=0.9)]


def detect_already_fixed(q: Question, report: dict) -> list[Finding]:
    """A faculty fix logged AFTER the report, and the reported text is gone from the live record."""
    rt = datetime.fromisoformat(report["reportedAt"].replace("Z", "+00:00"))
    out = []
    for h in q.error_history:
        try:
            ht = datetime.fromisoformat(h["at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ht <= rt:
            continue
        quoted = re.findall(r"['\"‘’]([^'\"‘’]{2,30})['\"‘’]", report["comment"])
        gone = [w for w in quoted if w.lower() not in (q.text + " " + q.solution_text).lower()]
        overlap = [w for w in quoted if w.lower() in (h.get("note") or "").lower()]
        if gone or overlap:
            out.append(Finding("already_fixed", None,
                               f"Faculty fixed this on {h['at'][:10]} ({h.get('type')}: {h.get('note')}); "
                               f"reported text {gone or overlap} is no longer in the live record.",
                               confidence=0.97, data={"history": h}))
    return out


def question_findings(q: Question) -> list[Finding]:
    fs: list[Finding] = []
    for det in (detect_exam_reference, detect_markup_leak, detect_options, detect_figure, detect_answer, detect_unit):
        fs.extend(det(q))
    return fs


# ---------------------------------------------------------------- report-level detectors
INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above)|you are (?:now )?(?:the |an? )?(?:admin|system|bot)|"
    r"system prompt|mark (?:this|the) report|change the answer to|act as|jailbreak|developer mode", re.I)
KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")


def is_injection(comment: str) -> bool:
    return bool(INJECTION.search(comment or ""))


def is_gibberish(comment: str) -> bool:
    c = (comment or "").strip().lower()
    if not c:
        return True
    if not re.fullmatch(r"[a-z]{3,}", c):
        return False
    return any(all(ch in row for ch in c) for row in KEYBOARD_ROWS) or not re.search(r"[aeiou]", c)
