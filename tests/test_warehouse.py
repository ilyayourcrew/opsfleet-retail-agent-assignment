from __future__ import annotations

import pytest

from retail_agent.resilience import CircuitBreaker
from retail_agent.warehouse.base import ErrorKind, WarehouseError
from retail_agent.warehouse.chaos import ChaosWarehouse
from retail_agent.warehouse.resilient import ResilientWarehouse


def test_bigquery_sql_is_transpiled_and_executed(warehouse):
    sql = (
        "SELECT FORMAT_DATE('%Y-%m', DATE(created_at)) AS month, COUNT(*) AS n "
        "FROM order_items GROUP BY month ORDER BY month DESC LIMIT 3"
    )
    res = warehouse.execute(sql, max_rows=10)
    assert list(res.df.columns) == ["month", "n"]
    assert len(res.df) == 3


def test_syntax_errors_are_typed_and_correctable(warehouse):
    with pytest.raises(WarehouseError) as ei:
        warehouse.execute("SELECT no_such_col FROM orders", max_rows=5)
    assert ei.value.kind == ErrorKind.SYNTAX
    assert ei.value.correctable


def test_transient_failures_are_retried(warehouse):
    flaky = ResilientWarehouse(ChaosWarehouse(warehouse.inner, failure_rate=0.5, seed=3), retry_attempts=5)
    for _ in range(5):
        flaky.execute("SELECT COUNT(*) AS n FROM orders", max_rows=1)
    assert flaky.retries > 0  # some calls failed and were transparently retried


def test_circuit_opens_after_repeated_failures(warehouse):
    always_down = ChaosWarehouse(warehouse.inner, failure_rate=1.0)
    w = ResilientWarehouse(always_down, retry_attempts=1, breaker=CircuitBreaker("t", failure_threshold=2, reset_timeout_s=60))
    kinds = []
    for _ in range(4):
        with pytest.raises(WarehouseError) as ei:
            w.execute("SELECT 1", max_rows=1)
        kinds.append(ei.value.kind)
    assert kinds[:2] == [ErrorKind.TRANSIENT, ErrorKind.TRANSIENT]
    assert kinds[-1] == ErrorKind.UNAVAILABLE  # fail fast, no more calls to the dead dependency
    assert always_down.injected == 2
