"""Golden knowledge bucket (requirement 1): every seeded trio is safe and runnable; retrieval finds
the right analyst logic for paraphrased questions; the promotion gate works without humans."""

from __future__ import annotations

import pytest

from retail_agent.config import PROJECT_ROOT
from retail_agent.golden.promotion import PromotionGate, mine_candidates
from retail_agent.golden.retriever import GoldenRetriever, format_for_prompt
from retail_agent.golden.store import Trio, TrioStore
from retail_agent.observability.trace_store import TraceStore, TurnState
from retail_agent.safety.policy import PolicyStore
from retail_agent.safety.sql_guard import SqlGuard

SEED = TrioStore(PROJECT_ROOT / "data" / "golden")


@pytest.fixture
def guard(settings, warehouse):
    return SqlGuard(PolicyStore(settings.config_dir).policy(), warehouse.physical_table)


@pytest.mark.parametrize("trio", SEED.active(), ids=lambda t: t.id)
def test_every_seed_trio_passes_guard_and_runs_offline(trio, guard, warehouse):
    ceo = PolicyStore(PROJECT_ROOT / "config").entitlement("ceo")
    checked = guard.check(trio.sql, ceo)
    df = warehouse.execute(checked.sql, 50).df
    assert not df.empty, trio.id


@pytest.mark.parametrize(
    "question,expected",
    [
        ("show me revenue per month for the past year", "monthly_revenue_trend"),
        ("who spends the most money with us?", "top_customers_by_spend"),
        ("Why are customers in Ohio underspending compared to Florida?", "state_spend_comparison"),
        ("compare Nike vs Columbia and explain the difference", "compare_two_brands"),
        ("what caused the spike in churn in October", "churn_rate_monthly"),
        ("prepare a Q3 quarterly report with action items", "quarterly_business_report"),
        ("which marketing channel gives the best customers", "traffic_source_value"),
        ("year to date revenue by product", "revenue_ytd_by_product"),
    ],
)
def test_retrieval_finds_relevant_analyst_logic(question, expected):
    hits = GoldenRetriever(SEED).search(question, k=3)
    assert hits and expected in [h.trio.id for h in hits]
    assert hits[0].trio.id == expected


def test_unrelated_questions_get_no_examples():
    assert GoldenRetriever(SEED).search("what's the weather in Paris tomorrow", k=3) == []


def test_prompt_rendering_contains_interpretation_and_sql():
    hits = GoldenRetriever(SEED).search("monthly revenue trend", k=1)
    text = format_for_prompt(hits)
    assert "Analyst interpretation" in text and "```sql" in text and "excluding Cancelled" in text


def test_semantic_channel_is_fused_and_failures_degrade_to_bm25(tmp_path):
    def fake_embed(texts, task):  # crude bag-of-letters embedding, deterministic
        return [[t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"] for t in texts]

    r = GoldenRetriever(SEED, embed=fake_embed, cache_path=tmp_path / "emb.json")
    hits = r.search("monthly revenue trend", k=2)
    assert hits[0].semantic is not None
    assert (tmp_path / "emb.json").exists()

    def broken(texts, task):
        raise ConnectionError("embedding API down")

    r2 = GoldenRetriever(SEED, embed=broken)
    assert r2.search("monthly revenue trend", k=1)[0].trio.id == "monthly_revenue_trend"
    assert r2.embedding_errors >= 1


def test_promotion_gate_and_mining(tmp_path, guard, warehouse):
    store = TrioStore(tmp_path / "golden")
    for t in SEED.active()[:3]:
        store.write(t)
    good = Trio(id="cand_good", question="How many orders were returned per month this year?",
                sql="SELECT FORMAT_DATE('%Y-%m', DATE(created_at)) AS m, COUNT(*) AS n FROM orders "
                    "WHERE status = 'Returned' GROUP BY m ORDER BY m", status="candidate")
    unsafe = Trio(id="cand_pii", question="customer emails", sql="SELECT email FROM users WHERE id > 0", status="candidate")
    broken = Trio(id="cand_broken", question="revenue by color", sql="SELECT color, SUM(sale_price) FROM order_items GROUP BY color", status="candidate")
    dup = SEED.active()[0].model_copy(update={"id": "cand_dup", "status": "candidate"})
    for c in (good, unsafe, broken, dup):
        store.write(c, candidate=True)

    results = {r.trio_id: r for r in PromotionGate(store, guard, warehouse).promote_candidates()}
    assert results["cand_good"].passed
    assert not results["cand_pii"].passed and results["cand_pii"].checks["safety"] is False
    assert not results["cand_broken"].passed
    assert not results["cand_dup"].passed and results["cand_dup"].checks["not_duplicate"] is False
    assert "cand_good" in {t.id for t in store.active()}
    assert {t.id for t in store.candidates()} == {"cand_pii", "cand_broken", "cand_dup"}

    # system-level learning: a thumbs-up turn becomes a candidate trio
    traces = TraceStore(tmp_path / "tr.sqlite")
    turn = TurnState(trace_id="trace-xyz-123", session_id="s", user_id="ceo", question="Average order value by month?")
    turn.sql_attempts = 1
    turn.span("tool", "run_analysis_sql", status="ok", sql="SELECT 1 AS x FROM orders")
    traces.save_turn(turn)
    traces.add_feedback("trace-xyz-123", "ceo", 1)
    mined = mine_candidates(traces, store)
    assert [m.id for m in mined] == ["mined_trace-xyz-"] and mined[0].source == "mined"
