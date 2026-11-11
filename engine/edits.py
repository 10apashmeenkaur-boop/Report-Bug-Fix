"""Surgical edits: {field, find, replace}. Apply, validate, and produce before/after for the console."""
from __future__ import annotations

import copy
import html as htmlmod
import json
import re
from typing import Any

from .normalize import html_to_text, split_options

TEXT_FIELDS = ("question", "solution")


def as_list(edit: Any) -> list[dict]:
    if not edit:
        return []
    return edit if isinstance(edit, list) else [edit]


def stored_value(rec: dict, fld: str) -> str:
    qt = rec["questionType"]
    if fld == "questionType":
        return qt
    v = (rec.get(qt) or {}).get(fld)
    if fld == "answer":
        return v if isinstance(v, str) else json.dumps(v)
    return v or ""


def locate(html: str, text: str) -> str | None:
    """Map a plain-text quote (from an LLM) to an exact substring of the stored HTML, or None.
    Tries: verbatim, HTML-escaped, entity variants (’ -> &rsquo;), whitespace-tolerant."""
    if not text:
        return None
    cands = [text, htmlmod.escape(text, quote=False), text.replace("’", "&rsquo;").replace("‘", "&lsquo;"),
             text.replace("×", "&times;"), text.replace(" ", "&nbsp;")]
    for c in cands:
        if html.count(c) == 1:
            return c
    # whitespace/tag tolerant: allow tags or extra spaces between words
    words = [re.escape(w) for w in text.split()]
    if 1 <= len(words) <= 12:
        pat = r"(?:\s|&nbsp;|<[^>]+>)*".join(words)
        ms = list(re.finditer(pat, html))
        if len(ms) == 1:
            return ms[0].group()
    return None


def apply(rec: dict, edit: Any) -> dict:
    """Return a NEW record with the edit(s) applied. Raises ValueError if any edit cannot apply cleanly."""
    new = copy.deepcopy(rec)
    for e in as_list(edit):
        fld, find, rep = e["field"], e.get("find"), e.get("replace")
        qt = new["questionType"]
        if fld == "questionType":
            if find != qt:
                raise ValueError(f"questionType is {qt}, edit expects {find}")
            new[rep] = new.pop(qt)
            new["questionType"] = rep
            continue
        body = new[qt]
        if fld == "answer":
            cur = stored_value(new, "answer")
            if cur != find:
                raise ValueError(f"answer is {cur!r}, edit expects {find!r}")
            try:
                body["answer"] = json.loads(rep) if rep.strip().startswith("[") else rep
            except json.JSONDecodeError:
                body["answer"] = rep
            continue
        cur = body.get(fld) or ""
        n = cur.count(find) if find else 0
        if n != 1:
            raise ValueError(f"'find' occurs {n} times in {fld} (must be exactly 1)")
        body[fld] = cur.replace(find, rep, 1)
    return new


def _digits_and_math(s: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", html_to_text(s))


def validate(rec: dict, edit: Any, repair: str | None, taxonomy: dict) -> list[str]:
    """Return a list of problems. Empty list = edit is safe to show / apply."""
    problems: list[str] = []
    edits = as_list(edit)
    if not edits:
        return problems
    if repair is None:
        return ["edit proposed without a repair type"]
    spec = taxonomy["repairs"].get(repair)
    if not spec:
        return [f"repair '{repair}' is not in the taxonomy"]
    allowed = set(spec["fields"])
    for e in edits:
        if e.get("field") not in allowed:
            problems.append(f"field '{e.get('field')}' is outside the scope of '{repair}' (allowed: {sorted(allowed) or 'none'})")
    if problems:
        return problems
    try:
        new = apply(rec, edits)
    except (ValueError, KeyError) as ex:
        return [f"edit does not apply cleanly: {ex}"]
    qt0, qt1 = rec["questionType"], new["questionType"]
    old_q, new_q = rec[qt0].get("question") or "", new[qt1].get("question") or ""
    old_s, new_s = rec[qt0].get("solution") or "", new[qt1].get("solution") or ""

    if repair in ("spelling", "noise", "exam_reference"):
        removed = [d for e in edits for d in _digits_and_math(e.get("find") or "")] if repair != "spelling" else []
        for a, b, name in ((old_q, new_q, "question"), (old_s, new_s, "solution")):
            da = _digits_and_math(a)
            for d in removed:  # removing a tag like '(NEET 2019)' legitimately removes its own digits
                if d in da and d not in _digits_and_math(b):
                    da.remove(d)
            if da != _digits_and_math(b):
                problems.append(f"{repair} edit changed a number in the {name} (words-only repair)")
            if a.count("<math") != b.count("<math"):
                problems.append(f"{repair} edit changed math markup in the {name}")
    if repair == "option_repair":
        s0, _ = split_options(html_to_text(old_q))
        s1, _ = split_options(html_to_text(new_q))
        if s0 != s1:
            problems.append("option_repair changed the stem text")
    if repair in ("answer_form",):
        for e in edits:
            if e["find"] not in e["replace"]:
                problems.append("answer_form must be additive (replacement must contain the original text)")
    if repair == "stem_data":
        _, o0 = split_options(html_to_text(old_q))
        _, o1 = split_options(html_to_text(new_q))
        if o0 != o1:
            problems.append("stem_data edit changed the options")
    if repair in ("spelling", "markup_leak", "notation", "noise", "exam_reference", "stem_data", "option_repair", "answer_form"):
        if len(new_q) > len(old_q) * 1.6 + 200 or len(new_s) > len(old_s) * 1.6 + 400:
            problems.append("edit is not surgical (field grew too much)")
    return problems


def before_after(rec: dict, edit: Any) -> dict:
    """For the console: per touched field, the old and new HTML/value."""
    out: dict[str, dict] = {}
    edits = as_list(edit)
    if not edits:
        return out
    try:
        new = apply(rec, edits)
    except Exception:
        return out
    for e in edits:
        f = e["field"]
        out[f] = {"before": stored_value(rec, f), "after": stored_value(new, f), "find": e.get("find"), "replace": e.get("replace")}
    return out
