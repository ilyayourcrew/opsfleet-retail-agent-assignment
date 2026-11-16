"""Agent-level metrics computed from the trace store (`retail-agent stats`).

In production the same definitions run as scheduled BigQuery queries over the log sink and feed
Cloud Monitoring dashboards and alerts (see docs/OBSERVABILITY.md).
"""

from __future__ import annotations

import json
import time
from typing import Any

from retail_agent.observability.trace_store import TraceStore


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, round(p / 100 * (len(vals) - 1))))
    return vals[idx]


def compute_metrics(store: TraceStore, since_hours: float = 24 * 7) -> dict[str, Any]:
    since = time.time() - since_hours * 3600
    turns = store.query("SELECT * FROM turns WHERE started_at >= ?", (since,))
    n = len(turns)
    if n == 0:
        return {"turns": 0}
    lat = [t["latency_ms"] for t in turns]
    sql_turns = [t for t in turns if t["sql_attempts"]]
    fb = store.query("SELECT rating FROM feedback WHERE created_at >= ?", (since,))
    by_status: dict[str, int] = {}
    for t in turns:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    refusals: dict[str, int] = {}
    for t in turns:
        if t["refusal_category"]:
            refusals[t["refusal_category"]] = refusals.get(t["refusal_category"], 0) + 1
    sql_attempts = sum(t["sql_attempts"] for t in turns)
    grounding = [json.loads(r["attributes"]).get("share") for r in store.query(
        "SELECT s.attributes FROM spans s JOIN turns t ON t.trace_id = s.trace_id "
        "WHERE s.name = 'numeric_grounding' AND t.started_at >= ?", (since,))]
    grounding = [g for g in grounding if g is not None]
    return {
        "window_hours": since_hours,
        "turns": n,
        "users": len({t["user_id"] for t in turns}),
        "status_breakdown": by_status,
        "success_rate": round(by_status.get("ok", 0) / n, 3),
        "error_rate": round(by_status.get("error", 0) / n, 3),
        "refusal_breakdown": refusals,
        "latency_ms_p50": round(_pct(lat, 50)),
        "latency_ms_p95": round(_pct(lat, 95)),
        "avg_model_calls_per_turn": round(sum(t["model_calls"] for t in turns) / n, 2),
        "sql_attempts": sql_attempts,
        "sql_failure_rate": round(sum(t["sql_failures"] for t in turns) / sql_attempts, 3) if sql_attempts else 0.0,
        "sql_empty_rate": round(sum(t["sql_empty"] for t in turns) / sql_attempts, 3) if sql_attempts else 0.0,
        "self_correction_success": sum(t["sql_self_corrected"] for t in turns),
        "turns_needing_retry": sum(1 for t in sql_turns if t["sql_attempts"] > 1),
        "guard_blocks": sum(t["guard_blocks"] for t in turns),
        "pii_redactions": sum(t["pii_redactions"] for t in turns),
        "fallback_model_turns": sum(t["fallback_used"] for t in turns),
        "degraded_turns": sum(t["degraded"] for t in turns),
        "budget_exceeded_turns": sum(t["budget_exceeded"] for t in turns),
        "tokens_in": sum(t["input_tokens"] for t in turns),
        "tokens_out": sum(t["output_tokens"] for t in turns),
        "cost_usd_total": round(sum(t["llm_cost_usd"] + t["bq_cost_usd"] for t in turns), 4),
        "cost_usd_per_turn": round(sum(t["llm_cost_usd"] + t["bq_cost_usd"] for t in turns) / n, 5),
        "bq_gb_scanned": round(sum(t["bq_bytes"] for t in turns) / 1e9, 3),
        "numeric_grounding_mean": round(sum(grounding) / len(grounding), 3) if grounding else None,
        "turns_with_ungrounded_numbers": sum(1 for g in grounding if g < 0.8),
        "feedback_count": len(fb),
        "feedback_positive_rate": round(sum(1 for f in fb if f["rating"] > 0) / len(fb), 3) if fb else None,
    }
