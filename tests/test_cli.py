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
