"""`run_analysis_sql` implementation: the governed path from LLM-written SQL to safe results.

    guard (rewrite + policy) -> cache -> dry-run (syntax + bytes) -> cost cap -> execute
    -> PII value scan -> pseudonymise customer ids -> compact JSON for the model

Self-correction contract (requirement 5): failures come back as structured, *actionable* payloads
(error type, message, hint, attempts_left). Empty results trigger cheap diagnostics (closest valid
filter values, data date range) so the model can fix the query on the next attempt. After
`max_sql_attempts` failed attempts in one turn the tool returns `give_up` and the model must
explain the limitation instead of looping - which bounds cost.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp

from retail_agent.agent.services import Services
from retail_agent.observability.trace_store import TurnState
from retail_agent.safety.pii import scan_dataframe
from retail_agent.safety.policy import Entitlement
from retail_agent.safety.sql_guard import GuardViolation
from retail_agent.warehouse.base import ErrorKind, WarehouseError

PROBE_COLUMNS = {
    "brand": "products", "category": "products", "department": "products", "name": "products",
    "status": "order_items", "country": "users", "state": "users", "city": "users", "traffic_source": "users",
}


def to_records(df: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    out = []
    for row in df.head(limit).to_dict(orient="records"):
        clean = {}
        for k, v in row.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                clean[str(k)] = None
            elif isinstance(v, pd.Timestamp) and v == v.normalize():
                clean[str(k)] = v.date().isoformat()
            elif isinstance(v, pd.Timestamp | dt.datetime | dt.date):
                clean[str(k)] = v.isoformat()[:19]
            elif hasattr(v, "item"):
                v = v.item()
                clean[str(k)] = round(v, 4) if isinstance(v, float) else v
            elif isinstance(v, float):
                clean[str(k)] = round(v, 4)
            else:
                clean[str(k)] = v
        out.append(clean)
    return out


def _numeric_summary(df: pd.DataFrame) -> dict[str, dict[str, float]]:
    summary = {}
    for c in df.select_dtypes("number").columns[:8]:
        s = df[c].dropna()
        if len(s):
            summary[str(c)] = {"min": round(float(s.min()), 4), "max": round(float(s.max()), 4), "sum": round(float(s.sum()), 4)}
    return summary


class AnalysisService:
    def __init__(self, services: Services):
        self.s = services

    # ------------------------------------------------------------------ public
    def run(self, sql: str, purpose: str, ent: Entitlement, session_id: str, turn: TurnState | None) -> dict[str, Any]:
        cfg = self.s.settings
        turn = turn or TurnState(trace_id="adhoc", session_id=session_id, user_id=ent.user_id)
        failures_so_far = turn.sql_failures + turn.sql_empty
        if failures_so_far >= cfg.max_sql_attempts:
            return self._give_up(turn)
        if turn.cost_usd >= cfg.budget_usd_per_question:
            turn.budget_exceeded = True
            return {"status": "budget_exceeded", "message": "The cost budget for this question is used up. "
                    "Answer with the results you already have and say what is missing."}
        turn.sql_attempts += 1
        attempt = turn.sql_attempts
        span = turn.span("tool", "run_analysis_sql", sql=sql, purpose=purpose[:200], attempt=attempt)
        t0 = dt.datetime.now()
        try:
            out = self._run(sql, ent, session_id, turn, span.attributes)
        finally:
            span.duration_ms = (dt.datetime.now() - t0).total_seconds() * 1000
        span.status = out["status"]
        if out["status"] == "ok" and failures_so_far > 0:
            turn.sql_self_corrected += 1
            out["self_corrected"] = True
        if out["status"] in ("error", "empty"):
            left = cfg.max_sql_attempts - (turn.sql_failures + turn.sql_empty)
            out["attempts_left"] = max(left, 0)
            out["instruction"] = (
                "Fix the SQL using the hint and call run_analysis_sql again." if left > 0 else
                "No attempts left: stop querying and explain to the user what you tried and why it did not work."
            )
        return out

    # ------------------------------------------------------------------ internals
    def _give_up(self, turn: TurnState) -> dict[str, Any]:
        turn.span("guard", "sql_attempts_exhausted", status="blocked")
        return {
            "status": "give_up",
            "message": f"{self.s.settings.max_sql_attempts} query attempts failed for this question. Do not retry. "
            "Explain briefly to the user what was attempted, what blocked it and suggest a rephrasing.",
        }

    def _run(self, sql: str, ent: Entitlement, session_id: str, turn: TurnState, attrs: dict[str, Any]) -> dict[str, Any]:
        cfg = self.s.settings
        try:
            checked = self.s.guard().check(sql, ent)
        except GuardViolation as v:
            turn.sql_failures += 1
            if v.kind == "policy":
                turn.guard_blocks += 1
            attrs.update(error_type=v.kind, error=v.message)
            return {"status": "error", "error_type": "policy_violation" if v.kind == "policy" else "syntax_error",
                    "message": v.message, "hint": v.hint}
        attrs["final_sql"] = checked.sql[:4000]
        key = hashlib.sha256(f"{ent.user_id}|{checked.sql}".encode()).hexdigest()
        cached = self.s.result_cache.get(key)
        if cached is not None:
            attrs["cache_hit"] = True
            df = cached
            bytes_processed = 0
        else:
            try:
                dry = self.s.warehouse.dry_run(checked.sql)
                attrs["estimated_bytes"] = dry.bytes_processed
                if dry.bytes_processed > cfg.bq_max_bytes_billed:
                    raise WarehouseError(ErrorKind.COST, f"Query would scan {dry.bytes_processed / 1e9:.1f} GB "
                                         f"(cap {cfg.bq_max_bytes_billed / 1e9:.1f} GB).",
                                         "Restrict created_at to a smaller window or select fewer columns.")
                res = self.s.warehouse.execute(checked.sql, max_rows=cfg.max_result_rows + 1)
            except WarehouseError as e:
                attrs.update(error_type=e.kind.value, error=e.message)
                if e.kind in (ErrorKind.UNAVAILABLE, ErrorKind.TRANSIENT, ErrorKind.AUTH, ErrorKind.INTERNAL):
                    # not the model's fault: do not burn a correction attempt, tell it to stop
                    turn.error = f"warehouse {e.kind.value}: {e.message}"
                    return {"status": "unavailable", "error_type": e.kind.value,
                            "message": "The data warehouse is temporarily unavailable. Do not retry now; "
                            "tell the user to try again in a minute. " + (e.hint or "")}
                turn.sql_failures += 1
                return {"status": "error", "error_type": e.kind.value, "message": e.message, "hint": e.hint
                        or "Check column names with describe_schema and BigQuery syntax."}
            df = res.df
            bytes_processed = res.bytes_billed or res.bytes_processed
            turn.bq_bytes += bytes_processed
            turn.bq_cost_usd += bytes_processed / 2**40 * cfg.bq_price_per_tib
            self.s.result_cache.set(key, df)
            attrs.update(bytes=bytes_processed, warehouse_ms=round(res.duration_ms))
            if res.job_id:  # joins this trace to BigQuery's own job history (INFORMATION_SCHEMA.JOBS)
                attrs["job_id"] = res.job_id

        truncated = len(df) > cfg.max_result_rows
        df = df.head(cfg.max_result_rows)
        df, scan = scan_dataframe(df)
        if scan.total:
            turn.pii_redactions += scan.total
            attrs["pii_masked"] = scan.findings
        df = self.s.pseudonymizer.apply(df, checked.pseudonymize_columns)
        attrs["rows"] = len(df)

        if df.empty:
            turn.sql_empty += 1
            notes = list(checked.notes)
            return {"status": "empty", "message": "The query ran but returned 0 rows.",
                    "diagnostics": self._diagnose_empty(sql, ent, notes), "notes": notes,
                    "hint": "Check filter values (see diagnostics), the date window, and join keys."}

        query_id = f"q{turn.sql_attempts}-{turn.trace_id[:6]}"
        self.s.remember_result(session_id, query_id, df)
        turn.result_frames.append(df)  # for the numeric grounding check at the end of the turn
        payload: dict[str, Any] = {
            "status": "ok",
            "query_id": query_id,
            "columns": [str(c) for c in df.columns],
            "row_count": len(df),
            "truncated": truncated,
            "rows": to_records(df, cfg.rows_shown_to_llm),
            "notes": list(checked.notes) + (["customer ids are pseudonymised"] if checked.pseudonymize_columns else []),
        }
        if len(df) > cfg.rows_shown_to_llm:
            payload["summary"] = _numeric_summary(df)
            payload["notes"].append(f"only the first {cfg.rows_shown_to_llm} rows are shown; summary covers all rows")
        if truncated:
            payload["notes"].append(f"result truncated to {cfg.max_result_rows} rows - aggregate further if totals matter")
        return payload

    # ------------------------------------------------------------------ empty-result diagnostics
    def _diagnose_empty(self, sql: str, ent: Entitlement, notes: list[str]) -> dict[str, Any]:
        diag: dict[str, Any] = {}
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except Exception:  # noqa: BLE001
            return diag
        literals: list[tuple[str, str]] = []
        for node in tree.find_all(exp.EQ, exp.In, exp.Like, exp.ILike):
            col = node.this if isinstance(node.this, exp.Column) else None
            if col is None or col.name.lower() not in PROBE_COLUMNS:
                continue
            vals = node.expressions if isinstance(node, exp.In) else [node.expression]
            for v in vals:
                if isinstance(v, exp.Literal) and v.is_string:
                    literals.append((col.name.lower(), v.this))
        seen = set()
        for col, value in literals[:3]:
            if col in seen:
                continue
            seen.add(col)
            diag[f"valid_{col}_values_like_{value[:20]}"] = self._probe(col, value, ent)
        diag["data_date_range"] = self._date_range(ent)
        if notes:
            diag["privacy_note"] = "Groups with fewer than the minimum number of customers are hidden; very narrow segments return no rows."
        if not ent.unrestricted:
            diag["entitlement_note"] = f"This user can only see products where {ent.describe()}."
        return diag

    def _probe(self, col: str, value: str, ent: Entitlement) -> list[str]:
        table = PROBE_COLUMNS[col]
        alias = table[0]
        frag = re.sub(r"[^a-z0-9 ]", "", value.lower())[:5].strip()
        count = f"COUNT(DISTINCT {alias}.id)" if table == "users" else "COUNT(*)"
        where = f"WHERE LOWER({alias}.{col}) LIKE '%{frag}%'" if frag else ""
        sql = f"SELECT {alias}.{col} AS value, {count} AS n FROM {table} AS {alias} {where} GROUP BY value ORDER BY n DESC LIMIT 8"
        try:
            checked = self.s.guard().check(sql, ent)
            df = self.s.warehouse.execute(checked.sql, 8).df
            if df.empty and frag:
                checked = self.s.guard().check(sql.replace(where, ""), ent)
                df = self.s.warehouse.execute(checked.sql, 8).df
            return [str(v) for v in df["value"].tolist()]
        except (GuardViolation, WarehouseError):
            return []

    def _date_range(self, ent: Entitlement) -> str:
        if ent.user_id in self.s.date_range_cache:
            return self.s.date_range_cache[ent.user_id]
        sql = "SELECT MIN(DATE(oi.created_at)) AS first_day, MAX(DATE(oi.created_at)) AS last_day FROM order_items AS oi"
        try:
            df = self.s.warehouse.execute(self.s.guard().check(sql, ent).sql, 1).df
            txt = f"{df['first_day'][0]} to {df['last_day'][0]}"
        except (GuardViolation, WarehouseError):
            txt = "unknown"
        self.s.date_range_cache[ent.user_id] = txt
        return txt
