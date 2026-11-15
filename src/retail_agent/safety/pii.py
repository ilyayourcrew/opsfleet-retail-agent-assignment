"""Value-level PII detection and masking (defence in depth behind the SQL guard).

The SQL guard makes PII columns unreachable. This module is the second and third layer: it
inspects *values* - in result sets before they reach the LLM, and in every text the agent emits
(answers, saved reports) - so PII is caught even if it arrives through an unexpected path
(e.g. a string column that happens to contain e-mails, or the model hallucinating an address).

Detection is by content shape, not column names:
  e-mail, phone numbers, street addresses, coordinate pairs, IP addresses, payment cards
  (Luhn-checked) and - column-wise - postal codes / single coordinates when most values of a
  string/float column look like them.
Customer ids are pseudonymised with a keyed HMAC so they stay joinable across answers
("customer C-3f9a12bc") without exposing the internal id.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field

import pandas as pd

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w.])")
STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr|Drive|Way|Ct|Court|Pl|Place|Ter|Terrace)\b\.?"
)
COORD_PAIR = re.compile(r"-?\b\d{1,2}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b")
POINT_GEOM = re.compile(r"POINT\s*\(\s*-?\d+\.\d+\s+-?\d+\.\d+\s*\)", re.I)
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
POSTAL_VALUE = re.compile(r"^\s*(?:\d{5}(?:-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}|\d{3}-\d{4}|\d{5}-\d{3})\s*$", re.I)

TEXT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", EMAIL),
    ("COORDINATES", COORD_PAIR),
    ("COORDINATES", POINT_GEOM),
    ("STREET_ADDRESS", STREET),
    ("PHONE", PHONE),
    ("IP_ADDRESS", IPV4),
]


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass
class ScanReport:
    findings: dict[str, int] = field(default_factory=dict)
    masked_columns: list[str] = field(default_factory=list)

    def add(self, kind: str, n: int = 1) -> None:
        self.findings[kind] = self.findings.get(kind, 0) + n

    @property
    def total(self) -> int:
        return sum(self.findings.values())


def redact_text(text: str, report: ScanReport | None = None) -> tuple[str, ScanReport]:
    """Mask PII-looking substrings in free text."""
    report = report or ScanReport()
    if not text:
        return text, report
    out = text
    for kind, pat in TEXT_PATTERNS:
        def _sub(m: re.Match[str], kind: str = kind) -> str:
            report.add(kind)
            return f"[REDACTED {kind}]"

        out = pat.sub(_sub, out)

    def _card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            report.add("PAYMENT_CARD")
            return "[REDACTED PAYMENT_CARD]"
        return m.group(0)

    out = CARD.sub(_card, out)
    return out, report


def _column_looks_like(series: pd.Series, pattern: re.Pattern[str], threshold: float = 0.8) -> bool:
    vals = series.dropna().astype(str).head(200)
    if len(vals) < 3:
        return False
    return (vals.str.match(pattern).mean()) >= threshold


def scan_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, ScanReport]:
    """Return a copy with PII values masked, plus a report of what was found."""
    report = ScanReport()
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_string_dtype(s) or s.dtype == object:
            if _column_looks_like(s, POSTAL_VALUE):
                out[col] = "[REDACTED POSTAL_CODE]"
                report.add("POSTAL_CODE", int(s.notna().sum()))
                report.masked_columns.append(str(col))
                continue
            masked = []
            for v in s.tolist():
                if isinstance(v, str):
                    v, _ = redact_text(v, report)
                masked.append(v)
            out[col] = masked
        elif pd.api.types.is_float_dtype(s):
            # raw coordinates: many distinct values with >=5 decimals inside lat/long range
            vals = s.dropna().head(200)
            if len(vals) >= 5 and vals.between(-180, 180).all():
                decimals = vals.map(lambda x: len(repr(float(x)).split(".")[-1]) if "." in repr(float(x)) else 0)
                if (decimals >= 5).mean() > 0.9 and vals.nunique() > 0.9 * len(vals) and str(col).lower() in {
                    "lat", "lng", "lon", "latitude", "longitude", "x", "y"
                }:
                    out[col] = None
                    report.add("COORDINATES", int(s.notna().sum()))
                    report.masked_columns.append(str(col))
    return out, report


class Pseudonymizer:
    def __init__(self, secret: str, prefix: str = "C-"):
        self._key = secret.encode()
        self.prefix = prefix

    def token(self, value: object) -> str:
        digest = hmac.new(self._key, str(value).encode(), hashlib.sha256).hexdigest()[:8]
        return f"{self.prefix}{digest}"

    def apply(self, df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
        out = df.copy()
        lower = {str(c).lower(): c for c in out.columns}
        for c in columns:
            real = lower.get(c.lower())
            if real is not None:
                out[real] = out[real].map(lambda v: None if pd.isna(v) else self.token(v))
        return out
