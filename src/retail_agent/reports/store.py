"""Saved Reports library + the strict two-phase deletion flow (requirement 3).

Reports are private to their author: *every* query here is scoped by owner_id at the repository
layer, so neither the LLM nor a tool bug can reach another user's reports.

Deletion is never executed by the LLM. The agent can only *request* a deletion:

  phase 1  request_deletion(owner, session, scope)  ->  PendingDeletion
           exact list of reports resolved now, snapshot hash, single-use token, 5 min TTL
  phase 2  confirm(token, owner, session, typed_phrase)  (called by the CLI / UI, not by the model)
           * token exists, not used, not expired, same owner + same session
           * typed phrase must be exactly "DELETE <n>" (n = number of reports listed)
           * the reports still exist, are still owned by the user and have not changed
             (snapshot hash) - otherwise the user must re-request
           -> hard delete (no recovery, per client requirement) + audit record.

"Hard delete" is meant literally: SQLite runs with secure_delete (freed pages are zeroed, so the text
is not recoverable from the file) and the FTS index uses secure-delete too; the audit log keeps ids
and counts, never titles or content. The single-use check is an atomic compare-and-set, so two
concurrent confirmations cannot both execute.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, session_id TEXT, title TEXT NOT NULL,
  content TEXT NOT NULL, sql_used TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports(owner_id, session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS reports_fts USING fts5(title, content, content='reports', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS reports_ai AFTER INSERT ON reports BEGIN
  INSERT INTO reports_fts(rowid, title, content) VALUES (new.id, new.title, new.content); END;
CREATE TRIGGER IF NOT EXISTS reports_ad AFTER DELETE ON reports BEGIN
  INSERT INTO reports_fts(reports_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content); END;
CREATE TRIGGER IF NOT EXISTS reports_au AFTER UPDATE ON reports BEGIN
  INSERT INTO reports_fts(reports_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content);
  INSERT INTO reports_fts(rowid, title, content) VALUES (new.id, new.title, new.content); END;
CREATE TABLE IF NOT EXISTS pending_actions (
  token TEXT PRIMARY KEY, owner_id TEXT NOT NULL, session_id TEXT NOT NULL, action TEXT NOT NULL,
  report_ids TEXT NOT NULL, snapshot TEXT NOT NULL, description TEXT, created_at REAL NOT NULL,
  expires_at REAL NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, owner_id TEXT, session_id TEXT, action TEXT, detail TEXT
);
"""

Scope = Literal["this_conversation", "matching", "ids", "all"]


@dataclass
class Report:
    id: int
    owner_id: str
    session_id: str | None
    title: str
    content: str
    created_at: str

    def brief(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "created_at": self.created_at[:16].replace("T", " ")}


@dataclass
class PendingDeletion:
    token: str
    owner_id: str
    session_id: str
    reports: list[Report]
    description: str
    expires_at: float

    @property
    def confirm_phrase(self) -> str:
        return f"DELETE {len(self.reports)}"


class DeletionError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _snapshot(reports: list[Report]) -> str:
    payload = json.dumps([(r.id, r.title, r.content) for r in sorted(reports, key=lambda r: r.id)])
    return hashlib.sha256(payload.encode()).hexdigest()


def _fts_query(text: str) -> str:
    # Phrase query; quotes doubled for FTS5 syntax. Matches title or content.
    clean = " ".join(text.replace('"', '""').split())
    return f'"{clean}"'


class ReportStore:
    def __init__(self, db_path: Path, ttl_s: float = 300.0):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA secure_delete = ON")
        self._con.executescript(SCHEMA)
        # FTS5 secure-delete (SQLite >= 3.44): deleted tokens are removed from the index, not just
        # tombstoned. Older SQLite: run the FTS 'optimize' command periodically instead.
        with contextlib.suppress(sqlite3.OperationalError):
            self._con.execute("INSERT INTO reports_fts(reports_fts, rank) VALUES ('secure-delete', 1)")
        self._lock = threading.Lock()
        self.ttl_s = ttl_s

    # ------------------------------------------------------------------ CRUD (owner-scoped)
    def save(self, owner_id: str, session_id: str | None, title: str, content: str, sql_used: list[str] | None = None) -> Report:
        ts = _now()
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO reports (owner_id, session_id, title, content, sql_used, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (owner_id, session_id, title.strip()[:200], content, json.dumps(sql_used or []), ts, ts),
            )
            rid = cur.lastrowid
        self._audit(owner_id, session_id, "save_report", {"id": rid})
        return Report(rid, owner_id, session_id, title, content, ts)  # type: ignore[arg-type]

    def _rows(self, sql: str, args: tuple) -> list[Report]:
        with self._lock:
            rows = self._con.execute(sql, args).fetchall()
        return [Report(r["id"], r["owner_id"], r["session_id"], r["title"], r["content"], r["created_at"]) for r in rows]

    def get(self, owner_id: str, report_id: int) -> Report | None:
        r = self._rows("SELECT * FROM reports WHERE owner_id = ? AND id = ?", (owner_id, report_id))
        return r[0] if r else None

    def list(self, owner_id: str, session_id: str | None = None, text: str | None = None, limit: int = 50) -> list[Report]:
        if text:
            return self._rows(
                "SELECT r.* FROM reports r JOIN reports_fts f ON f.rowid = r.id "
                "WHERE reports_fts MATCH ? AND r.owner_id = ? " + ("AND r.session_id = ? " if session_id else "")
                + "ORDER BY r.id DESC LIMIT ?",
                (_fts_query(text), owner_id, *([session_id] if session_id else []), limit),
            )
        if session_id:
            return self._rows("SELECT * FROM reports WHERE owner_id = ? AND session_id = ? ORDER BY id DESC LIMIT ?", (owner_id, session_id, limit))
        return self._rows("SELECT * FROM reports WHERE owner_id = ? ORDER BY id DESC LIMIT ?", (owner_id, limit))

    # ------------------------------------------------------------------ phase 1
    def request_deletion(
        self,
        owner_id: str,
        session_id: str,
        scope: Scope,
        text_query: str | None = None,
        report_ids: list[int] | None = None,
    ) -> PendingDeletion | None:
        if scope == "this_conversation":
            reports = self.list(owner_id, session_id=session_id, limit=500)
            desc = "all reports created in this conversation"
        elif scope == "matching":
            if not text_query or not text_query.strip():
                raise DeletionError("A search text is required to delete matching reports.")
            reports = self.list(owner_id, text=text_query, limit=500)
            desc = f"all reports mentioning '{text_query}'"
        elif scope == "ids":
            ids = sorted(set(int(i) for i in (report_ids or [])))
            reports = [r for i in ids if (r := self.get(owner_id, i)) is not None]
            desc = "reports " + ", ".join(f"#{i}" for i in ids)
        elif scope == "all":
            reports = self.list(owner_id, limit=500)
            desc = "ALL of your saved reports"
        else:
            raise DeletionError(f"Unknown scope {scope}")
        if not reports:
            return None
        # one live request per user+session: a new request supersedes the previous one
        with self._lock:
            self._con.execute(
                "UPDATE pending_actions SET status = 'superseded' WHERE owner_id = ? AND session_id = ? AND status = 'pending'",
                (owner_id, session_id),
            )
        token = secrets.token_urlsafe(16)
        expires = time.time() + self.ttl_s
        with self._lock:
            self._con.execute(
                "INSERT INTO pending_actions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (token, owner_id, session_id, "delete_reports", json.dumps([r.id for r in reports]),
                 _snapshot(reports), desc, time.time(), expires, "pending"),
            )
        self._audit(owner_id, session_id, "delete_requested", {"ids": [r.id for r in reports], "scope": scope})
        return PendingDeletion(token, owner_id, session_id, reports, desc, expires)

    def pending(self, owner_id: str, session_id: str) -> PendingDeletion | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM pending_actions WHERE owner_id = ? AND session_id = ? AND status = 'pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (owner_id, session_id),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] < time.time():
            self._set_status(row["token"], "expired")
            return None
        ids = json.loads(row["report_ids"])
        reports = [r for i in ids if (r := self.get(owner_id, i)) is not None]
        return PendingDeletion(row["token"], owner_id, session_id, reports, row["description"], row["expires_at"])

    # ------------------------------------------------------------------ phase 2
    def confirm(self, token: str, owner_id: str, session_id: str, typed_phrase: str) -> list[int]:
        with self._lock:
            row = self._con.execute("SELECT * FROM pending_actions WHERE token = ?", (token,)).fetchone()
        if not row or row["status"] != "pending":
            raise DeletionError("This confirmation is no longer valid. Please ask again.")
        if row["owner_id"] != owner_id or row["session_id"] != session_id:
            self._audit(owner_id, session_id, "delete_confirm_denied", {"reason": "owner/session mismatch"})
            raise DeletionError("This confirmation belongs to a different user or conversation.")
        if row["expires_at"] < time.time():
            self._set_status(token, "expired")
            raise DeletionError("The confirmation expired. Please ask again.")
        ids = json.loads(row["report_ids"])
        expected = f"DELETE {len(ids)}"
        if typed_phrase.strip() != expected:
            self._set_status(token, "cancelled")
            self._audit(owner_id, session_id, "delete_cancelled", {"ids": ids})
            raise DeletionError("Deletion cancelled - nothing was deleted.")
        reports = [r for i in ids if (r := self.get(owner_id, i)) is not None]
        if len(reports) != len(ids) or _snapshot(reports) != row["snapshot"]:
            self._set_status(token, "stale")
            raise DeletionError("The reports changed since you asked. Please review the list again.")
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                # compare-and-set: only the first confirmation of a pending token may proceed
                claimed = self._con.execute(
                    "UPDATE pending_actions SET status = 'done' WHERE token = ? AND status = 'pending'", (token,)
                ).rowcount
                if claimed != 1:
                    self._con.execute("ROLLBACK")
                    raise DeletionError("This confirmation is no longer valid. Please ask again.")
                for i in ids:
                    self._con.execute("DELETE FROM reports WHERE id = ? AND owner_id = ?", (i, owner_id))
                self._con.execute("COMMIT")
            except DeletionError:
                raise
            except Exception:
                self._con.execute("ROLLBACK")
                raise
        self._audit(owner_id, session_id, "delete_executed", {"ids": ids})
        return ids

    def cancel(self, token: str, owner_id: str) -> None:
        with self._lock:
            self._con.execute("UPDATE pending_actions SET status='cancelled' WHERE token = ? AND owner_id = ?", (token, owner_id))

    # ------------------------------------------------------------------ internals
    def _set_status(self, token: str, status: str) -> None:
        with self._lock:
            self._con.execute("UPDATE pending_actions SET status = ? WHERE token = ?", (status, token))

    def _audit(self, owner: str, session: str | None, action: str, detail: dict[str, Any]) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO audit_log (ts, owner_id, session_id, action, detail) VALUES (?,?,?,?,?)",
                (time.time(), owner, session, action, json.dumps(detail)),
            )

    def audit(self, owner_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._con.execute("SELECT * FROM audit_log WHERE owner_id = ? ORDER BY id", (owner_id,)).fetchall()]
