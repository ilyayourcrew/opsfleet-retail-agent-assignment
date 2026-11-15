"""Backend-agnostic resilience for the warehouse: circuit breaker + transient retries."""

from __future__ import annotations

from typing import Any

from retail_agent.resilience import CircuitBreaker, CircuitOpenError, retry_transient
from retail_agent.warehouse.base import (
    DryRunResult,
    ErrorKind,
    QueryResult,
    Warehouse,
    WarehouseError,
)

# Failures that say something about the health of the service (vs. a bad query).
_HEALTH_FAILURES = {ErrorKind.TRANSIENT, ErrorKind.TIMEOUT, ErrorKind.INTERNAL}


class ResilientWarehouse:
    def __init__(self, inner: Warehouse, retry_attempts: int = 3, breaker: CircuitBreaker | None = None):
        self.inner = inner
        self.name = inner.name
        self.dialect = inner.dialect
        self.breaker = breaker or CircuitBreaker(f"warehouse:{inner.name}", failure_threshold=4, reset_timeout_s=30)
        self.retry_attempts = retry_attempts
        self.retries = 0  # observable counter

    def _call(self, fn):
        attempt_no = {"n": 0}

        @retry_transient(
            lambda e: isinstance(e, WarehouseError) and e.kind == ErrorKind.TRANSIENT,
            attempts=self.retry_attempts,
        )
        def attempt():
            attempt_no["n"] += 1
            if attempt_no["n"] > 1:
                self.retries += 1
            try:
                self.breaker.before_call()
            except CircuitOpenError as e:
                raise WarehouseError(
                    ErrorKind.UNAVAILABLE, str(e), "The data warehouse is recovering; try again shortly."
                ) from e
            try:
                out = fn()
            except WarehouseError as e:
                if e.kind in _HEALTH_FAILURES:
                    self.breaker.record_failure()
                raise
            self.breaker.record_success()
            return out

        return attempt()

    def physical_table(self, table: str) -> str:
        return self.inner.physical_table(table)

    def dry_run(self, sql: str) -> DryRunResult:
        return self._call(lambda: self.inner.dry_run(sql))

    def execute(self, sql: str, max_rows: int) -> QueryResult:
        return self._call(lambda: self.inner.execute(sql, max_rows))

    def health(self) -> dict[str, Any]:
        return {**self.inner.health(), "circuit": self.breaker.state, "retries": self.retries}
