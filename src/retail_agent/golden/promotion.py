"""Keeping the golden bucket fresh without a human bottleneck (requirement 1 + 4b).

Sources of new trios:
  * analysts drop YAML files into `candidates/` (or GCS `candidates/` prefix in prod);
  * `mine_candidates` turns positively-rated, successful agent turns into candidates
    (question + the final SQL that worked + the answer as the "report").

Automated gate (every candidate, and nightly re-validation of active trios):
  1. schema        - required fields present, sane lengths
  2. safety        - SQL passes the SQL guard (read-only, PII-free, k-anonymity)
  3. executes      - SQL runs on the warehouse (dry-run + execute) and returns rows
  4. not_duplicate - not the same SQL / near-identical question as an active trio
  5. prompt_safe   - question, interpretation and report text pass the same injection / PII heuristics
                     as user input: promoted trios are injected into *other users'* system prompts, so
                     a mined conversation must not become a cross-user prompt-injection channel
  6. judge         - optional LLM judge: does the SQL answer the question? (score >= 0.7)
Pass -> promoted to active. Fail -> stays candidate with reasons. Active trios whose SQL starts
failing (schema drift) are deprecated automatically. Humans only review the *exceptions* (and can
always veto by setting status: deprecated), which is what lets curation scale.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from retail_agent.golden.retriever import tokenize
from retail_agent.golden.store import Trio, TrioStore
from retail_agent.observability.trace_store import TraceStore
from retail_agent.safety.input_guard import heuristic_check
from retail_agent.safety.policy import Entitlement
from retail_agent.safety.sql_guard import GuardViolation, SqlGuard
from retail_agent.warehouse.base import Warehouse, WarehouseError

SERVICE_ENTITLEMENT = Entitlement("golden-gate", "Golden bucket service", "service", unrestricted=True)


@dataclass
class GateResult:
    trio_id: str
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


class PromotionGate:
    def __init__(
        self,
        store: TrioStore,
        guard: SqlGuard,
        warehouse: Warehouse,
        judge: Callable[[str, str, str], float] | None = None,
        duplicate_threshold: float = 0.85,
    ):
        self.store = store
        self.guard = guard
        self.warehouse = warehouse
        self.judge = judge  # (question, sql, sample_rows_json) -> score 0..1
        self.duplicate_threshold = duplicate_threshold

    def evaluate(self, trio: Trio, against: list[Trio] | None = None) -> GateResult:
        res = GateResult(trio.id, passed=False)
        res.checks["schema"] = bool(trio.question.strip()) and len(trio.sql) > 20 and len(trio.question) < 500
        if not res.checks["schema"]:
            res.reasons.append("missing/invalid question or SQL")
        rows_json = "[]"
        try:
            checked = self.guard.check(trio.sql, SERVICE_ENTITLEMENT)
            res.checks["safety"] = True
            try:
                self.warehouse.dry_run(checked.sql)
                df = self.warehouse.execute(checked.sql, max_rows=20).df
                res.checks["executes"] = True
                res.checks["non_empty"] = not df.empty
                if df.empty:
                    res.reasons.append("query returns no rows")
                rows_json = df.head(10).to_json(orient="records", date_format="iso")
            except WarehouseError as e:
                res.checks["executes"] = False
                res.reasons.append(f"execution failed: {e.message}")
        except GuardViolation as e:
            res.checks["safety"] = False
            res.reasons.append(f"guard: {e.message}")
        others = [t for t in (against if against is not None else self.store.active()) if t.id != trio.id]
        dup = next(
            (o.id for o in others if o.normalized_sql() == trio.normalized_sql() or _jaccard(o.question, trio.question) >= self.duplicate_threshold),
            None,
        )
        res.checks["not_duplicate"] = dup is None
        if dup:
            res.reasons.append(f"duplicate of {dup}")
        unsafe = [v for v in (heuristic_check(x) for x in [trio.question, trio.analyst_report, *trio.interpretation])
                  if not v.allowed]
        res.checks["prompt_safe"] = not unsafe
        if unsafe:
            res.reasons.append(f"text not safe to inject into prompts: {unsafe[0].category}")
        if self.judge and all(res.checks.values()):
            try:
                score = float(self.judge(trio.question, trio.sql, rows_json))
            except Exception as e:  # noqa: BLE001
                score = 0.0
                res.reasons.append(f"judge unavailable: {type(e).__name__}")
            res.checks["judge"] = score >= 0.7
            if score < 0.7:
                res.reasons.append(f"judge score {score:.2f} < 0.70")
        res.passed = all(res.checks.values())
        return res

    def promote_candidates(self) -> list[GateResult]:
        results = []
        for cand in self.store.candidates():
            r = self.evaluate(cand)
            results.append(r)
            if r.passed:
                promoted = cand.model_copy(update={
                    "status": "active",
                    "last_validated_at": datetime.now(timezone.utc).date().isoformat(),
                })
                self.store.write(promoted)
                self.store.remove_candidate(cand.id)
        return results

    def revalidate_active(self) -> list[GateResult]:
        """Nightly job: deprecate trios that no longer run (schema drift, renamed values...)."""
        results = []
        actives = self.store.active()
        for t in actives:
            r = self.evaluate(t, against=actives)
            r.checks.pop("judge", None)
            if not (r.checks.get("safety") and r.checks.get("executes")):
                self.store.write(t.model_copy(update={"status": "deprecated"}))
            else:
                self.store.write(t.model_copy(update={"last_validated_at": datetime.now(timezone.utc).date().isoformat()}))
            results.append(r)
        return results


def mine_candidates(traces: TraceStore, store: TrioStore, min_rating: int = 1) -> list[Trio]:
    """System-level learning: positively-rated, successful turns become candidate trios."""
    rows = traces.query(
        """SELECT t.trace_id, t.question, t.answer_preview, t.user_id FROM turns t
           JOIN feedback f ON f.trace_id = t.trace_id
           WHERE f.rating >= ? AND t.status = 'ok' AND t.sql_attempts > 0""",
        (min_rating,),
    )
    existing = {t.id for t in store.active()} | {t.id for t in store.candidates()}
    out = []
    for r in rows:
        tid = f"mined_{r['trace_id'][:10]}"
        if tid in existing:
            continue
        sql = None
        for s in reversed(traces.spans(r["trace_id"])):
            if s["name"] == "run_analysis_sql" and s["status"] == "ok" and s["attributes"].get("sql"):
                sql = s["attributes"]["sql"]
                break
        if not sql:
            continue
        trio = Trio(
            id=tid,
            question=r["question"],
            intent="mined",
            tags=sorted(set(tokenize(r["question"])))[:8],
            sql=sql,
            interpretation=["Mined from a positively rated conversation; interpretation inferred from the accepted answer."],
            analyst_report=re.sub(r"\s+", " ", r["answer_preview"] or "")[:600],
            author=f"user:{r['user_id']}",
            source="mined",
            status="candidate",
            quality_score=0.6,
        )
        store.write(trio, candidate=True)
        out.append(trio)
    return out


JUDGE_PROMPT = """You review SQL written for a retail analytics question.
Question: {question}
SQL:
{sql}
First rows of the result (JSON): {rows}
Does the SQL correctly and fully answer the question (right metric definitions, filters, time window,
grouping)? Reply JSON {{"score": <0..1>, "reason": "<short>"}}."""


def make_llm_judge(generate: Callable[[str], str]) -> Callable[[str, str, str], float]:
    def judge(question: str, sql: str, rows: str) -> float:
        raw = generate(JUDGE_PROMPT.format(question=question, sql=sql, rows=rows[:2000]))
        m = re.search(r"\{.*\}", raw or "", re.S)
        return float(json.loads(m.group(0)).get("score", 0)) if m else 0.0

    return judge
