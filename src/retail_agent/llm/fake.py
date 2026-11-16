"""Offline models implementing ADK's BaseLlm.

* `ScriptedLlm`  - replays an exact list of responses; used by tests to drive precise scenarios
                   (bad SQL -> self-correction, deletion flow, ...) through the real ADK Runner.
* `RuleBasedLlm` - a deterministic keyword router that uses the golden trios injected into the
                   system instruction. It makes the whole product usable with no network at all
                   (demo/CI) and doubles as documentation of the expected tool protocol.
Both report approximate token usage so the cost meter and budgets are exercised offline.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types


def _usage(req: LlmRequest, out_text: str) -> types.GenerateContentResponseUsageMetadata:
    sys_text = str(getattr(req.config, "system_instruction", "") or "")
    in_chars = len(sys_text) + sum(len(str(c)) for c in req.contents)
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=in_chars // 4,
        candidates_token_count=max(1, len(out_text) // 4),
        total_token_count=in_chars // 4 + max(1, len(out_text) // 4),
    )


def text_response(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def call_response(name: str, **args: Any) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])
    )


def last_user_text(req: LlmRequest) -> str:
    for c in reversed(req.contents):
        if c.role == "user":
            texts = [p.text for p in (c.parts or []) if p.text]
            if texts:
                return " ".join(texts)
    return ""


def last_function_responses(req: LlmRequest) -> list[types.FunctionResponse]:
    if not req.contents:
        return []
    last = req.contents[-1]
    return [p.function_response for p in (last.parts or []) if p.function_response]


def tool_calls_this_turn(req: LlmRequest) -> list[str]:
    """Names of tools already called since the last real user text message."""
    names: list[str] = []
    for c in reversed(req.contents):
        if c.role == "user" and any(p.text for p in (c.parts or [])):
            break
        for p in c.parts or []:
            if p.function_call:
                names.append(p.function_call.name)
    return names


class ScriptedLlm(BaseLlm):
    """Returns the next scripted step on every call. A step is an LlmResponse or a callable."""

    model: str = "scripted"
    steps: list[Any] = []
    calls: list[LlmRequest] = []

    def model_post_init(self, __context: Any) -> None:
        self.steps = list(self.steps)
        self.calls = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.calls.append(llm_request)
        if not self.steps:
            resp = text_response("(script exhausted)")
        else:
            step = self.steps.pop(0)
            resp = step(llm_request) if callable(step) else step
            if isinstance(resp, Exception):
                raise resp
        resp = resp.model_copy(deep=True)
        out_text = " ".join(p.text or "" for p in (resp.content.parts or [])) if resp.content else ""
        resp.usage_metadata = _usage(llm_request, out_text)
        resp.model_version = self.model
        yield resp


# --------------------------------------------------------------------------- rule based
_SQL_BLOCK = re.compile(r"```sql\s*\n(.*?)```", re.S | re.I)
_PREF_FORMAT = re.compile(r"format:\s*(table|bullets|prose)", re.I)


def _rows_to_markdown(rows: list[dict[str, Any]], limit: int = 15) -> str:
    if not rows:
        return "_no rows_"
    cols = list(rows[0])
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols)
    body = "\n".join("| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |" for r in rows[:limit])
    return head + "\n" + body


def _rows_to_bullets(rows: list[dict[str, Any]], limit: int = 15) -> str:
    out = []
    for r in rows[:limit]:
        vals = list(r.items())
        first = _fmt(vals[0][1]) if vals else ""
        rest = ", ".join(f"{k}: {_fmt(v)}" for k, v in vals[1:])
        out.append(f"- **{first}** - {rest}")
    return "\n".join(out)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _numeric_cols(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    return [k for k, v in rows[0].items() if isinstance(v, int | float) and not isinstance(v, bool)]


class RuleBasedLlm(BaseLlm):
    """Deterministic offline 'analyst'. Not smart - but it follows the same tool protocol."""

    model: str = "rule-based-offline"

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        resp = self._decide(llm_request)
        out_text = " ".join(p.text or "" for p in (resp.content.parts or [])) if resp.content else ""
        resp.usage_metadata = _usage(llm_request, out_text)
        resp.model_version = self.model
        yield resp

    # ------------------------------------------------------------------ routing
    def _decide(self, req: LlmRequest) -> LlmResponse:
        question = last_user_text(req)
        q = question.lower()
        system = str(getattr(req.config, "system_instruction", "") or "")
        fmt_m = _PREF_FORMAT.search(system)
        fmt = fmt_m.group(1).lower() if fmt_m else "table"
        responses = last_function_responses(req)
        if responses:
            return self._after_tool(responses[0], question, fmt, tool_calls_this_turn(req))

        if "report" in q and re.search(r"\b(delete|remove|erase|purge)\b", q):
            if re.search(r"this (conversation|chat|session)|we (made|created)|so far", q):
                return call_response("request_report_deletion", scope="this_conversation")
            m = re.search(r"(?:mentioning|about|containing|with|on)\s+['\"]?(.+?)['\"]?\s*$", question, re.I)
            if m:
                return call_response("request_report_deletion", scope="matching", text_query=m.group(1).strip(" ?.!"))
            return call_response("request_report_deletion", scope="all")
        if re.search(r"\b(list|show|which|what)\b.*\breports\b", q) and "create" not in q:
            return call_response("list_reports")
        pref = self._preference(q)
        if pref:
            return call_response("remember_preference", key=pref[0], value=pref[1])
        if re.search(r"\b(what data|which tables|tables|schema|columns|structure of the database|what can (i|we) (ask|do))\b", q):
            return call_response("describe_schema")
        sql_m = _SQL_BLOCK.search(system)
        if sql_m:
            return call_response("run_analysis_sql", sql=sql_m.group(1).strip(), purpose=question[:200])
        return text_response(
            "I couldn't map this question to a known analysis in offline mode. Try one of: monthly revenue, "
            "top customers, revenue by category, compare brands, spend by state, churn last month."
        )

    @staticmethod
    def _preference(q: str) -> tuple[str, str] | None:
        if not re.search(r"\b(i (prefer|like|want)|please (always|use)|from now on|going forward)\b", q):
            return None
        if "table" in q:
            return ("format", "table")
        if "bullet" in q:
            return ("format", "bullets")
        if "chart" in q or "graph" in q or "visual" in q:
            return ("visuals", "charts")
        if re.search(r"short|brief|concise", q):
            return ("depth", "brief")
        if re.search(r"detail|deep|thorough", q):
            return ("depth", "deep")
        return None

    def _after_tool(self, fr: types.FunctionResponse, question: str, fmt: str, done: list[str]) -> LlmResponse:
        name = fr.name
        r: dict[str, Any] = dict(fr.response or {})
        if "result" in r and isinstance(r["result"], dict):
            r = r["result"]
        status = r.get("status", "ok")
        if name == "run_analysis_sql":
            if status != "ok":
                msg = r.get("message") or r.get("error") or "the query did not return data"
                return text_response(f"I could not complete that analysis: {msg}")
            rows = r.get("rows", [])
            if re.search(r"\b(create|generate|write|make|prepare)\b.*\breport\b", question.lower()) and "save_report" not in done:
                content = self._report(question, rows)
                title = re.sub(r"(?i)^(please\s+)?(create|generate|write|make|prepare)\s+(me\s+)?(a\s+)?", "", question).strip()[:80] or "Analysis report"
                return call_response("save_report", title=title[0].upper() + title[1:], content_markdown=content)
            table = _rows_to_bullets(rows) if fmt == "bullets" else _rows_to_markdown(rows)
            head = self._headline(rows)
            more = f"\n\n_Showing {min(len(rows), 15)} of {r.get('row_count', len(rows))} rows._" if len(rows) > 15 else ""
            notes = r.get("notes") or []
            note_txt = ("\n\n_Note: " + "; ".join(notes) + "._") if notes else ""
            return text_response(f"{head}\n\n{table}{more}{note_txt}")
        if name == "save_report":
            if status != "ok":
                return text_response(f"I couldn't save the report: {r.get('message')}")
            return text_response(f"Saved report **#{r.get('report_id')} - {r.get('title')}**.\n\n{r.get('content_preview', '')}")
        if name == "request_report_deletion":
            if status == "nothing_to_delete":
                return text_response("I didn't find any of your reports matching that request - nothing to delete.")
            items = "\n".join(f"- #{x['id']} {x['title']} ({x['created_at']})" for x in r.get("reports", []))
            return text_response(
                f"I found {len(r.get('reports', []))} report(s) that would be **permanently deleted**:\n{items}\n\n"
                "Please confirm in the prompt below - nothing is deleted until you do."
            )
        if name == "list_reports":
            reps = r.get("reports", [])
            if not reps:
                return text_response("You have no saved reports yet.")
            return text_response("Your saved reports:\n" + "\n".join(f"- #{x['id']} {x['title']} ({x['created_at']})" for x in reps))
        if name == "remember_preference":
            return text_response(f"Got it - I'll remember that ({r.get('key')} = {r.get('value')}).")
        if name == "describe_schema":
            tables = r.get("tables", {})
            lines = [f"- **{t}** - {info.get('description', '')} ({len(info.get('columns', []))} columns)" for t, info in tables.items()]
            return text_response("Here is the data I can analyse:\n" + "\n".join(lines) + "\n\nPersonal details (names, emails, addresses, locations) are never available; demographics only in aggregates.")
        return text_response(json.dumps(r, default=str)[:1500])

    @staticmethod
    def _headline(rows: list[dict[str, Any]]) -> str:
        nums = _numeric_cols(rows)
        if not rows or not nums:
            return "Here is what the data shows:"
        key = list(rows[0])[0]
        metric = nums[-1]
        top = max(rows, key=lambda x: x.get(metric) or 0)
        return f"**Headline:** highest {metric} is {_fmt(top.get(metric))} for {key} = {top.get(key)} (offline analysis)."

    def _report(self, question: str, rows: list[dict[str, Any]]) -> str:
        nums = _numeric_cols(rows)
        metric = nums[-1] if nums else None
        key = list(rows[0])[0] if rows else "item"
        insights = []
        if metric and rows:
            srt = sorted(rows, key=lambda x: x.get(metric) or 0, reverse=True)
            insights.append(f"Top {key}: {srt[0].get(key)} with {metric} {_fmt(srt[0].get(metric))}.")
            if len(srt) > 1:
                insights.append(f"Lowest {key}: {srt[-1].get(key)} with {metric} {_fmt(srt[-1].get(metric))}.")
            total = sum((x.get(metric) or 0) for x in rows)
            insights.append(f"Total {metric} across the period: {_fmt(total)}.")
        return (
            f"## Executive summary\n{question}\n\n## Key metrics\n{_rows_to_markdown(rows, 12)}\n\n"
            "## Insights\n" + "\n".join(f"- {i}" for i in insights) + "\n\n"
            "## Action items\n- Double down on the top performer (owner: category lead).\n"
            "- Investigate the weakest segment's drivers (owner: analytics).\n"
            "- Review progress in the next weekly sync (owner: exec sponsor).\n"
        )


class ChaosLlmError(RuntimeError):
    """Looks like a provider 503 to ADK's FallbackModel (it reads `status_code`)."""

    status_code = 503


class ChaosLlm(BaseLlm):
    """Wraps a model and fails a fraction of calls with a retriable 503 (resilience demos/tests)."""

    model: str = "chaos"
    inner: BaseLlm
    failure_rate: float = 1.0
    should_fail: Callable[[], bool] | None = None
    injected: int = 0

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        import random

        fail = self.should_fail() if self.should_fail else random.random() < self.failure_rate
        if fail:
            self.injected += 1
            raise ChaosLlmError("Injected fault: model 503 Service Unavailable")
        async for r in self.inner.generate_content_async(llm_request, stream):
            yield r
