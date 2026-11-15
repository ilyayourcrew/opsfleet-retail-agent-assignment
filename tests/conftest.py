"""Shared fixtures. Every test runs fully offline against a small synthetic DuckDB warehouse."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from retail_agent.config import Settings
from retail_agent.warehouse.synthetic import generate, write_duckdb

FIXED_NOW = datetime(2026, 11, 16, 12, 0, 0)


@pytest.fixture(scope="session")
def offline_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("wh") / "thelook_test.duckdb"
    write_duckdb(generate(n_users=1500, n_products=300, seed=11, now=FIXED_NOW), path)
    return path


@pytest.fixture
def settings(tmp_path, offline_db) -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="fake",
        warehouse="duckdb",
        duckdb_path=offline_db,
        state_dir=tmp_path / "state",
        pseudonym_secret="test-secret",
        k_anonymity_min=5,
    )


@pytest.fixture
def warehouse(settings):
    from retail_agent.warehouse.factory import build_warehouse

    settings.ensure_dirs()
    return build_warehouse(settings)
