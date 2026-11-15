"""Builds the configured warehouse stack: Resilient(Chaos?(Backend))."""

from __future__ import annotations

from retail_agent.config import Settings
from retail_agent.warehouse.base import Warehouse
from retail_agent.warehouse.chaos import ChaosWarehouse
from retail_agent.warehouse.resilient import ResilientWarehouse


def build_warehouse(settings: Settings, chaos_rate: float = 0.0) -> ResilientWarehouse:
    backend: Warehouse
    if settings.warehouse == "duckdb":
        from retail_agent.warehouse.duckdb_backend import DuckDBWarehouse

        backend = DuckDBWarehouse(settings.duckdb_path)
    else:
        from retail_agent.warehouse.bigquery_backend import BigQueryWarehouse

        backend = BigQueryWarehouse(
            project_id=settings.gcp_project,
            dataset=settings.bq_dataset,
            location=settings.bq_location,
            max_bytes_billed=settings.bq_max_bytes_billed,
            timeout_s=settings.bq_timeout_s,
        )
    if chaos_rate > 0:
        backend = ChaosWarehouse(backend, chaos_rate)
    return ResilientWarehouse(backend, retry_attempts=3)
