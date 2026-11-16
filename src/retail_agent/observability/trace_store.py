"""Turn/span store for deep-dive debugging and agent metrics.

Every user turn gets a trace_id (= ADK invocation id). Spans record each model call (the message
delta sent, the response parts, tokens, latency), each tool call (args such as the SQL, result
status, rows, errors), guard decisions and errors. A turn row aggregates the counters used for
metrics. Storage: SQLite (prototype) + JSON lines (ship to Cloud Logging / BigQuery in prod).

Logs are a data sink too: everything is passed through the PII redactor at write time (user
questions, model text, tool arguments and previews), and saved-report bodies are never logged
(only their length), so a hard-deleted report does not survive in the traces.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from retail_agent.safety.pii import ScanReport, redact_text

_BODY_KEYS = {"content_markdown", "content", "content_preview"}


def scrub(value: Any, report: ScanReport | None = None, _key: str = "") -> Any:
    """Recursively redact PII-looking strings and elide report bodies before persisting."""
    if isinstance(value, str):
        if _key in _BODY_KEYS and len(value) > 120:
            return f"<{len(value)} chars of report text, not logged>"
        return redact_text(value, report)[0]
    if isinstance(value, dict):
        return {k: scrub(v, report, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v, report, _key) for v in value]
    return value

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
  trace_id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT, started_at REAL, ended_at REAL,
  latency_ms REAL, question TEXT, answer_preview TEXT, status TEXT, refusal_category TEXT,
  model_calls INTEGER, tool_calls INTEGER, sql_attempts INTEGER, sql_failures INTEGER,
  sql_empty INTEGER, sql_self_corrected INTEGER, guard_blocks INTEGER, pii_redactions INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, llm_cost_usd REAL, bq_bytes INTEGER,
  bq_cost_usd REAL, fallback_used INTEGER, degraded INTEGER, budget_exceeded INTEGER, error TEXT,
  persona TEXT, golden_ids TEXT, provider TEXT
);
CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT PRIMARY KEY, trace_id TEXT, seq INTEGER, kind TEXT, name TEXT, started_at REAL,
  duration_ms REAL, status TEXT, attributes TEXT
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id, seq);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, user_id TEXT, rating INTEGER,
  comment TEXT, created_at REAL
);
"""


@dataclass
class Span:
    kind: str  # model | tool | guard | error | event
    name: str
    started_at: float
    duration_ms: float = 0.0
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class TurnState:
    trace_id: str
    session_id: str
    user_id: str
    question: str = ""
    provider: str = ""
    started_at: float = field(default_factory=time.time)
    answer: str = ""
    status: str = "ok"  # ok | refused | error | budget_exceeded | degraded
    refusal_category: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    sql_attempts: int = 0
    sql_failures: int = 0
    sql_empty: int = 0
    sql_self_corrected: int = 0
    guard_blocks: int = 0
    pii_redactions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    llm_cost_usd: float = 0.0
    bq_bytes: int = 0
    bq_cost_usd: float = 0.0
    fallback_used: bool = False
    degraded: bool = False
    budget_exceeded: bool = False
    error: str = ""
    persona: str = ""
    golden_ids: list[str] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    result_frames: list[Any] = field(default_factory=list)  # this turn's guarded results (memory only)
    _open: dict[str, float] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return self.llm_cost_usd + self.bq_cost_usd

    def span(self, kind: str, name: str, status: str = "ok", duration_ms: float = 0.0, **attrs: Any) -> Span:
        s = Span(kind=kind, name=name, started_at=time.time(), duration_ms=duration_ms, status=status, attributes=attrs)
        self.spans.append(s)
        return s


class TurnRegistry:
    """In-flight turns keyed by invocation id (shared by plugins and tools)."""

    def __init__(self) -> None:
        self._turns: dict[str, TurnState] = {}
        self._lock = threading.Lock()

    def start(self, state: TurnState) -> TurnState:
        with self._lock:
            self._turns[state.trace_id] = state
        return state

    def get(self, trace_id: str | None) -> TurnState | None:
        if not trace_id:
            return None
        with self._lock:
            return self._turns.get(trace_id)

    def pop(self, trace_id: str) -> TurnState | None:
        with self._lock:
            return self._turns.pop(trace_id, None)


class TraceStore:
    def __init__(self, db_path: Path, jsonl_path: Path | None = None):
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(SCHEMA)
        # additive migration for stores created by an earlier version
        cols = {r[1] for r in self._con.execute("PRAGMA table_info(turns)")}
        if "cached_tokens" not in cols:
            self._con.execute("ALTER TABLE turns ADD COLUMN cached_tokens INTEGER DEFAULT 0")

    def save_turn(self, t: TurnState) -> None:
        ended = time.time()
        row = {
            "trace_id": t.trace_id, "session_id": t.session_id, "user_id": t.user_id,
            "started_at": t.started_at, "ended_at": ended, "latency_ms": (ended - t.started_at) * 1000,
            "question": scrub(t.question), "answer_preview": scrub((t.answer or "")[:500]), "status": t.status,
            "refusal_category": t.refusal_category, "model_calls": t.model_calls, "tool_calls": t.tool_calls,
            "sql_attempts": t.sql_attempts, "sql_failures": t.sql_failures, "sql_empty": t.sql_empty,
            "sql_self_corrected": t.sql_self_corrected, "guard_blocks": t.guard_blocks,
            "pii_redactions": t.pii_redactions, "input_tokens": t.input_tokens, "output_tokens": t.output_tokens, "cached_tokens": t.cached_tokens,
            "llm_cost_usd": round(t.llm_cost_usd, 6), "bq_bytes": t.bq_bytes, "bq_cost_usd": round(t.bq_cost_usd, 6),
            "fallback_used": int(t.fallback_used), "degraded": int(t.degraded),
            "budget_exceeded": int(t.budget_exceeded), "error": scrub(t.error[:1000]), "persona": t.persona,
            "golden_ids": json.dumps(t.golden_ids), "provider": t.provider,
        }
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self._lock:
            self._con.execute(f"INSERT OR REPLACE INTO turns ({cols}) VALUES ({qs})", list(row.values()))
            for i, s in enumerate(t.spans):
                self._con.execute(
                    "INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?,?)",
                    (s.span_id, t.trace_id, i, s.kind, s.name, s.started_at, s.duration_ms, s.status,
                     json.dumps(scrub(json.loads(json.dumps(s.attributes, default=str))))),
                )
        if self.jsonl_path:
            rec = {**row, "spans": [scrub(json.loads(json.dumps(asdict(s), default=str))) for s in t.spans]}
            with self._lock, open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    def add_feedback(self, trace_id: str, user_id: str, rating: int, comment: str = "") -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO feedback (trace_id, user_id, rating, comment, created_at) VALUES (?,?,?,?,?)",
                (trace_id, user_id, rating, comment, time.time()),
            )

    # ------------------------------------------------------------------ queries
    def turn(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._con.execute("SELECT * FROM turns WHERE trace_id LIKE ? ORDER BY started_at DESC", ("%" + trace_id.strip() + "%",)).fetchone()
        return dict(r) if r else None

    def spans(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute("SELECT * FROM spans WHERE trace_id = ? ORDER BY seq", (trace_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["attributes"] = json.loads(d["attributes"] or "{}")
            out.append(d)
        return out

    def last_trace_id(self, session_id: str | None = None, user_id: str | None = None) -> str | None:
        q, args = "SELECT trace_id FROM turns", []
        conds = []
        if session_id:
            conds.append("session_id = ?")
            args.append(session_id)
        if user_id:
            conds.append("user_id = ?")
            args.append(user_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY started_at DESC LIMIT 1"
        with self._lock:
            r = self._con.execute(q, args).fetchone()
        return r[0] if r else None

    def recent_turns(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM turns"
        args: list[Any] = []
        if status:
            q += " WHERE status = ?"
            args.append(status)
        q += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._con.execute(q, args).fetchall()]

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, args).fetchall()]
