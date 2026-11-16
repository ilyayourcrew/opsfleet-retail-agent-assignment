"""Command-line interface.

    retail-agent chat     [--user ceo] [--provider gemini|ollama|fake] [--warehouse bigquery|duckdb]
    retail-agent ask      "question"   (one-shot, same options)
    retail-agent trace    [TRACE_ID]   (deep-dive: full model/tool correspondence of a turn)
    retail-agent stats    [--hours 168]
    retail-agent persona  show | publish FILE | rollback | history
    retail-agent learn    [--revalidate]  (mine rated turns -> candidate trios -> automated gate)
    retail-agent users    (who can see what)
    retail-agent doctor   (check credentials / connectivity)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from retail_agent.config import Settings, get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Retail data-analysis agent (Google ADK + Gemini + BigQuery)")
persona_app = typer.Typer(help="Manage the agent persona (tone) without redeploying.")
app.add_typer(persona_app, name="persona")
console = Console()

HELP = """[bold]Commands[/bold]
  /help                 this help
  /reports              list your saved reports
  /prefs [reset]        show (or reset) your learned preferences
  /feedback + | - [why] rate the last answer (feeds the learning loop)
  /trace [id]           show what happened in the last (or given) turn
  /stats                agent metrics
  /persona              active persona
  /whoami               your identity and data access
  /new                  start a new conversation
  /quit                 exit"""


def _quiet_logs() -> None:
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.ERROR)
    for name in ("google_adk", "google.adk", "google_genai", "LiteLLM", "httpx", "litellm"):
        logging.getLogger(name).setLevel(logging.ERROR)


def _settings(provider: str | None, warehouse: str | None) -> Settings:
    base = get_settings()
    upd = {}
    if provider:
        upd["llm_provider"] = provider
    if warehouse:
        upd["warehouse"] = warehouse
    s = base.model_copy(update=upd) if upd else base
    s.ensure_dirs()
    return s


def _runtime(provider: str | None, warehouse: str | None, chaos: float = 0.0, llm_chaos: float = 0.0):
    from retail_agent.agent.engine import ChatEngine
    from retail_agent.agent.services import Services
    from retail_agent.llm.factory import build_model
    from retail_agent.warehouse.base import WarehouseError

    s = _settings(provider, warehouse)
    if s.llm_provider == "gemini" and not s.google_api_key:
        console.print("[red]GOOGLE_API_KEY is not set.[/red] Add it to .env (free key: https://aistudio.google.com/apikey) "
                      "or run offline with [bold]--provider fake --warehouse duckdb[/bold].")
        raise typer.Exit(2)
    try:
        services = Services.build(s, chaos_rate=chaos)
    except WarehouseError as e:
        console.print(f"[red]Warehouse not available:[/red] {e.message}\n{e.hint}")
        raise typer.Exit(2) from e
    return s, services, ChatEngine(services, build_model(s, chaos_rate=llm_chaos))


def _footer(r) -> str:
    bits = [f"trace {r.trace_id.removeprefix('e-')[:8]}", f"{r.latency_ms / 1000:.1f}s", f"${r.cost_usd:.4f}"]
    if r.sql_attempts:
        bits.append(f"{r.sql_attempts} SQL")
    if r.status != "ok":
        bits.append(f"status={r.status}")
    return " - ".join(bits)


def _render_answer(r) -> None:
    console.print(Markdown(r.answer))
    for c in r.charts:
        console.print(f"[green]Chart saved:[/green] {c}")
    console.print(f"[dim]{_footer(r)}[/dim]")


def _handle_deletion(engine, user: str, session_id: str, r) -> None:
    pend = r.pending_deletion
    if not pend or not pend.reports:
        return
    t = Table(title=f"[bold red]Permanent deletion requested[/bold red] - {pend.description}", show_lines=False)
    t.add_column("#", justify="right")
    t.add_column("Title")
    t.add_column("Created")
    for rep in pend.reports:
        b = rep.brief()
        t.add_row(str(b["id"]), b["title"], b["created_at"])
    console.print(t)
    console.print("[yellow]This cannot be undone.[/yellow]")
    typed = console.input(f"Type [bold]{pend.confirm_phrase}[/bold] to confirm, or press Enter to cancel: ")
    ok, msg = engine.confirm_deletion(user, session_id, pend.token, typed)
    console.print(f"[{'green' if ok else 'yellow'}]{msg}[/]")


async def _chat_loop(engine, services, user: str, session_id: str | None) -> None:
    sid = session_id or await engine.new_session(user)
    ent = services.policies.entitlement(user)
    persona, ver = services.personas.active()
    console.print(Panel.fit(
        f"[bold]Retail Insights[/bold] - hi {ent.display_name}! Ask me about sales, customers and products.\n"
        f"Data access: {ent.describe()}  |  persona: {persona.name}@{ver}  |  "
        f"model: {services.settings.llm_provider}  |  warehouse: {services.warehouse.name}\n"
        "Type /help for commands.", border_style="cyan"))
    last_trace = ""
    while True:
        try:
            text = (await asyncio.to_thread(console.input, "[bold cyan]you>[/bold cyan] ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            if cmd in ("quit", "exit", "q"):
                break
            if cmd == "new":
                sid = await engine.new_session(user)
                console.print("[dim]New conversation started.[/dim]")
            elif cmd == "help":
                console.print(HELP)
            elif cmd == "reports":
                _print_reports(services, user)
            elif cmd == "prefs":
                if arg.strip() == "reset":
                    services.prefs.reset(user)
                    console.print("Preferences cleared.")
                else:
                    console.print(services.prefs.render(user))
            elif cmd == "feedback":
                _feedback(services, user, last_trace, arg)
            elif cmd == "trace":
                _print_trace(services, arg.strip() or last_trace)
            elif cmd == "stats":
                _print_stats(services, 24 * 7)
            elif cmd == "persona":
                p, v = services.personas.active()
                console.print(f"{p.name}@{v}\n{p.render()}")
            elif cmd == "whoami":
                console.print(f"{user}: {ent.display_name} ({ent.role}) - {ent.describe()}")
            else:
                console.print("Unknown command - /help")
            continue
        with console.status("Thinking...") as status:
            r = await engine.ask(user, sid, text, on_status=lambda m: status.update(m))
        last_trace = r.trace_id
        _render_answer(r)
        _handle_deletion(engine, user, sid, r)
    console.print("[dim]Bye.[/dim]")


def _feedback(services, user: str, trace_id: str, arg: str) -> None:
    if not trace_id:
        console.print("Nothing to rate yet.")
        return
    sign, _, comment = arg.strip().partition(" ")
    rating = 1 if sign in ("+", "up", "good", "1") else -1 if sign in ("-", "down", "bad", "-1") else 0
    if rating == 0:
        console.print("Usage: /feedback + | - [comment]")
        return
    services.traces.add_feedback(trace_id, user, rating, comment)
    learned = services.prefs.learn_from_message(user, comment) if comment else []
    console.print("Thanks - feedback recorded." + (f" Noted: {learned}" if learned else ""))


def _print_reports(services, user: str) -> None:
    reps = services.reports.list(user)
    if not reps:
        console.print("You have no saved reports.")
        return
    t = Table("#", "Title", "Created")
    for r in reps:
        b = r.brief()
        t.add_row(str(b["id"]), b["title"], b["created_at"])
    console.print(t)


def _print_trace(services, trace_id: str) -> None:
    if not trace_id:
        trace_id = services.traces.last_trace_id() or ""
    turn = services.traces.turn(trace_id) if trace_id else None
    if not turn:
        console.print("No trace found.")
        return
    tid = turn["trace_id"]
    head = (f"[bold]Trace {tid}[/bold]  user={turn['user_id']}  session={turn['session_id'][:8]}  status={turn['status']}"
            f"  latency={turn['latency_ms'] / 1000:.2f}s  cost=${turn['llm_cost_usd'] + turn['bq_cost_usd']:.5f}\n"
            f"model_calls={turn['model_calls']} tool_calls={turn['tool_calls']} sql_attempts={turn['sql_attempts']} "
            f"sql_failures={turn['sql_failures']} empty={turn['sql_empty']} self_corrected={turn['sql_self_corrected']} "
            f"guard_blocks={turn['guard_blocks']} pii_redactions={turn['pii_redactions']} fallback={bool(turn['fallback_used'])}\n"
            f"persona={turn['persona']}  golden={json.loads(turn['golden_ids'] or '[]')}  provider={turn['provider']}\n"
            f"Q: {turn['question']}")
    if turn["error"]:
        head += f"\n[red]error: {turn['error']}[/red]"
    console.print(Panel(head, border_style="magenta"))
    t = Table("#", "kind", "name", "status", "ms", "details", show_lines=True)
    for i, s in enumerate(services.traces.spans(tid)):
        a = dict(s["attributes"])
        if s["name"] == "request":
            detail = "\n".join(f"{m['role']}: " + json.dumps(m["parts"], default=str)[:400] for m in a.get("delta", []))
        elif s["name"] == "response":
            detail = f"{a.get('model_version', '')} in={a.get('tokens_in')} out={a.get('tokens_out')}\n" + json.dumps(a.get("parts"), default=str)[:600]
        elif s["name"] == "run_analysis_sql":
            detail = f"attempt {a.get('attempt')}: {a.get('purpose', '')}\nSQL: {a.get('sql', '')[:500]}"
            if a.get("error"):
                detail += f"\n[red]{a.get('error_type')}: {a.get('error')}[/red]"
            if "rows" in a:
                detail += f"\nrows={a['rows']} bytes={a.get('bytes', 0)} cache={a.get('cache_hit', False)}"
        else:
            detail = json.dumps(a, default=str)[:500]
        t.add_row(str(i), s["kind"], s["name"], s["status"], f"{s['duration_ms']:.0f}", detail)
    console.print(t)
    console.print(Panel(Markdown(turn["answer_preview"] or ""), title="answer (preview)", border_style="dim"))


def _print_stats(services, hours: float) -> None:
    from retail_agent.observability.metrics import compute_metrics

    m = compute_metrics(services.traces, hours)
    t = Table("metric", "value", title=f"Agent metrics - last {hours:g}h")
    for k, v in m.items():
        t.add_row(k, json.dumps(v) if isinstance(v, dict) else str(v))
    console.print(t)


# =============================================================================== commands
@app.command()
def chat(
    user: str = typer.Option("ceo", help="User id (see `retail-agent users`)."),
    provider: str = typer.Option(None, help="gemini | ollama | fake (default from .env)"),
    warehouse: str = typer.Option(None, help="bigquery | duckdb (default from .env)"),
    session: str = typer.Option(None, help="Resume an existing session id."),
    chaos: float = typer.Option(0.0, help="Fault injection: share of warehouse calls that fail (demo resilience)."),
    llm_chaos: float = typer.Option(0.0, help="Fault injection: share of primary-model calls that fail."),
):
    """Interactive chat with the analyst agent."""
    _quiet_logs()
    _, services, engine = _runtime(provider, warehouse, chaos, llm_chaos)
    from retail_agent.safety.policy import UnknownUserError

    try:
        services.policies.entitlement(user)
    except UnknownUserError:
        console.print(f"[red]Unknown user '{user}'.[/red] Known users: {', '.join(services.policies.users())}")
        raise typer.Exit(2) from None
    asyncio.run(_chat_loop(engine, services, user, session))


@app.command()
def ask(
    question: str,
    user: str = typer.Option("ceo"),
    provider: str = typer.Option(None),
    warehouse: str = typer.Option(None),
    show_trace: bool = typer.Option(False, "--trace", help="Print the trace after the answer."),
):
    """Ask one question (non-interactive)."""
    _quiet_logs()
    _, services, engine = _runtime(provider, warehouse)

    async def run():
        sid = await engine.new_session(user)
        with console.status("Thinking...") as status:
            r = await engine.ask(user, sid, question, on_status=lambda m: status.update(m))
        _render_answer(r)
        if show_trace:
            _print_trace(services, r.trace_id)

    asyncio.run(run())


@app.command()
def trace(trace_id: str = typer.Argument("", help="Trace id or prefix (default: last turn).")):
    """Deep-dive into one turn: model messages, tool calls, SQL, errors, cost."""
    from retail_agent.observability.trace_store import TraceStore

    s = get_settings()

    class _S:
        traces = TraceStore(s.app_db_path)

    _print_trace(_S, trace_id)


@app.command()
def stats(hours: float = typer.Option(168, help="Window in hours.")):
    """Agent-level metrics (success, latency, SQL retries, guard blocks, cost...)."""
    from retail_agent.observability.trace_store import TraceStore

    s = get_settings()

    class _S:
        traces = TraceStore(s.app_db_path)

    _print_stats(_S, hours)


@app.command()
def users():
    """List known users and their data entitlements."""
    from retail_agent.safety.policy import PolicyStore

    ps = PolicyStore(get_settings().config_dir)
    t = Table("user id", "name", "role", "data access")
    for uid in ps.users():
        e = ps.entitlement(uid)
        t.add_row(uid, e.display_name, e.role, e.describe())
    console.print(t)


@persona_app.command("show")
def persona_show():
    """Show the active persona."""
    from retail_agent.persona.registry import PersonaRegistry

    s = get_settings()
    p, v = PersonaRegistry(s.app_db_path, s.config_dir / "personas" / "executive_crisp.yaml").active()
    console.print(Panel(p.render(), title=f"active persona {v}"))


@persona_app.command("publish")
def persona_publish(path: Path, by: str = typer.Option("editor", help="Who is publishing (audit).")):
    """Validate a persona file with the automated quality gate and make it live (no redeploy)."""
    from retail_agent.persona.quality import build_gate
    from retail_agent.persona.registry import PersonaRegistry

    _quiet_logs()
    s = get_settings()
    reg = PersonaRegistry(s.app_db_path, s.config_dir / "personas" / "executive_crisp.yaml")
    rep = reg.publish(path, build_gate(s), published_by=by)
    for k, v in rep.checks.items():
        console.print(f"  {'[green]PASS' if v else '[red]FAIL'}[/] {k}")
    for p in rep.problems:
        console.print(f"  [red]-[/red] {p}")
    if rep.passed:
        _, v = reg.active()
        console.print(f"[green]Published and live as {v}.[/green] The next message of every user uses it.")
    else:
        console.print("[red]Rejected - the current persona stays live.[/red]")
        raise typer.Exit(1)


@persona_app.command("rollback")
def persona_rollback():
    """Re-activate the previous persona version."""
    from retail_agent.persona.registry import PersonaRegistry

    s = get_settings()
    v = PersonaRegistry(s.app_db_path, s.config_dir / "personas" / "executive_crisp.yaml").rollback()
    console.print(f"Active persona is now {v}." if v else "Nothing to roll back to.")


@persona_app.command("history")
def persona_history():
    """List published persona versions."""
    from retail_agent.persona.registry import PersonaRegistry

    s = get_settings()
    t = Table("version", "name", "by", "at", "active")
    for h in PersonaRegistry(s.app_db_path, s.config_dir / "personas" / "executive_crisp.yaml").history():
        t.add_row(h["version"], h["name"], h["by"], h["at"], "*" if h["active"] else "")
    console.print(t)


@app.command()
def learn(revalidate: bool = typer.Option(False, help="Also re-validate active trios (nightly job).")):
    """System-level learning: mine positively rated turns into candidate trios and run the automated gate."""
    from retail_agent.agent.services import Services
    from retail_agent.golden.promotion import PromotionGate, make_llm_judge, mine_candidates

    _quiet_logs()
    s = get_settings()
    services = Services.build(s)
    mined = mine_candidates(services.traces, services.trios)
    console.print(f"Mined {len(mined)} new candidate trio(s) from rated conversations.")
    judge = None
    if s.llm_provider == "gemini" and services.input_guard.classifier:
        judge = make_llm_judge(services.input_guard.classifier)
    gate = PromotionGate(services.trios, services.guard(), services.warehouse, judge=judge)
    for r in gate.promote_candidates():
        console.print(f"  {'[green]PROMOTED' if r.passed else '[yellow]KEPT AS CANDIDATE'}[/] {r.trio_id} {r.reasons or ''}")
    if revalidate:
        for r in gate.revalidate_active():
            ok = r.checks.get("safety") and r.checks.get("executes")
            console.print(f"  {'[green]valid' if ok else '[red]DEPRECATED'}[/] {r.trio_id} {r.reasons if not ok else ''}")


@app.command()
def doctor(provider: str = typer.Option(None), warehouse: str = typer.Option(None)):
    """Check configuration, credentials and connectivity."""
    _quiet_logs()
    s = _settings(provider, warehouse)
    console.print(f"provider={s.llm_provider} model={s.model_primary} fallback={s.model_fallback} warehouse={s.warehouse}")
    if s.warehouse == "duckdb":
        console.print(("[green]OK[/green] " if s.duckdb_path.exists() else "[red]MISSING[/red] ") + f"offline DB {s.duckdb_path}")
    else:
        try:
            from retail_agent.warehouse.factory import build_warehouse

            w = build_warehouse(s)
            t0 = time.time()
            d = w.dry_run(f"SELECT COUNT(*) FROM `{s.bq_dataset}.orders`")
            console.print(f"[green]OK[/green] BigQuery dry-run ({d.bytes_processed} bytes, {time.time() - t0:.1f}s), project={w.inner.runner.client.project}")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]FAIL[/red] BigQuery: {getattr(e, 'message', e)} {getattr(e, 'hint', '')}")
    if s.llm_provider == "gemini":
        if not s.google_api_key:
            console.print("[red]FAIL[/red] GOOGLE_API_KEY missing")
        else:
            try:
                from google import genai

                from retail_agent.llm.factory import _export_key

                _export_key(s)
                c = genai.Client()
                for m in (s.model_primary, s.model_fallback):
                    t0 = time.time()
                    r = c.models.generate_content(model=m, contents="Reply with the single word: ok")
                    console.print(f"[green]OK[/green] {m}: {r.text.strip()[:20]!r} ({time.time() - t0:.1f}s)")
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]FAIL[/red] Gemini: {e}")


if __name__ == "__main__":
    app()
