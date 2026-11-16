"""ADK plugins: cross-cutting behaviour applied to every turn, model call and tool call.

ObservabilityPlugin - trace per turn: every model call (message delta in, parts out, tokens, latency,
                      model version), every tool call (args, status, latency), errors; cost meter.
GuardrailsPlugin    - input guard (early exit, no model call wasted), budgets (cost, model calls,
                      tool calls), PII redaction of model output and tool results, and graceful
                      handling of model/tool failures (the conversation never crashes).
"""

from __future__ import annotations

import json
import time
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from retail_agent.agent.services import Services
from retail_agent.observability.trace_store import TurnState
from retail_agent.safety.pii import ScanReport, redact_text


def _text_of(content: types.Content | None) -> str:
    if not content or not content.parts:
        return ""
    return " ".join(p.text for p in content.parts if p.text)


def _summarize_parts(content: types.Content | None, limit: int = 1200) -> list[dict[str, Any]]:
    out = []
    for p in (content.parts if content and content.parts else []):
        if p.text:
            out.append({"text": p.text[:limit]})
        elif p.function_call:
            out.append({"function_call": p.function_call.name, "args": json.loads(json.dumps(p.function_call.args or {}, default=str))})
        elif p.function_response:
            resp = p.function_response.response or {}
            out.append({"function_response": p.function_response.name,
                        "status": resp.get("status") if isinstance(resp, dict) else None,
                        "preview": json.dumps(resp, default=str)[:limit]})
    return out


def _model_reply(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


class ObservabilityPlugin(BasePlugin):
    def __init__(self, services: Services, provider_label: str):
        super().__init__(name="observability")
        self.s = services
        self.provider_label = provider_label
        self._t_model: dict[str, float] = {}
        self._t_tool: dict[str, float] = {}
        self._seen_contents: dict[str, int] = {}

    def _turn(self, invocation_id: str) -> TurnState | None:
        return self.s.turns.get(invocation_id)

    async def before_run_callback(self, *, invocation_context) -> types.Content | None:
        ic = invocation_context
        self.s.turns.start(TurnState(
            trace_id=ic.invocation_id, session_id=ic.session.id, user_id=ic.user_id,
            question=_text_of(ic.user_content)[:2000], provider=self.provider_label,
        ))
        return None

    async def before_model_callback(self, *, callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse | None:
        inv = callback_context.invocation_id
        turn = self._turn(inv)
        self._t_model[inv] = time.perf_counter()
        if turn:
            seen = self._seen_contents.get(inv, max(len(llm_request.contents) - 1, 0))
            delta = llm_request.contents[seen:]
            self._seen_contents[inv] = len(llm_request.contents)
            turn.span("model", "request", model=llm_request.model,
                      messages_in_context=len(llm_request.contents),
                      delta=[{"role": c.role, "parts": _summarize_parts(c)} for c in delta])
        return None

    async def after_model_callback(self, *, callback_context: CallbackContext, llm_response: LlmResponse) -> LlmResponse | None:
        inv = callback_context.invocation_id
        turn = self._turn(inv)
        if not turn or llm_response.partial:
            return None
        dur = (time.perf_counter() - self._t_model.pop(inv, time.perf_counter())) * 1000
        turn.model_calls += 1
        u = llm_response.usage_metadata
        tin = int(getattr(u, "prompt_token_count", 0) or 0)
        tout = int(getattr(u, "candidates_token_count", 0) or 0) + int(getattr(u, "thoughts_token_count", 0) or 0)
        turn.input_tokens += tin
        turn.output_tokens += tout
        cfg = self.s.settings
        turn.llm_cost_usd += tin / 1e6 * cfg.price_in_per_mtok + tout / 1e6 * cfg.price_out_per_mtok
        version = llm_response.model_version or ""
        primary = cfg.model_primary if cfg.llm_provider == "gemini" else ""
        if version and ("fallback" in version or (primary and not version.startswith(primary))):
            turn.fallback_used = True
        turn.span("model", "response", duration_ms=dur, model_version=version, tokens_in=tin, tokens_out=tout,
                  finish_reason=str(llm_response.finish_reason or ""), parts=_summarize_parts(llm_response.content),
                  error=llm_response.error_message or "")
        return None

    async def on_model_error_callback(self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception) -> LlmResponse | None:
        turn = self._turn(callback_context.invocation_id)
        if turn:
            turn.span("error", "model_error", status="error", error_type=type(error).__name__, error=str(error)[:500])
            turn.error = f"model: {type(error).__name__}: {str(error)[:200]}"
        return None

    async def before_tool_callback(self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext) -> dict | None:
        self._t_tool[f"{tool_context.invocation_id}:{tool_context.function_call_id}"] = time.perf_counter()
        return None

    async def after_tool_callback(self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, result: dict) -> dict | None:
        turn = self._turn(tool_context.invocation_id)
        key = f"{tool_context.invocation_id}:{tool_context.function_call_id}"
        dur = (time.perf_counter() - self._t_tool.pop(key, time.perf_counter())) * 1000
        if turn:
            turn.tool_calls += 1
            status = result.get("status", "ok") if isinstance(result, dict) else "ok"
            if tool.name != "run_analysis_sql":  # run_analysis_sql records its own detailed span
                turn.span("tool", tool.name, status=str(status), duration_ms=dur,
                          args=json.loads(json.dumps(tool_args, default=str)),
                          result_preview=json.dumps(result, default=str)[:800])
            else:
                for s in reversed(turn.spans):
                    if s.name == "run_analysis_sql":
                        s.attributes["tool_total_ms"] = round(dur)
                        break
        return None

    async def on_tool_error_callback(self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, error: Exception) -> dict | None:
        turn = self._turn(tool_context.invocation_id)
        if turn:
            turn.span("error", f"tool_error:{tool.name}", status="error", error_type=type(error).__name__, error=str(error)[:500])
        return None

    async def on_event_callback(self, *, invocation_context, event) -> Any:
        turn = self._turn(invocation_context.invocation_id)
        if turn and event.content and event.author != "user":
            text = _text_of(event.content)
            if text and not event.partial:
                turn.answer = text
        return None

    async def after_run_callback(self, *, invocation_context) -> None:
        inv = invocation_context.invocation_id
        self._seen_contents.pop(inv, None)
        turn = self.s.turns.pop(inv)
        if turn:
            if turn.status == "ok" and turn.error and not turn.answer:
                turn.status = "error"
            self.s.traces.save_turn(turn)
            self.s.turns.start(turn)  # keep it readable for the caller until it collects the result
        return None


class GuardrailsPlugin(BasePlugin):
    def __init__(self, services: Services):
        super().__init__(name="guardrails")
        self.s = services

    def _turn(self, invocation_id: str) -> TurnState | None:
        return self.s.turns.get(invocation_id)

    # ---------------------------------------------------------------- input guard
    async def before_run_callback(self, *, invocation_context) -> types.Content | None:
        ic = invocation_context
        text = _text_of(ic.user_content)
        turn = self._turn(ic.invocation_id)
        import asyncio

        verdict = await asyncio.to_thread(self.s.input_guard.check, text)
        if turn:
            turn.span("guard", "input_guard", status="ok" if verdict.allowed else "blocked",
                      category=verdict.category, reason=verdict.reason, source=verdict.source)
        if not verdict.allowed:
            if turn:
                turn.status, turn.refusal_category, turn.answer = "refused", verdict.category, verdict.user_message
                turn.guard_blocks += 1
            return types.Content(role="model", parts=[types.Part(text=verdict.user_message)])
        # implicit preference learning from the user's own words (cheap, deterministic)
        learned = self.s.prefs.learn_from_message(ic.user_id, text)
        if turn and learned:
            turn.span("event", "implicit_preference_signal", signals=learned)
        return None

    # ---------------------------------------------------------------- budgets
    async def before_model_callback(self, *, callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse | None:
        turn = self._turn(callback_context.invocation_id)
        cfg = self.s.settings
        if not turn:
            return None
        if turn.cost_usd >= cfg.budget_usd_per_question or turn.model_calls >= cfg.max_llm_calls_per_turn:
            turn.budget_exceeded = True
            turn.status = "budget_exceeded"
            turn.span("guard", "budget_exceeded", status="blocked", cost_usd=round(turn.cost_usd, 4), model_calls=turn.model_calls)
            return _model_reply(
                "I stopped here to stay within the cost limit for a single question. "
                "Here is what I have so far - please narrow the question (time window, segment) and I'll continue."
                + (f"\n\nPartial findings: {turn.answer[:800]}" if turn.answer else "")
            )
        return None

    async def before_tool_callback(self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext) -> dict | None:
        turn = self._turn(tool_context.invocation_id)
        if turn and turn.tool_calls >= self.s.settings.max_tool_calls_per_turn:
            turn.span("guard", "tool_budget_exceeded", status="blocked", tool=tool.name)
            return {"status": "budget_exceeded", "message": "Tool-call budget for this question reached. "
                    "Answer with the information you already have."}
        return None

    # ---------------------------------------------------------------- output PII redaction
    async def after_model_callback(self, *, callback_context: CallbackContext, llm_response: LlmResponse) -> LlmResponse | None:
        content = llm_response.content
        if not content or not content.parts:
            return None
        report = ScanReport()
        changed = False
        for p in content.parts:
            if p.text:
                clean, report = redact_text(p.text, report)
                if clean != p.text:
                    p.text = clean
                    changed = True
        if changed:
            turn = self._turn(callback_context.invocation_id)
            if turn:
                turn.pii_redactions += report.total
                turn.span("guard", "output_pii_redacted", status="redacted", findings=report.findings)
            return llm_response
        return None

    async def after_tool_callback(self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, result: dict) -> dict | None:
        """Defence in depth: no tool result reaches the model with PII-looking strings."""
        report = ScanReport()

        def walk(v):
            if isinstance(v, str):
                return redact_text(v, report)[0]
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            return v

        if tool.name in ("describe_schema", "search_golden_knowledge"):
            return None  # static, trusted content
        cleaned = walk(result)
        if report.total:
            turn = self._turn(tool_context.invocation_id)
            if turn:
                turn.pii_redactions += report.total
                turn.span("guard", "tool_output_pii_redacted", status="redacted", tool=tool.name, findings=report.findings)
            return cleaned
        return None

    # ---------------------------------------------------------------- graceful failures
    async def on_model_error_callback(self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception) -> LlmResponse | None:
        turn = self._turn(callback_context.invocation_id)
        if turn:
            turn.status = "degraded"
            turn.degraded = True
        question = turn.question if turn else ""
        fallback = self._degraded_answer(question, callback_context.user_id) if question else None
        msg = ("The AI service is temporarily unavailable (all model endpoints failed), so I can't write a full "
               "analysis right now.")
        if fallback:
            msg += "\n\nIn the meantime, here is the closest standard analysis from our analyst library:\n\n" + fallback
        else:
            msg += " Please try again in a minute - nothing was lost."
        return _model_reply(msg)

    async def on_tool_error_callback(self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, error: Exception) -> dict | None:
        return {"status": "error", "error_type": "tool_failure",
                "message": f"The {tool.name} tool failed unexpectedly ({type(error).__name__}). Do not retry it; "
                "continue with what you have or tell the user."}

    def _degraded_answer(self, question: str, user_id: str) -> str | None:
        """LLM down: answer from the best-matching golden trio, still through the full guard."""
        from retail_agent.agent.analysis import to_records
        from retail_agent.safety.pii import scan_dataframe

        hits = self.s.retriever.search(question, k=1)
        if not hits or hits[0].lexical < 3.0:
            return None
        trio = hits[0].trio
        try:
            ent = self.s.policies.entitlement(user_id)
            checked = self.s.guard().check(trio.sql, ent)
            df = self.s.warehouse.execute(checked.sql, 20).df
            df, _ = scan_dataframe(df)
            df = self.s.pseudonymizer.apply(df, checked.pseudonymize_columns)
        except Exception:  # noqa: BLE001 - degraded mode is best effort
            return None
        rows = to_records(df, 15)
        if not rows:
            return None
        cols = list(rows[0])
        table = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n" + "\n".join(
            "| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows)
        return f"**{trio.question}**\n\n{table}"
