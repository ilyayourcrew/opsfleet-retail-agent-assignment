"""Fault injection wrapper used to demonstrate and test resilience (`--chaos` / RA_CHAOS_RATE)."""

from __future__ import annotations

import random
from typing import Any

from retail_agent.warehouse.base import (
    DryRunResult,
    ErrorKind,
    QueryResult,
    Warehouse,
    WarehouseError,
)


class ChaosWarehouse:
    """Fails a fraction of calls with a transient error before delegating."""

    def __init__(self, inner: Warehouse, failure_rate: float, seed: int | None = None):
        self.inner = inner
        self.failure_rate = failure_rate
        self.name = f"chaos({inner.name})"
        self.dialect = inner.dialect
        self._rng = random.Random(seed)
        self.injected = 0

    def _maybe_fail(self) -> None:
        if self._rng.random() < self.failure_rate:
            self.injected += 1
            raise WarehouseError(ErrorKind.TRANSIENT, "Injected fault: warehouse 503 Service Unavailable")

    def physical_table(self, table: str) -> str:
        return self.inner.physical_table(table)

    def dry_run(self, sql: str) -> DryRunResult:
        self._maybe_fail()
        return self.inner.dry_run(sql)

    def execute(self, sql: str, max_rows: int) -> QueryResult:
        self._maybe_fail()
        return self.inner.execute(sql, max_rows)

    def health(self) -> dict[str, Any]:
        return {**self.inner.health(), "chaos_failure_rate": self.failure_rate, "injected": self.injected}
