"""BigQuery adapter.

`BigQueryRunner` keeps the interface of the client provided with the assignment
(`execute_query`, `get_table_schema`) and extends it with what production use needs:
dry-run validation, a hard `maximum_bytes_billed` cap, timeouts, job labels and the query cache.
`BigQueryWarehouse` wraps it behind the `Warehouse` port and adds error classification,
retries for transient failures and a circuit breaker.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd

from retail_agent.warehouse.base import (
    DryRunResult,
    ErrorKind,
    QueryResult,
    WarehouseError,
)

log = logging.getLogger(__name__)


class BigQueryRunner:
    """A lean BigQuery client for executing SQL queries and returning DataFrame results.

    Interface-compatible with the `bq_client.py` shared by OpsFleet, extended with dry-run,
    byte caps and timeouts.
    """

    def __init__(
        self,
        project_id: str | None = None,
        dataset_id: str | None = "bigquery-public-data.thelook_ecommerce",
        location: str = "US",
        max_bytes_billed: int | None = None,
        timeout_s: float = 45.0,
    ) -> None:
        from google.cloud import bigquery  # imported lazily: offline mode never needs it

        self._bq = bigquery
        logging.info("Initializing BigQuery client")
        try:
            self.client = bigquery.Client(project=project_id, location=location)
        except Exception as e:  # noqa: BLE001 - surface as typed error below
            logging.error(f"Failed to initialize BigQuery client: {e}")
            raise
        self.dataset_id = dataset_id
        self.location = location
        self.max_bytes_billed = max_bytes_billed
        self.timeout_s = timeout_s
        logging.info(f"BigQuery client initialized for dataset: {self.dataset_id}")

    def dry_run(self, sql_query: str) -> Any:
        cfg = self._bq.QueryJobConfig(dry_run=True, use_query_cache=False)
        return self.client.query(sql_query, job_config=cfg, location=self.location)

    def run(self, sql_query: str, max_rows: int | None = None) -> tuple[pd.DataFrame, Any]:
        cfg = self._bq.QueryJobConfig(
            use_query_cache=True,
            maximum_bytes_billed=self.max_bytes_billed,
            # Server-side limit: BigQuery cancels the job itself. A client-side timeout alone only stops
            # *waiting* - the job would keep running (and billing) after we gave up on it.
            job_timeout_ms=int(self.timeout_s * 1000),
            labels={"app": "retail-agent"},
        )
        job = self.client.query(sql_query, job_config=cfg, location=self.location)
        try:
            rows = job.result(timeout=self.timeout_s, max_results=max_rows)
        except BaseException:
            _cancel_quietly(job)  # timeouts, errors, Ctrl+C: never leave an orphaned job running
            raise
        df = rows.to_dataframe(create_bqstorage_client=False)
        return df, job

    def execute_query(self, sql_query: str) -> pd.DataFrame:
        """Execute a SQL query and return results as a DataFrame (original interface)."""
        try:
            logging.info("Executing BigQuery query")
            df, _ = self.run(sql_query)
            logging.info(f"Query completed successfully, returned {len(df)} rows")
            return df
        except Exception as e:
            logging.error(f"BigQuery execution failed: {e}")
            raise

    def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """Get schema information for a specific table (original interface)."""
        table = self.client.get_table(f"{self.dataset_id}.{table_name}")
        return [
            {
                "name": f.name,
                "type": f.field_type,
                "mode": f.mode,
                "description": f.description or "",
            }
            for f in table.schema
        ]


def _cancel_quietly(job: Any) -> None:
    try:
        if not job.done():
            job.cancel()
            log.info("Cancelled BigQuery job %s", getattr(job, "job_id", "?"))
    except Exception:  # noqa: BLE001 - best effort; the server-side job timeout is the backstop
        log.warning("Could not cancel BigQuery job %s", getattr(job, "job_id", "?"))


def classify_bigquery_error(exc: BaseException) -> WarehouseError:
    """Map google-cloud errors onto ErrorKind so callers can decide: fix, retry or degrade."""
    from google.api_core import exceptions as gexc
    from google.auth import exceptions as auth_exc

    msg = str(getattr(exc, "message", None) or exc)
    low = msg.lower()
    if isinstance(exc, WarehouseError):
        return exc
    if isinstance(exc, auth_exc.DefaultCredentialsError | auth_exc.RefreshError):
        return WarehouseError(
            ErrorKind.AUTH,
            "BigQuery credentials are missing or expired.",
            "Run `gcloud auth application-default login` and set RA_GCP_PROJECT.",
        )
    if "bytesbilledlimitexceeded" in low or "bytes billed" in low:
        return WarehouseError(
            ErrorKind.COST,
            "Query would scan more data than the per-query cap allows.",
            "Filter on created_at to a narrower date range, select fewer columns, or aggregate earlier.",
        )
    if isinstance(exc, gexc.Forbidden):
        if "ratelimitexceeded" in low or "quotaexceeded" in low or "rate limit" in low:
            return WarehouseError(ErrorKind.TRANSIENT, "BigQuery rate limit reached.")
        return WarehouseError(ErrorKind.AUTH, f"BigQuery access denied: {msg[:300]}")
    if isinstance(exc, gexc.BadRequest | gexc.NotFound):
        return WarehouseError(ErrorKind.SYNTAX, _short_bq_message(msg))
    if isinstance(
        exc,
        gexc.TooManyRequests
        | gexc.InternalServerError
        | gexc.BadGateway
        | gexc.ServiceUnavailable
        | gexc.GatewayTimeout
        | ConnectionError,
    ):
        return WarehouseError(ErrorKind.TRANSIENT, f"BigQuery temporarily unavailable: {msg[:200]}")
    if isinstance(exc, TimeoutError) or "timeout" in low or "deadline" in low:
        return WarehouseError(
            ErrorKind.TIMEOUT,
            "Query took too long.",
            "Simplify: aggregate earlier, filter a smaller date range, avoid cross joins.",
        )
    return WarehouseError(ErrorKind.INTERNAL, msg[:300])


def _short_bq_message(msg: str) -> str:
    # BigQuery messages embed the job URL and location noise; keep the useful part.
    for marker in ("Syntax error", "Unrecognized name", "No matching signature", "Table", "Name"):
        idx = msg.find(marker)
        if idx >= 0:
            return msg[idx : idx + 400]
    return msg[:400]


class BigQueryWarehouse:
    """Port adapter: every google-cloud exception is translated into a typed WarehouseError.

    Retries and the circuit breaker live in `ResilientWarehouse`, which wraps any backend.
    """

    name = "bigquery"
    dialect = "bigquery"

    def __init__(
        self,
        project_id: str | None,
        dataset: str,
        location: str = "US",
        max_bytes_billed: int = 2_000_000_000,
        timeout_s: float = 45.0,
    ) -> None:
        self.dataset = dataset
        self.max_bytes_billed = max_bytes_billed
        try:
            self.runner = BigQueryRunner(
                project_id=project_id,
                dataset_id=dataset,
                location=location,
                max_bytes_billed=max_bytes_billed,
                timeout_s=timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            raise classify_bigquery_error(e) from e

    def physical_table(self, table: str) -> str:
        return f"{self.dataset}.{table}"

    def dry_run(self, sql: str) -> DryRunResult:
        try:
            job = self.runner.dry_run(sql)
        except Exception as e:  # noqa: BLE001
            raise classify_bigquery_error(e) from e
        tables = [f"{t.project}.{t.dataset_id}.{t.table_id}" for t in (job.referenced_tables or [])]
        return DryRunResult(bytes_processed=int(job.total_bytes_processed or 0), referenced_tables=tables)

    def execute(self, sql: str, max_rows: int) -> QueryResult:
        t0 = time.perf_counter()
        try:
            df, job = self.runner.run(sql, max_rows=max_rows)
        except Exception as e:  # noqa: BLE001
            raise classify_bigquery_error(e) from e
        return QueryResult(
            df=df,
            bytes_processed=int(job.total_bytes_processed or 0),
            bytes_billed=int(job.total_bytes_billed or 0),
            duration_ms=(time.perf_counter() - t0) * 1000,
            cache_hit=bool(job.cache_hit),
            job_id=job.job_id,
        )

    def health(self) -> dict[str, Any]:
        return {"backend": "bigquery", "dataset": self.dataset}
