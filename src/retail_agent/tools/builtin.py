"""Built-in tools. Each factory closes over the Services container and returns the function ADK
exposes to the model. Identity (user, session, turn) always comes from the ToolContext - never
from model-supplied arguments - so the model cannot act on behalf of another user."""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from retail_agent.agent.analysis import AnalysisService
from retail_agent.golden.retriever import format_for_prompt
from retail_agent.memory.preferences import ALLOWED, PreferenceError
from retail_agent.reports.store import DeletionError
from retail_agent.safety.pii import redact_text
from retail_agent.safety.policy import UnknownUserError
from retail_agent.tools.registry import register
from retail_agent.warehouse.schema import RELATIONSHIPS, TABLES


def _ids(tc: ToolContext) -> tuple[str, str, str]:
    return tc.user_id, tc.session.id, tc.invocation_id


@register("describe_schema", "analysis", "What data is available")
def make_describe_schema(services):
    def describe_schema(table: str = "") -> dict:
        """Describe the analytics database: tables, columns, meaning, relationships and privacy rules.

        Use it for questions about what data exists / what can be analysed, or when unsure about a
        column name. Args: table - optional single table name (orders, order_items, products, users).
        """
        policy = services.policies.policy()
        names = [table] if table in TABLES else list(policy.allowed_tables)
        out: dict[str, Any] = {"tables": {}}
        for n in names:
            t = TABLES[n]
            pii = policy.pii.get(n, frozenset())
            quasi = policy.quasi.get(n, frozenset())
            cols = []
            for c in t.columns:
                if c.name in pii:
                    continue
                tag = " [aggregate-only]" if c.name in quasi else ""
                cols.append(f"{c.name} ({c.type}){tag}: {c.description}")
            out["tables"][n] = {"description": t.description, "grain": t.grain, "columns": cols, "notes": list(t.notes),
                                "unavailable_personal_columns": sorted(pii)}
        out["relationships"] = list(RELATIONSHIPS)
        out["privacy"] = (f"Personal data is never available. Demographics only in aggregates over at least "
                          f"{policy.k_min} customers. Customer ids are pseudonymised.")
        return out

    return describe_schema


@register("search_golden_knowledge", "analysis", "Find analyst precedents")
def make_search_golden(services):
    def search_golden_knowledge(question: str) -> dict:
        """Search the golden knowledge base of past analyst work (question -> SQL -> report).

        The most relevant examples for the user's message are already in your instructions; call this
        only for a *different* sub-question during multi-step analysis. Args: question - natural language.
        """
        hits = services.retriever.search(question, k=services.settings.golden_top_k)
        return {"examples": format_for_prompt(hits) or "No relevant analyst examples found.",
                "ids": [h.trio.id for h in hits]}

    return search_golden_knowledge


@register("run_analysis_sql", "analysis", "Governed SQL execution")
def make_run_sql(services):
    analysis = AnalysisService(services)

    def run_analysis_sql(sql: str, purpose: str, tool_context: ToolContext) -> dict:
        """Run ONE read-only BigQuery Standard SQL query over orders, order_items, products, users.

        Write plain table names (e.g. `FROM order_items AS oi`). Aggregate in SQL; never select
        personal columns. Returns rows (or a structured error / empty-result diagnostics to fix the
        query). Args: sql - the query; purpose - one sentence on what this query answers.
        """
        user_id, session_id, trace_id = _ids(tool_context)
        try:
            ent = services.policies.entitlement(user_id)
        except UnknownUserError as e:
            return {"status": "error", "error_type": "no_access", "message": str(e)}
        return analysis.run(sql, purpose, ent, session_id, services.turns.get(trace_id))

    return run_analysis_sql


@register("save_report", "reports", "Save a report to the user's library")
def make_save_report(services):
    def save_report(title: str, content_markdown: str, tool_context: ToolContext) -> dict:
        """Save a finished report (markdown) to the user's private Saved Reports library.

        Use when the user asks to create/generate/save a report. Include insights and action items.
        Args: title - short title; content_markdown - the full report.
        """
        user_id, session_id, trace_id = _ids(tool_context)
        clean, rep = redact_text(content_markdown)
        title_clean, _ = redact_text(title)
        turn = services.turns.get(trace_id)
        if turn and rep.total:
            turn.pii_redactions += rep.total
        sqls = [s.attributes.get("sql") for s in (turn.spans if turn else []) if s.name == "run_analysis_sql" and s.status == "ok"]
        r = services.reports.save(user_id, session_id, title_clean, clean, sql_used=[s for s in sqls if s])
        return {"status": "ok", "report_id": r.id, "title": r.title, "content_preview": clean[:1500]}

    return save_report


@register("list_reports", "reports", "List the user's reports")
def make_list_reports(services):
    def list_reports(tool_context: ToolContext, text_query: str = "", this_conversation_only: bool = False) -> dict:
        """List the user's own saved reports (newest first).

        Args: text_query - optional words the report must mention; this_conversation_only - only
        reports created in the current conversation.
        """
        user_id, session_id, _ = _ids(tool_context)
        reps = services.reports.list(user_id, session_id=session_id if this_conversation_only else None,
                                     text=text_query or None)
        return {"status": "ok", "reports": [r.brief() for r in reps]}

    return list_reports


@register("get_report", "reports", "Read one report")
def make_get_report(services):
    def get_report(report_id: int, tool_context: ToolContext) -> dict:
        """Return the content of one of the user's saved reports. Args: report_id."""
        user_id, _, _ = _ids(tool_context)
        r = services.reports.get(user_id, int(report_id))
        if r is None:
            return {"status": "not_found", "message": f"You have no report #{report_id}."}
        return {"status": "ok", **r.brief(), "content": r.content}

    return get_report


@register("request_report_deletion", "reports", "Start the confirmation flow for deleting reports")
def make_request_deletion(services):
    def request_report_deletion(
        scope: str, tool_context: ToolContext, text_query: str = "", report_ids: list[int] | None = None
    ) -> dict:
        """Prepare deletion of the user's saved reports. Nothing is deleted by this tool.

        It resolves the exact reports and asks the user for explicit confirmation in the interface;
        deletion happens only after the user types the confirmation phrase. Never claim reports were
        deleted. Args: scope - "this_conversation" | "matching" (needs text_query, e.g. a client or
        brand name) | "ids" (needs report_ids) | "all".
        """
        user_id, session_id, trace_id = _ids(tool_context)
        if scope not in {"this_conversation", "matching", "ids", "all"}:
            return {"status": "error", "message": "scope must be this_conversation, matching, ids or all"}
        try:
            pending = services.reports.request_deletion(user_id, session_id, scope, text_query or None, report_ids)
        except DeletionError as e:
            return {"status": "error", "message": str(e)}
        turn = services.turns.get(trace_id)
        if pending is None:
            if turn:
                turn.span("event", "deletion_nothing_found", scope=scope, text_query=text_query)
            return {"status": "nothing_to_delete", "message": "No matching reports owned by this user."}
        if turn:
            turn.span("event", "deletion_requested", ids=[r.id for r in pending.reports], scope=scope)
        return {
            "status": "awaiting_user_confirmation",
            "description": pending.description,
            "reports": [r.brief() for r in pending.reports],
            "confirmation": f"The interface now asks the user to type '{pending.confirm_phrase}'. "
                            "Tell the user what will be deleted and that it is permanent.",
        }

    return request_report_deletion


@register("remember_preference", "memory", "Store a user preference")
def make_remember_preference(services):
    def remember_preference(key: str, value: str, tool_context: ToolContext) -> dict:
        """Remember how this user likes answers presented, when they state a preference.

        Args: key - one of format (table|bullets|prose), depth (brief|standard|deep),
        visuals (charts|text), note (free text, e.g. "always compare to last year"); value.
        """
        user_id, _, _ = _ids(tool_context)
        try:
            p = services.prefs.set_explicit(user_id, key, value)
        except PreferenceError as e:
            return {"status": "error", "message": str(e), "allowed": {k: sorted(v) if v else "free text" for k, v in ALLOWED.items()}}
        return {"status": "ok", "key": p.key, "value": p.value}

    return remember_preference


@register("create_chart", "visuals", "Render a chart (example extension)")
def make_create_chart(services):
    def create_chart(query_id: str, x_column: str, y_columns: list[str], chart_type: str, title: str,
                     tool_context: ToolContext) -> dict:
        """Render a PNG chart from a previous run_analysis_sql result.

        Args: query_id - from the run_analysis_sql result; x_column; y_columns - one or more numeric
        columns; chart_type - "bar" or "line"; title.
        """
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        _, session_id, trace_id = _ids(tool_context)
        df = services.session_results.get(session_id, {}).get(query_id)
        if df is None:
            return {"status": "error", "message": f"Unknown query_id {query_id}. Run the query first."}
        missing = [c for c in [x_column, *y_columns] if c not in df.columns]
        if missing:
            return {"status": "error", "message": f"Columns not in result: {missing}", "columns": list(df.columns)}
        fig, ax = plt.subplots(figsize=(9, 4.8))
        data = df.head(40)
        if chart_type == "line":
            for y in y_columns:
                ax.plot(data[x_column].astype(str), data[y], marker="o", label=y)
        else:
            width = 0.8 / max(len(y_columns), 1)
            xs = range(len(data))
            for i, y in enumerate(y_columns):
                ax.bar([x + i * width for x in xs], data[y], width=width, label=y)
            ax.set_xticks([x + width * (len(y_columns) - 1) / 2 for x in xs])
            ax.set_xticklabels(data[x_column].astype(str))
        ax.set_title(title)
        ax.legend()
        ax.grid(alpha=0.3)
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        fig.tight_layout()
        path = services.settings.charts_dir / f"{query_id}_{chart_type}.png"
        fig.savefig(path, dpi=120)
        plt.close(fig)
        turn = services.turns.get(trace_id)
        if turn:
            turn.span("event", "chart_created", path=str(path))
        return {"status": "ok", "path": str(path), "message": "Chart saved; mention the file path to the user."}

    return create_chart
