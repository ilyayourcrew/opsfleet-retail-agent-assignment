"""User-level learning loop (requirement 4a): per-user presentation preferences.

Two signal types:
  * explicit - the user states it ("I prefer tables"); the agent calls `remember_preference`
               (or the user runs /prefs). Confidence 1.0, always wins.
  * implicit - inferred from behaviour: phrases like "too long", "more detail", "as bullet points",
               "show me a chart". Each observation adds evidence; an inferred preference becomes
               active after 2 consistent observations and never overrides an explicit one.
Active preferences are rendered into the system instruction on every turn.
In production the same records live in Firestore and can also be updated by an offline job that
summarises feedback ("thumbs down: too verbose") per user.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ALLOWED: dict[str, set[str] | None] = {
    "format": {"table", "bullets", "prose"},
    "depth": {"brief", "standard", "deep"},
    "visuals": {"charts", "text"},
    "note": None,  # free-text (e.g. "always show % change vs last year"); max 200 chars
}
ACTIVATION_EVIDENCE = 2

SIGNALS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b(too long|too much text|shorter|tl;?dr|keep it short|just the numbers|in short)\b", re.I), "depth", "brief"),
    (re.compile(r"\b(more detail|go deeper|dig deeper|elaborate|in depth|more thorough|explain more)\b", re.I), "depth", "deep"),
    (re.compile(r"\b(as|in) a table\b|\btabular\b|\bput (it|that) in a table\b", re.I), "format", "table"),
    (re.compile(r"\bbullet(s| points?)\b", re.I), "format", "bullets"),
    (re.compile(r"\b(chart|graph|plot|visuali[sz]e)\b", re.I), "visuals", "charts"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_prefs (
  user_id TEXT, key TEXT, value TEXT, source TEXT, evidence INTEGER, updated_at REAL,
  PRIMARY KEY (user_id, key)
);
"""


@dataclass
class Preference:
    key: str
    value: str
    source: str  # explicit | inferred
    evidence: int

    @property
    def active(self) -> bool:
        return self.source == "explicit" or self.evidence >= ACTIVATION_EVIDENCE


class PreferenceError(ValueError):
    pass


class PreferenceStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._con.executescript(SCHEMA)
        self._lock = threading.Lock()

    @staticmethod
    def validate(key: str, value: str) -> tuple[str, str]:
        key, value = key.strip().lower(), value.strip()
        if key not in ALLOWED:
            raise PreferenceError(f"Unknown preference '{key}'. Allowed: {', '.join(ALLOWED)}.")
        allowed = ALLOWED[key]
        if allowed is not None:
            value = value.lower()
            if value not in allowed:
                raise PreferenceError(f"'{value}' is not valid for {key}. Allowed: {', '.join(sorted(allowed))}.")
        elif not value or len(value) > 200:
            raise PreferenceError("Notes must be 1-200 characters.")
        return key, value

    def get_all(self, user_id: str) -> list[Preference]:
        with self._lock:
            rows = self._con.execute("SELECT key, value, source, evidence FROM user_prefs WHERE user_id = ? ORDER BY key", (user_id,)).fetchall()
        return [Preference(*r) for r in rows]

    def active(self, user_id: str) -> dict[str, Preference]:
        return {p.key: p for p in self.get_all(user_id) if p.active}

    def set_explicit(self, user_id: str, key: str, value: str) -> Preference:
        key, value = self.validate(key, value)
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO user_prefs VALUES (?,?,?,?,?,?)", (user_id, key, value, "explicit", 1, time.time())
            )
        return Preference(key, value, "explicit", 1)

    def observe(self, user_id: str, key: str, value: str) -> Preference | None:
        """Record one implicit observation. Returns the preference if it is (now) active."""
        key, value = self.validate(key, value)
        with self._lock:
            row = self._con.execute("SELECT value, source, evidence FROM user_prefs WHERE user_id=? AND key=?", (user_id, key)).fetchone()
            if row and row[1] == "explicit":
                return None  # never override what the user told us
            evidence = (row[2] + 1) if row and row[0] == value else 1
            self._con.execute(
                "INSERT OR REPLACE INTO user_prefs VALUES (?,?,?,?,?,?)", (user_id, key, value, "inferred", evidence, time.time())
            )
        p = Preference(key, value, "inferred", evidence)
        return p if p.active else None

    def learn_from_message(self, user_id: str, text: str) -> list[tuple[str, str]]:
        observed = []
        for pat, key, value in SIGNALS:
            if pat.search(text or ""):
                self.observe(user_id, key, value)
                observed.append((key, value))
        return observed

    def reset(self, user_id: str) -> None:
        with self._lock:
            self._con.execute("DELETE FROM user_prefs WHERE user_id = ?", (user_id,))

    def render(self, user_id: str) -> str:
        prefs = self.active(user_id)
        if not prefs:
            return "No stored preferences yet - use a clear, executive-friendly default (headline + compact table)."
        lines = []
        for p in prefs.values():
            how = "stated by the user" if p.source == "explicit" else f"learned from {p.evidence} signals"
            lines.append(f"- {p.key}: {p.value} ({how})")
        return "\n".join(lines)
