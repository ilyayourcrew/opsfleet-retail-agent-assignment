"""ChatEngine: the application service used by the CLI, the eval harness and tests.

Wires ADK (App + Runner + session service + plugins + tools) and adds the pieces that must not be
delegated to the model: the turn-level error boundary/timeout, and the deterministic deletion
confirmation path.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.models import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from retail_agent.agent.instructions import build_instruction_provider
from retail_agent.agent.plugins import GuardrailsPlugin, ObservabilityPlugin
from retail_agent.agent.services import Services
from retail_agent.reports.store import DeletionError, PendingDeletion
from retail_agent.tools.registry import build_tools

APP_NAME = "retail_analyst"


@dataclass
class TurnResult:
    answer: str
    trace_id: str
    status: str
    latency_ms: float
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    sql_attempts: int = 0
    pending_deletion: PendingDeletion | None = None
    charts: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)


class ChatEngine:
    def __init__(self, services: Services, model: BaseLlm | str, persistent_sessions: bool = True):
        self.s = services
        cfg = services.settings
        provider_label = f"{cfg.llm_provider}:{getattr(model, 'model', model)}"
        self.agent = LlmAgent(
            name="retail_analyst",
            description="Data analysis assistant for retail executives.",
            model=model,
            instruction=build_instruction_provider(services),
            tools=build_tools(services),
            generate_content_config=types.GenerateContentConfig(
                temperature=0.2,
                http_options=types.HttpOptions(timeout=int(cfg.llm_timeout_s * 1000)),
            ),
        )
        self.app = App(
            name=APP_NAME,
            root_agent=self.agent,
            plugins=[ObservabilityPlugin(services, provider_label), GuardrailsPlugin(services)],
        )
        if persistent_sessions:
            from google.adk.sessions.sqlite_session_service import SqliteSessionService

            self.sessions = SqliteSessionService(str(cfg.session_db_path))
        else:
            self.sessions = InMemorySessionService()
        self.runner = Runner(app=self.app, session_service=self.sessions)

    async def new_session(self, user_id: str) -> str:
        sess = await self.sessions.create_session(app_name=APP_NAME, user_id=user_id)
        return sess.id

    async def ask(self, user_id: str, session_id: str, text: str,
                  on_status: Callable[[str], None] | None = None) -> TurnResult:
        t0 = time.perf_counter()
        cfg = self.s.settings
        answer_parts: list[str] = []
        tools_used: list[str] = []
        trace_id = ""
        status = "ok"
        msg = types.Content(role="user", parts=[types.Part(text=text)])

        async def consume():
            nonlocal trace_id
            async for event in self.runner.run_async(
                user_id=user_id, session_id=session_id, new_message=msg,
                run_config=RunConfig(max_llm_calls=cfg.max_llm_calls_per_turn + 2),
            ):
                trace_id = event.invocation_id or trace_id
                for call in event.get_function_calls() or []:
                    tools_used.append(call.name)
                    if on_status:
                        on_status(_STATUS.get(call.name, f"Running {call.name}..."))
                if event.is_final_response() and event.content and event.content.parts:
                    txt = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                    if txt:
                        answer_parts.append(txt)

        try:
            await asyncio.wait_for(consume(), timeout=cfg.turn_timeout_s)
        except TimeoutError:
            status = "timeout"
            answer_parts.append("This is taking longer than expected, so I stopped to avoid leaving you waiting. "
                                "Try narrowing the question (shorter period or fewer segments).")
        except Exception as e:  # noqa: BLE001 - the UI must never crash
            status = "error"
            ref = trace_id[:8] if trace_id else uuid.uuid4().hex[:8]
            answer_parts.append(f"Sorry - something went wrong on my side ({type(e).__name__}). "
                                f"Your conversation is intact; please try again. Reference: {ref}")
            if not trace_id:
                trace_id = ref

        turn = self.s.turns.pop(trace_id) if trace_id else None
        if turn is not None:
            if status != "ok":
                turn.status = status
                turn.error = turn.error or status
                turn.answer = turn.answer or answer_parts[-1]
                self.s.traces.save_turn(turn)
            status = turn.status if status == "ok" else status
        pending = self.s.reports.pending(user_id, session_id) if "request_report_deletion" in tools_used else None
        chart_paths = [sp.attributes["path"] for sp in (turn.spans if turn else []) if sp.name == "chart_created"]
        return TurnResult(
            answer="\n\n".join(answer_parts).strip() or "(no answer)",
            trace_id=trace_id,
            status=status,
            latency_ms=(time.perf_counter() - t0) * 1000,
            cost_usd=turn.cost_usd if turn else 0.0,
            tokens_in=turn.input_tokens if turn else 0,
            tokens_out=turn.output_tokens if turn else 0,
            sql_attempts=turn.sql_attempts if turn else 0,
            pending_deletion=pending,
            charts=chart_paths,
            tools_used=tools_used,
        )

    # ------------------------------------------------------------------ deterministic confirmation
    def confirm_deletion(self, user_id: str, session_id: str, token: str, typed_phrase: str) -> tuple[bool, str]:
        """Called by the interface with the user's typed phrase. The LLM is not involved."""
        try:
            ids = self.s.reports.confirm(token, user_id, session_id, typed_phrase)
        except DeletionError as e:
            return False, str(e)
        return True, f"Permanently deleted {len(ids)} report(s): " + ", ".join(f"#{i}" for i in ids) + "."


_STATUS = {
    "run_analysis_sql": "Querying the data warehouse...",
    "describe_schema": "Looking at the data model...",
    "search_golden_knowledge": "Checking how analysts approached similar questions...",
    "save_report": "Saving the report...",
    "list_reports": "Looking up your reports...",
    "get_report": "Opening the report...",
    "request_report_deletion": "Preparing the deletion request...",
    "remember_preference": "Noting your preference...",
    "create_chart": "Drawing the chart...",
}
