"""CLI rendering of untrusted text (report titles, SQL, user input) and the confirmation step.

Rich parses `[...]` as markup. Before the fix, a model-written title "Q1 [draft] review" was shown as
"Q1  review" in the deletion confirmation (not the exact list the user must approve), and a title
containing "[/bold]" crashed the CLI in the middle of the confirmation.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from retail_agent import cli
from retail_agent.agent.services import Services

TITLES = ["Q1 [draft] review", "Fix [/bold] closing tag", "[link=https://evil.example]Click[/link] me"]


@pytest.fixture
def recording(monkeypatch):
    con = Console(record=True, width=200, force_terminal=False)
    monkeypatch.setattr(cli, "console", con)
    return con


class _Engine:
    def __init__(self):
        self.confirmed = None

    def confirm_deletion(self, user, sid, token, typed):
        self.confirmed = typed
        return False, "Deletion cancelled - nothing was deleted."


class _Result:
    def __init__(self, pending):
        self.pending_deletion = pending


def test_deletion_confirmation_shows_exact_titles_and_never_crashes(settings, recording, monkeypatch):
    services = Services.build(settings)
    for t in TITLES:
        services.reports.save("ceo", "s1", t, "body")
    pend = services.reports.request_deletion("ceo", "s1", "this_conversation")
    monkeypatch.setattr(recording, "input", lambda prompt="": "")
    eng = _Engine()
    cli._handle_deletion(eng, "ceo", "s1", _Result(pend))
    out = recording.export_text()
    for t in TITLES:
        assert t in out  # shown verbatim, markup not interpreted
    assert eng.confirmed == "" and "nothing was deleted" in out


def test_report_list_and_trace_view_show_literal_text(settings, recording):
    services = Services.build(settings)
    services.reports.save("ceo", "s1", TITLES[1], "body")
    cli._print_reports(services, "ceo")
    assert TITLES[1] in recording.export_text()


# ---------------------------------------------------------------- fresh-machine behaviour of the commands
# Found by running every README command in a clean clone without .env (a reviewer's machine): `learn`
# crashed with a traceback and `persona publish` could not run offline.
from typer.testing import CliRunner  # noqa: E402

runner = CliRunner()


def _use(monkeypatch, s):
    monkeypatch.setattr(cli, "get_settings", lambda: s)


def test_admin_commands_without_api_key_explain_instead_of_crashing(settings, recording, monkeypatch):
    _use(monkeypatch, settings.model_copy(update={"llm_provider": "gemini", "google_api_key": None}))
    for args in (["learn"], ["persona", "publish", str(settings.config_dir / "personas" / "upbeat_coach.yaml")]):
        r = runner.invoke(cli.app, args)
        assert r.exit_code == 2, (args, r.output)
        assert r.exception is None or isinstance(r.exception, SystemExit)
        out = recording.export_text()  # note: export_text() clears the buffer
        assert "GOOGLE_API_KEY is not set" in out and "--provider fake" in out


def test_persona_publish_and_learn_run_offline(settings, recording, monkeypatch, tmp_path):
    import shutil

    golden = tmp_path / "golden"  # `learn --revalidate` rewrites trio files: never touch the repo's copies
    shutil.copytree(settings.golden_dir, golden)
    settings = settings.model_copy(update={"golden_dir": golden})
    _use(monkeypatch, settings)
    persona = str(settings.config_dir / "personas" / "upbeat_coach.yaml")
    r = runner.invoke(cli.app, ["persona", "publish", persona, "--provider", "fake", "--warehouse", "duckdb"])
    assert r.exit_code == 0, recording.export_text()
    assert "Published and live" in recording.export_text()
    r = runner.invoke(cli.app, ["learn", "--revalidate", "--provider", "fake", "--warehouse", "duckdb"])
    assert r.exit_code == 0, recording.export_text()
    assert "Mined 0 new candidate" in recording.export_text()
    rewritten = (golden / "trios" / "churn_rate_monthly.yaml").read_text(encoding="utf-8")
    # still readable for analysts after re-validation: block literal, no escaped "\n" sequences
    assert "sql: |" in rewritten and "\\n" not in rewritten


def test_unexpected_errors_end_in_a_message_not_a_traceback(recording, monkeypatch):
    def boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(cli, "app", boom)
    monkeypatch.delenv("RA_DEBUG", raising=False)
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1
    out = recording.export_text()
    assert "Unexpected error (RuntimeError)" in out and "simulated failure" in out and "Traceback" not in out
