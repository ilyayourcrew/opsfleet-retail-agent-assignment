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
