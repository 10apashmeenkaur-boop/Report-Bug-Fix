"""Turn Acadza's production HTML + inline MathML into things code and LLMs can reason about.

- html_to_text(): linear, readable text (MathML -> "(a)/(b)", "x^2", "√(...)").
- parse_question(): stem + options {A..D} parsed out of the question HTML.
- normalize_answer(): canonical answer + malformed flags.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

OPTION_RE = re.compile(r"\(([A-D])\)")


# ---------------------------------------------------------------- MathML -> text
def _kids(tag: Tag) -> list:
    return [c for c in tag.children if not (isinstance(c, NavigableString) and not c.strip())]


def _math(node) -> str:
    if isinstance(node, NavigableString):
        return str(node).strip()
    name = node.name.lower() if node.name else ""
    ks = _kids(node)
    if name in ("mfrac",) and len(ks) == 2:
        return f"({_math(ks[0])})/({_math(ks[1])})"
    if name == "msup" and len(ks) == 2:
        return f"{_math(ks[0])}^({_math(ks[1])})"
    if name == "msub" and len(ks) == 2:
        return f"{_math(ks[0])}_{_math(ks[1])}"
    if name == "msubsup" and len(ks) == 3:
        return f"{_math(ks[0])}_{_math(ks[1])}^({_math(ks[2])})"
    if name == "msqrt":
        return f"√({''.join(_math(k) for k in ks)})"
    if name == "mroot" and len(ks) == 2:
        return f"root{_math(ks[1])}({_math(ks[0])})"
    if name in ("mover", "munder") and len(ks) == 2:
        return f"{_math(ks[0])}{_math(ks[1]) if _math(ks[1]) not in ('→', '¯') else ''}"
    if name == "mspace":
        return " "
    if name in ("mi", "mn", "mo", "mtext"):
        t = node.get_text().strip()
        return f" {t} " if name in ("mo",) and t in ("=", "+", "−", "-", "×", "⇒", "∴") else t
    return "".join(_math(k) for k in ks)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for m in soup.find_all("math"):
        m.replace_with(NavigableString(" " + _math(m) + " "))
    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for h in soup.find_all(["h1", "h2", "h3", "p", "div"]):
        h.insert_after(NavigableString("\n"))
    text = soup.get_text()
    text = text.replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


# ---------------------------------------------------------------- question parsing
@dataclass
class Question:
    id: str
    qtype: str
    subject: str
    chapter: str
    raw: dict
    question_html: str
    solution_html: str
    answer: Any
    text: str
    stem: str
    options: dict[str, str]
    solution_text: str
    ques_images: list
    solution_images: list
    error_history: list
    answer_info: dict = field(default_factory=dict)

    def brief(self, with_key: bool = True, with_solution: bool = True) -> str:
        """Plain-text view handed to LLMs. Key/solution can be withheld for blind solving."""
        lines = [f"Subject: {self.subject} | Chapter: {self.chapter} | Type: {self.qtype}",
                 f"Question: {self.stem}"]
        for k in sorted(self.options):
            lines.append(f"({k}) {self.options[k] or '[BLANK]'}")
        if with_key:
            lines.append(f"Stored answer key: {self.answer_info.get('display')}")
        if with_solution:
            lines.append(f"Stored solution: {self.solution_text or '[NONE]'}")
        return "\n".join(lines)


def split_options(text: str) -> tuple[str, dict[str, str]]:
    """Options start at the LAST '(A)' that is followed by a '(B)'.
    ('Assertion (A): ...' must not be read as option A.)"""
    starts = [m.start() for m in re.finditer(r"\(A\)", text)]
    start = None
    for s in reversed(starts):
        if re.search(r"\(B\)", text[s:]):
            start = s
            break
    if start is None:
        return text.strip(), {}
    stem, tail = text[:start].strip(), text[start:]
    parts = OPTION_RE.split(tail)
    opts: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        letter, body = parts[i], parts[i + 1]
        if letter in opts:  # an option body that itself contains '(A)' etc.
            opts[list(opts)[-1]] += f" ({letter}) {body}"
            continue
        opts[letter] = re.sub(r"\s+", " ", body).strip()
    return stem, opts


def normalize_answer(qtype: str, ans: Any) -> dict:
    """Canonical answer + problems. Never mutates the record."""
    info: dict[str, Any] = {"raw": ans, "malformed": False, "problems": []}
    if qtype == "mcq":
        letters: list[str] = []
        items = ans if isinstance(ans, list) else [ans]
        for it in items:
            if not isinstance(it, str):
                info["problems"].append(f"non-string entry {it!r}")
                continue
            if it.strip() == "":
                info["problems"].append("empty-string entry")
                continue
            found = re.findall(r"[A-D]", it.upper())
            if len(found) > 1 or "," in it:
                info["problems"].append(f"several letters packed in one entry {it!r}")
            letters.extend(found)
        letters = sorted(set(letters))
        info["letters"] = letters
        info["canonical"] = letters
        info["malformed"] = bool(info["problems"])
        info["display"] = ", ".join(letters) if letters else "[EMPTY]"
        if len(letters) == 1:
            info["single_letter_mcq"] = True
    elif qtype == "scq":
        s = str(ans).strip().upper() if ans is not None else ""
        info["canonical"] = s
        info["letters"] = [s] if s else []
        info["display"] = s or "[EMPTY]"
        if not re.fullmatch(r"[A-D]", s):
            info["malformed"] = True
            info["problems"].append(f"scq answer {ans!r} is not a single letter A-D")
    else:  # integerQuestion
        s = str(ans).strip() if ans is not None else ""
        info["canonical"] = s
        info["display"] = s or "[EMPTY]"
        try:
            info["value"] = float(s)
        except ValueError:
            info["malformed"] = True
            info["problems"].append(f"integer answer {ans!r} is not numeric")
    return info


def load_question(rec: dict) -> Question:
    qt = rec["questionType"]
    body = rec.get(qt) or {}
    qhtml = body.get("question") or ""
    text = html_to_text(qhtml)
    stem, opts = split_options(text) if qt in ("scq", "mcq") else (text, {})
    q = Question(
        id=rec["_id"], qtype=qt, subject=rec.get("subject") or "", chapter=rec.get("chapter") or "",
        raw=rec, question_html=qhtml, solution_html=body.get("solution") or "",
        answer=body.get("answer"), text=text, stem=stem, options=opts,
        solution_text=html_to_text(body.get("solution")),
        ques_images=body.get("quesImages") or [], solution_images=body.get("solutionImages") or [],
        error_history=rec.get("errorHistory") or [],
    )
    q.answer_info = normalize_answer(qt, q.answer)
    return q


def load_all(path: str) -> dict[str, Question]:
    with open(path, encoding="utf-8") as f:
        return {r["_id"]: load_question(r) for r in json.load(f)}


def to_number(s: str | None) -> float | None:
    """Pull the first number out of an option / answer string ('4800 N/m^(2)' -> 4800)."""
    if s is None:
        return None
    t = str(s).replace("−", "-").replace(" ", "")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m:
        return None
    val = float(m.group())
    e = re.search(r"[×x]10\^\(?(-?\d+)\)?", t)
    if e and t.find(m.group()) < t.find(e.group()):
        val *= 10 ** int(e.group(1))
    return val
