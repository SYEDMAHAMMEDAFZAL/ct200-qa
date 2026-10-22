"""Lightweight, explainable text diff between two versions of a section.

The point is not a pretty diff but a *severity* a QA engineer can act on:

* ``substantive`` - a number, unit, error code or modal/negation word changed
  ("40 mmHg" -> "30 mmHg", "E1-E5" -> "E1-E6", "must" -> "may"). Test cases built on
  the old text are probably wrong now.
* ``wording``     - words changed but none of the above. The test case is
  probably still right but someone should glance at it.
* ``none``        - identical after whitespace normalisation.

Known limit: a pure wording change can still flip meaning ("inflate" ->
"deflate"); that is reported as ``wording``, which is why it is never treated
as "safe", only as lower priority.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter

from app.parser import collapse_ws

QUANTITY_RE = re.compile(
    r"(?<![\w.])[<>≤≥±]?\s?\d+(?:[.,]\d+)?(?:\s?[–-]\s?\d+(?:[.,]\d+)?)?"
    r"(?:\s?(?:mmHg|bpm|%|seconds?|s\b|minutes?|min\b|cm|mm|years?|V|x))?",
    re.I,
)
CODE_RE = re.compile(r"\bE\d+(?:\s?[–-]\s?E\d+)?\b")
CRITICAL_WORDS = {"not", "no", "never", "must", "shall", "should", "may", "cannot", "can", "except",
                  "only", "always", "unless", "without", "immediately", "minimum", "maximum"}
TOKEN_RE = re.compile(r"\w+(?:[.,]\d+)?|[^\w\s]")


def _quantities(text: str) -> Counter:
    vals = [collapse_ws(m.group(0)) for m in QUANTITY_RE.finditer(text)]
    vals += [m.group(0) for m in CODE_RE.finditer(text)]
    return Counter(v for v in vals if v.strip())


def diff_summary(old: str, new: str, max_changes: int = 12) -> dict:
    old_n, new_n = collapse_ws(old), collapse_ws(new)
    if old_n == new_n:
        return {"changed": False, "severity": "none", "similarity": 1.0,
                "changes": [], "numeric_changes": {"replaced": [], "removed": [], "added": []}, "critical_words": []}

    a, b = TOKEN_RE.findall(old_n), TOKEN_RE.findall(new_n)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    changes, touched, replaced = [], [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        o, n = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        touched += [t.lower() for t in a[i1:i2] + b[j1:j2]]
        if op == "replace" and re.search(r"\d", o) and re.search(r"\d", n):
            replaced.append({"old": o, "new": n, "context_before": " ".join(a[max(0, i1 - 4):i1])})
        if len(changes) < max_changes:
            ctx = " ".join(a[max(0, i1 - 4):i1])
            changes.append({"op": op, "old": o, "new": n, "context_before": ctx})

    qa, qb = _quantities(old_n), _quantities(new_n)
    removed = sorted((qa - qb).elements())
    added = sorted((qb - qa).elements())
    crit = sorted({t for t in touched if t in CRITICAL_WORDS})
    severity = "substantive" if (removed or added or replaced or crit) else "wording"
    return {
        "changed": True,
        "severity": severity,
        "similarity": round(sm.ratio(), 3),
        "changes": changes,
        "numeric_changes": {"replaced": replaced, "removed": removed, "added": added},
        "critical_words": crit,
    }


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=collapse_ws(a).lower(), b=collapse_ws(b).lower(), autojunk=False).ratio()
