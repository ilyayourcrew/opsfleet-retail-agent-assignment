"""Numeric grounding: does every number in the answer come from this turn's query results?

A cheap, deterministic hallucination signal for analysis answers and reports (requirement 6, "verify
that generated reports answer the user's intent" and are correct). For each number the model wrote,
we look for a source among:

* the values of the result sets this turn produced (as returned, or x100 for rates),
* simple derivations an analyst would state: column totals and means, differences, ratios, shares
  of a column total (also of two rows combined) and percentage changes between two result values.

Matching respects the precision the model displayed ("$1.2M" matches 1,234,567; "11.6%" matches a
0.1163 change). Years, dates, times, list markers and small integers (ranks, months, "top 10") are
not checked because they match by coincidence or are not data claims.

The share of grounded numbers is recorded on every trace (span `numeric_grounding`), averaged in
`retail-agent stats` and in the eval summary. It is a monitoring signal, not a blocker: a low score
routes the turn to review; it does not hide the answer.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field

import pandas as pd

_STRIP = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b"),  # ISO dates / timestamps
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),  # times
    re.compile(r"(?m)^\s*\d+[.)]\s"),  # numbered list markers
    re.compile(r"`[^`]*`"),  # inline code (ids, trace refs)
]
_NUM = re.compile(r"(?<![\w.\-])(-)?\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(%|[kKmMbB]\b)?(?![\w.]\d)")
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9}
MAX_SOURCE_VALUES = 60


@dataclass
class GroundingResult:
    checked: int = 0
    grounded: int = 0
    ungrounded: list[str] = field(default_factory=list)

    @property
    def share(self) -> float | None:
        return round(self.grounded / self.checked, 3) if self.checked else None


def _answer_numbers(text: str) -> list[tuple[str, float, float]]:
    """(raw, value, tolerance) for each data-like number in the text."""
    clean = text
    for pat in _STRIP:
        clean = pat.sub(" ", clean)
    out = []
    for m in _NUM.finditer(clean):
        sign, whole, frac, suffix = m.group(1), m.group(2), m.group(3) or "", (m.group(4) or "").lower()
        value = float(whole.replace(",", "") + frac)
        decimals = len(frac) - 1 if frac else 0
        mult = _MULT.get(suffix, 1.0)
        value *= mult
        if sign:
            value = -value
        if not frac and not suffix and (abs(value) <= 100 or 1900 <= value <= 2100):
            continue  # small integers and years are not data claims worth checking
        tol = 0.5 * 10 ** (-decimals) * mult
        out.append((m.group(0).strip(), value, tol))
    return out


def _source_values(frames: list[pd.DataFrame]) -> list[float]:
    vals: list[float] = []
    for df in frames:
        num = df.select_dtypes("number")
        for c in num.columns:
            col = [float(v) for v in num[c].dropna().tolist() if math.isfinite(float(v))]
            vals.extend(col[:MAX_SOURCE_VALUES])
            if col:
                vals.extend([sum(col), sum(col) / len(col)])
    return vals


def _column_shares(frames: list[pd.DataFrame]) -> list[float]:
    """Shares of a column total, incl. combined shares of two rows ("Shipped + Processing = 49.9%")."""
    out: list[float] = [100.0]
    for df in frames:
        for c in df.select_dtypes("number").columns:
            col = [float(v) for v in df[c].dropna().tolist() if math.isfinite(float(v))][:MAX_SOURCE_VALUES]
            total = sum(col)
            if not total:
                continue
            out.extend(v / total * 100 for v in col)
            for x, y in itertools.combinations(col, 2):
                out.extend([x + y, (x + y) / total * 100])
    return out


def _candidates(frames: list[pd.DataFrame]) -> list[float]:
    base = _source_values(frames)
    cands = list(base) + [v * 100 for v in base] + _column_shares(frames)
    head = base[: MAX_SOURCE_VALUES]
    for a, b in itertools.permutations(head, 2):
        cands.append(a - b)
        if b:
            cands.extend([a / b, a / b * 100, (a / b - 1) * 100])
    return cands


def check_grounding(answer: str, frames: list[pd.DataFrame]) -> GroundingResult:
    res = GroundingResult()
    nums = _answer_numbers(answer or "")
    if not nums or not frames:
        return res
    cands = _candidates(frames)
    for raw, value, tol in nums:
        res.checked += 1
        allowed = max(tol, abs(value) * 0.001)
        if any(abs(abs(c) - abs(value)) <= allowed for c in cands):
            res.grounded += 1
        elif len(res.ungrounded) < 10:
            res.ungrounded.append(raw)
    return res
