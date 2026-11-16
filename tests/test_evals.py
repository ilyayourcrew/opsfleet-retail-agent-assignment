"""The eval suite is itself code that gates releases, so its inputs are tested too."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from retail_agent.agent.services import Services
from retail_agent.safety.policy import PolicyStore
from retail_agent.safety.sql_guard import SqlGuard

ROOT = Path(__file__).resolve().parents[1]
SPEC = yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text(encoding="utf-8"))
_mod_spec = importlib.util.spec_from_file_location("run_evals", ROOT / "evals" / "run_evals.py")
run_evals = importlib.util.module_from_spec(_mod_spec)
_mod_spec.loader.exec_module(run_evals)


@pytest.mark.parametrize("case", [c for c in SPEC["cases"] if c.get("reference_sql")], ids=lambda c: c["id"])
def test_heldout_reference_sql_passes_guard_and_runs(case, settings, warehouse):
    store = PolicyStore(settings.config_dir)
    guard = SqlGuard(store.policy(), warehouse.physical_table)
    df = warehouse.execute(guard.check(case["reference_sql"], store.entitlement("ceo")).sql, 50).df
    assert len(df) > 0 and run_evals.numbers_in(df), "reference must produce comparable numbers"


def test_case_ids_unique_and_thresholds_cover_every_scored_kind():
    ids = [c["id"] for c in SPEC["cases"]]
    assert len(ids) == len(set(ids))
    assert {"golden_execution_accuracy", "heldout_execution_accuracy", "redteam_pass_rate"} <= set(SPEC["thresholds"])


def test_coincidental_small_numbers_do_not_score():
    assert not run_evals._signal(3) and not run_evals._signal(12.0)
    assert run_evals._signal(12.5) and run_evals._signal(1234)


def test_no_golden_ablation_removes_trios(settings):
    s = Services.build(settings.model_copy(update={"golden_top_k": 0}))
    assert s.retriever.search("monthly revenue trend", k=s.settings.golden_top_k) == []
