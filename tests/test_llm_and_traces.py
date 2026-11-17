from __future__ import annotations

from google.adk.models import FallbackModel, LlmRequest
from google.genai import types

from retail_agent.llm.fake import ChaosLlm, ScriptedLlm, text_response
from retail_agent.observability.metrics import compute_metrics
from retail_agent.observability.trace_store import TraceStore, TurnState


def _req(text: str) -> LlmRequest:
    return LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text=text)])])


async def _collect(model, req):
    return [r async for r in model.generate_content_async(req)]


async def test_fallback_model_switches_on_provider_outage():
    primary = ChaosLlm(inner=ScriptedLlm(steps=[text_response("primary")]), failure_rate=1.0)
    backup = ScriptedLlm(steps=[text_response("from backup")])
    model = FallbackModel(models=[primary, backup])
    out = await _collect(model, _req("hi"))
    assert out[0].content.parts[0].text == "from backup"
    assert primary.injected == 1
    assert out[0].usage_metadata.prompt_token_count >= 0


def test_trace_store_roundtrip_and_metrics(tmp_path):
    store = TraceStore(tmp_path / "t.sqlite", tmp_path / "t.jsonl")
    t = TurnState(trace_id="abc123", session_id="s1", user_id="ceo", question="q")
    t.sql_attempts, t.sql_failures, t.sql_self_corrected = 2, 1, 1
    t.span("tool", "run_analysis_sql", status="error", sql="SELEC 1")
    t.span("tool", "run_analysis_sql", sql="SELECT 1")
    store.save_turn(t)
    store.add_feedback("abc123", "ceo", 1)
    assert store.turn("abc")["question"] == "q"
    spans = store.spans("abc123")
    assert [s["status"] for s in spans] == ["error", "ok"] and spans[0]["attributes"]["sql"] == "SELEC 1"
    m = compute_metrics(store)
    assert m["turns"] == 1 and m["sql_failure_rate"] == 0.5 and m["feedback_positive_rate"] == 1.0
    assert (tmp_path / "t.jsonl").read_text().count("abc123") >= 1


async def test_slow_model_times_out_into_fallback_and_breaker_skips_it():
    import asyncio

    from retail_agent.llm.fake import _BREAKERS, BreakerLlm

    class SlowLlm(ScriptedLlm):
        async def generate_content_async(self, llm_request, stream=False):
            await asyncio.sleep(5)
            yield text_response("too late")

    _BREAKERS.clear()
    slow = BreakerLlm(inner=SlowLlm(model="slow-model"), model="slow-model", timeout_s=0.2)
    backup = ScriptedLlm(steps=[text_response("fast backup")] * 3, model="backup")
    model = FallbackModel(models=[slow, backup])
    import time

    t0 = time.perf_counter()
    for _ in range(3):
        out = await _collect(model, _req("hi"))
        assert out[0].content.parts[0].text == "fast backup"
    # two timeouts open the breaker; the third call skips the slow model instantly
    assert time.perf_counter() - t0 < 1.0
    assert _BREAKERS["slow-model"].state == "open"


class _StreamingLlm(ScriptedLlm):
    """First chunk fast, then a long generation (like a model writing a report)."""

    gen_s: float = 0.4

    async def generate_content_async(self, llm_request, stream=False):
        import asyncio

        await asyncio.sleep(0.02)
        part = text_response("Q1 revenue grew")
        part.partial = True
        yield part
        await asyncio.sleep(self.gen_s)
        yield text_response("Q1 revenue grew 11.6% QoQ. Full report follows.")


async def test_long_generation_that_has_started_is_not_thrown_away():
    """Regression (seen live): one fixed 20 s deadline cut the primary off mid-report; the weaker
    fallback then regenerated the answer (~45 s extra). Time-to-first-token decides 'hung'."""
    from retail_agent.llm.fake import _BREAKERS, BreakerLlm

    _BREAKERS.clear()
    primary = BreakerLlm(inner=_StreamingLlm(model="writer"), model="writer", timeout_s=0.2, total_timeout_s=2.0)
    backup = ScriptedLlm(steps=[text_response("weaker backup")], model="backup")
    out = await _collect(FallbackModel(models=[primary, backup]), _req("Create the Q1 report"))
    assert [o.content.parts[0].text for o in out] == ["Q1 revenue grew 11.6% QoQ. Full report follows."]
    assert all(not o.partial for o in out)  # caller asked for non-streaming: only the aggregated result
    assert _BREAKERS["writer"].state == "closed"


async def test_generation_beyond_the_total_cap_falls_back_without_tripping():
    from retail_agent.llm.fake import _BREAKERS, BreakerLlm

    _BREAKERS.clear()
    primary = BreakerLlm(inner=_StreamingLlm(model="rambler", gen_s=1.0), model="rambler", timeout_s=0.2, total_timeout_s=0.3)
    backup = ScriptedLlm(steps=[text_response("backup answer")], model="backup")
    out = await _collect(FallbackModel(models=[primary, backup]), _req("Create the Q1 report"))
    assert out[0].content.parts[0].text == "backup answer"
    assert _BREAKERS["rambler"].state == "closed"  # counted as a failure, not tripped on the first one
