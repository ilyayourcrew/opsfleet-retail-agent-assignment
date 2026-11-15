"""Warehouse port: the only way the application reaches analytical data.

Two adapters implement it: BigQuery (online, the real dataset) and DuckDB (offline synthetic copy).
Both receive SQL that has already passed the SQL guard, written in BigQuery dialect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import pandas as pd


class ErrorKind(str, Enum):
    """How the caller should react to a failure. Drives the self-correction loop."""

    SYNTAX = "syntax"  # invalid SQL / unknown column -> LLM can fix
    POLICY = "policy"  # blocked by the SQL guard -> LLM can rephrase
    COST = "cost"  # would scan too many bytes -> LLM should narrow the query
    TIMEOUT = "timeout"  # query too slow -> LLM should simplify
    TRANSIENT = "transient"  # 5xx / rate limit / network -> retried internally, not by the LLM
    UNAVAILABLE = "unavailable"  # dependency down (circuit open) -> degrade gracefully
    AUTH = "auth"  # credentials / permissions -> operator problem, stop
    INTERNAL = "internal"


@dataclass
class WarehouseError(Exception):
    kind: ErrorKind
    message: str
    hint: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.kind.value}] {self.message}"

    @property
    def correctable(self) -> bool:
        return self.kind in {ErrorKind.SYNTAX, ErrorKind.POLICY, ErrorKind.COST, ErrorKind.TIMEOUT}


@dataclass
class DryRunResult:
    bytes_processed: int
    referenced_tables: list[str] = field(default_factory=list)


@dataclass
class QueryResult:
    df: pd.DataFrame
    bytes_processed: int = 0
    bytes_billed: int = 0
    duration_ms: float = 0.0
    cache_hit: bool = False
    job_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Warehouse(Protocol):
    """Port implemented by BigQueryWarehouse and DuckDBWarehouse."""

    name: str
    dialect: str  # sqlglot dialect the backend executes

    def physical_table(self, table: str) -> str:
        """Fully-qualified physical name for one of the allowed logical tables."""

    def dry_run(self, sql: str) -> DryRunResult:
        """Validate + estimate cost without running. Raises WarehouseError."""

    def execute(self, sql: str, max_rows: int) -> QueryResult:
        """Run a read-only query. Raises WarehouseError."""

    def health(self) -> dict[str, Any]:
        """Cheap liveness information for `/status`."""
