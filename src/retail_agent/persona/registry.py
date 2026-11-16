"""Persona management without redeployment (requirement 8).

A persona controls *tone and presentation only* (tone, style rules, report structure). Safety and
data rules are enforced in code (SQL guard, plugins, confirmation flow), the prompt's safety section
declares precedence over the persona, and the gate below rejects personas that try to change rules,
so a persona can never relax them.

Workflow for the (non-developer) editor:
    1. edit / copy a YAML file under config/personas/
    2. `retail-agent persona publish config/personas/<file>.yaml`
       -> automated quality gate -> new immutable version -> becomes active
    3. the next message of every user uses it (the instruction is rebuilt each turn)
    4. `retail-agent persona rollback` restores the previous version instantly
In production: Firestore `personas/{name}/versions/{n}` + an `active` pointer, edited from a small
admin page or a Google Sheet; the gate runs as a Cloud Run Job on change.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


class Persona(BaseModel):
    name: str = Field(min_length=2, max_length=60, pattern=r"^[a-z0-9_\-]+$")
    description: str = Field(default="", max_length=300)
    tone: str = Field(min_length=10, max_length=800)
    style_rules: list[str] = Field(default_factory=list, max_length=12)
    report_structure: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("style_rules", "report_structure")
    @classmethod
    def _short_items(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip() or len(item) > 250:
                raise ValueError("each rule must be 1-250 characters")
        return v

    def render(self) -> str:
        rules = "\n".join(f"- {r}" for r in self.style_rules)
        structure = " -> ".join(self.report_structure) if self.report_structure else "(free)"
        return f"Persona: {self.name}\nTone: {self.tone}\nStyle rules:\n{rules}\nReport structure: {structure}"


# Instructions a persona may not contain: anything that tries to change capabilities or safety.
FORBIDDEN = [
    (r"\b(ignore|override|bypass|disable|disregard|forget)\b.{0,40}\b(rule|policy|polic(y|ies)|guard|safety|restriction|instruction)s?", "tries to override rules"),
    (r"\b(e-?mail|phone|address|first name|last name|full name|postal|zip|latitude|longitude|ip)\b.{0,30}\b(show|reveal|include|list|display|share)|\b(show|reveal|include|list|display|share)\b.{0,30}\b(e-?mails?|phone|address(es)?|first names?|last names?|full names?|postal|zip|coordinates)\b", "asks for personal data"),
    # Actions on data or reports, not single words: "call out any revenue drop" and "start with an
    # update on KPIs" are normal tone rules and must pass (a non-developer cannot debug a false reject).
    (r"\b(delete|remove|erase|purge|drop|truncate)\b.{0,30}\b(reports?|data|tables?|records?|rows?|orders?|customers?|users?)\b"
     r"|\b(update|insert|modify|change|overwrite)\b.{0,20}\b(the\s+)?(data(base)?|tables?|records?|rows?)\b",
     "mentions data-changing operations"),
    (r"\b(system prompt|developer mode|jailbreak|tool call|function call)\b", "references internal mechanics"),
    (r"https?://", "contains links"),
    (r"\b(all users|other users'?|every user'?s) (data|reports)\b", "cross-user access"),
]


@dataclass
class GateReport:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


class PersonaQualityGate:
    """Automated gate: schema -> policy lint -> behavioural checks (optional, e.g. eval subset)."""

    def __init__(self, behaviour_check: Callable[[Persona], list[str]] | None = None):
        self.behaviour_check = behaviour_check

    def run(self, raw: dict) -> tuple[Persona | None, GateReport]:
        rep = GateReport(passed=False)
        try:
            persona = Persona(**raw)
            rep.checks["schema"] = True
        except ValidationError as e:
            rep.checks["schema"] = False
            rep.problems += [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
            return None, rep
        text = " ".join([persona.tone, persona.description, *persona.style_rules, *persona.report_structure])
        lint = [f"policy: {why} ('{m.group(0)[:40]}')" for pat, why in FORBIDDEN if (m := re.search(pat, text, re.I))]
        rep.checks["policy_lint"] = not lint
        rep.problems += lint
        if self.behaviour_check and not lint:
            try:
                failures = self.behaviour_check(persona)
            except Exception as e:  # noqa: BLE001 - a gate that cannot run must not pass
                failures = [f"behaviour check could not run: {type(e).__name__}: {e}"]
            rep.checks["behaviour"] = not failures
            rep.problems += failures
        rep.passed = all(rep.checks.values())
        return (persona if rep.passed else None), rep


SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, content TEXT, published_by TEXT, published_at REAL,
  gate_report TEXT, active INTEGER DEFAULT 0
);
"""


class PersonaRegistry:
    def __init__(self, db_path: Path, default_file: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._con.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.default_file = default_file

    def active(self) -> tuple[Persona, str]:
        """Returns (persona, version label). Cheap: one indexed read per turn."""
        with self._lock:
            row = self._con.execute("SELECT id, content FROM persona_versions WHERE active = 1 ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            return Persona(**json.loads(row[1])), f"v{row[0]}"
        data = yaml.safe_load(self.default_file.read_text(encoding="utf-8"))
        return Persona(**data), "default"

    def publish(self, path: Path, gate: PersonaQualityGate, published_by: str = "editor") -> GateReport:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        persona, rep = gate.run(raw)
        if persona is None:
            return rep
        with self._lock:
            self._con.execute("BEGIN")
            self._con.execute("UPDATE persona_versions SET active = 0")
            self._con.execute(
                "INSERT INTO persona_versions (name, content, published_by, published_at, gate_report, active) VALUES (?,?,?,?,?,1)",
                (persona.name, persona.model_dump_json(), published_by, time.time(), json.dumps(rep.checks)),
            )
            self._con.execute("COMMIT")
        return rep

    def rollback(self) -> str | None:
        with self._lock:
            rows = self._con.execute("SELECT id, active FROM persona_versions ORDER BY id DESC").fetchall()
            ids = [r[0] for r in rows]
            current = next((r[0] for r in rows if r[1]), None)
            if current is None or ids.index(current) + 1 >= len(ids):
                return None
            prev = ids[ids.index(current) + 1]
            self._con.execute("UPDATE persona_versions SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (prev,))
        return f"v{prev}"

    def history(self) -> list[dict]:
        with self._lock:
            rows = self._con.execute("SELECT id, name, published_by, published_at, active FROM persona_versions ORDER BY id DESC").fetchall()
        return [{"version": f"v{r[0]}", "name": r[1], "by": r[2], "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[3])), "active": bool(r[4])} for r in rows]
