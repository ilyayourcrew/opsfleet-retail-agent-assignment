"""Central configuration.

Everything that differs between environments (offline laptop, CI, prod) is a setting here, read from
environment variables / `.env` with the `RA_` prefix. Business policy that non-developers may edit
(PII policy, entitlements, personas) lives in YAML under `config/` and is re-read at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RA_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---------------------------------------------------------------------------
    llm_provider: Literal["gemini", "ollama", "fake"] = "gemini"
    google_api_key: str | None = Field(default=None, validation_alias="GOOGLE_API_KEY")
    model_primary: str = "gemini-3.8-flash"
    # comma-separated fallback chain, tried in order on 429/5xx (different models = separate capacity)
    model_fallback: str = "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    # cheap model for side tasks: input-scope classifier, promotion / eval judge
    model_light: str = "gemini-3.5-flash-lite"
    ollama_model: str = "ollama_chat/qwen2.5:7b"
    embedding_model: str = "gemini-embedding-001"
    llm_timeout_s: float = 60.0
    # time-to-first-token deadline for every model except the last in the chain (no token -> fall back);
    # once a model is streaming, llm_timeout_s caps the whole answer
    llm_attempt_timeout_s: float = 20.0
    llm_retry_attempts: int = 3
    # USD per 1M tokens (input, output) used by the cost meter. Defaults match Gemini Flash list
    # prices at the time of writing; override per deployment.
    price_in_per_mtok: float = 0.30
    price_out_per_mtok: float = 2.50

    # --- Warehouse ---------------------------------------------------------------------
    warehouse: Literal["bigquery", "duckdb"] = "bigquery"
    gcp_project: str | None = None
    bq_dataset: str = "bigquery-public-data.thelook_ecommerce"
    bq_location: str = "US"
    bq_max_bytes_billed: int = 2_000_000_000
    bq_timeout_s: float = 45.0
    bq_price_per_tib: float = 6.25
    duckdb_path: Path = PROJECT_ROOT / "var" / "thelook_offline.duckdb"

    # --- Safety / policy -----------------------------------------------------------------
    k_anonymity_min: int = 5
    max_result_rows: int = 200
    rows_shown_to_llm: int = 50
    pseudonym_secret: str = ""  # empty -> a random per-installation key in state_dir (see resolved_pseudonym_secret)

    # --- Agent behaviour / budgets -------------------------------------------------------
    max_sql_attempts: int = 3
    max_tool_calls_per_turn: int = 12
    budget_usd_per_question: float = 1.0
    golden_top_k: int = 3
    max_llm_calls_per_turn: int = 15
    turn_timeout_s: float = 150.0

    # --- Storage -------------------------------------------------------------------------
    config_dir: Path = PROJECT_ROOT / "config"
    golden_dir: Path = PROJECT_ROOT / "data" / "golden"
    state_dir: Path = PROJECT_ROOT / "var"

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.model_fallback.split(",") if m.strip()]

    @property
    def app_db_path(self) -> Path:
        """SQLite DB for reports, preferences, pending actions, traces, feedback."""
        return self.state_dir / "app.sqlite"

    @property
    def session_db_path(self) -> Path:
        """SQLite DB used by ADK's SqliteSessionService (conversation history)."""
        return self.state_dir / "sessions.sqlite"

    @property
    def charts_dir(self) -> Path:
        return self.state_dir / "charts"

    @property
    def trace_log_path(self) -> Path:
        return self.state_dir / "traces.jsonl"

    def resolved_pseudonym_secret(self) -> str:
        """HMAC key for customer pseudonyms. A key that ships in the repo would make every pseudonym
        reversible (ids are small integers: hash them all), so an unset or placeholder value is replaced
        by a random key generated once per installation and kept in the git-ignored state dir.
        Production: Secret Manager."""
        if self.pseudonym_secret and self.pseudonym_secret not in _PLACEHOLDER_SECRETS:
            return self.pseudonym_secret
        key_file = self.state_dir / "pseudonym.key"
        if not key_file.exists():
            import secrets

            self.state_dir.mkdir(parents=True, exist_ok=True)
            key_file.write_text(secrets.token_hex(32), encoding="utf-8")
        return key_file.read_text(encoding="utf-8").strip()

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.charts_dir):
            d.mkdir(parents=True, exist_ok=True)


_PLACEHOLDER_SECRETS = {"change-me", "changeme", "secret"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reset_settings_cache() -> None:
    get_settings.cache_clear()
