"""DuckDB adapter for fully offline runs (tests, CI, demos without cloud access).

The agent always writes BigQuery SQL; this adapter transpiles it to DuckDB with sqlglot, so the
same prompts, guard and golden SQL work in both modes.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import sqlglot

from retail_agent.warehouse.base import DryRunResult, ErrorKind, QueryResult, WarehouseError


class DuckDBWarehouse:
    name = "duckdb"
    dialect = "duckdb"

    def __init__(self, db_path: Path, timeout_s: float = 30.0) -> None:
        import duckdb

        self._duckdb = duckdb
        if not Path(db_path).exists():
            raise WarehouseError(
                ErrorKind.UNAVAILABLE,
                f"Offline database not found at {db_path}.",
                "Build it with: python scripts/build_offline_db.py",
            )
        self.db_path = Path(db_path)
        self.timeout_s = timeout_s
        self._con = duckdb.connect(str(db_path), read_only=True)
        self._lock = threading.Lock()

    def physical_table(self, table: str) -> str:
        return table

    def to_duckdb(self, bigquery_sql: str) -> str:
        try:
            return sqlglot.transpile(bigquery_sql, read="bigquery", write="duckdb")[0]
        except sqlglot.errors.ParseError as e:
            raise WarehouseError(ErrorKind.SYNTAX, f"Syntax error: {e}") from e

    def _classify(self, e: Exception) -> WarehouseError:
        d = self._duckdb
        msg = str(e).split("\n")[0][:400]
        if isinstance(e, d.ParserException | d.BinderException | d.CatalogException | d.ConversionException):
            return WarehouseError(ErrorKind.SYNTAX, msg)
        if isinstance(e, d.InterruptException):
            return WarehouseError(ErrorKind.TIMEOUT, "Query took too long.", "Simplify the query.")
        if isinstance(e, d.InvalidInputException | d.OutOfRangeException | d.NotImplementedException):
            return WarehouseError(ErrorKind.SYNTAX, msg)
        return WarehouseError(ErrorKind.INTERNAL, msg)

    def dry_run(self, sql: str) -> DryRunResult:
        q = self.to_duckdb(sql)
        with self._lock:
            try:
                self._con.execute(f"EXPLAIN {q}")
            except Exception as e:  # noqa: BLE001
                raise self._classify(e) from e
        return DryRunResult(bytes_processed=0, referenced_tables=[])

    def execute(self, sql: str, max_rows: int) -> QueryResult:
        q = self.to_duckdb(sql)
        t0 = time.perf_counter()
        with self._lock:
            timer = threading.Timer(self.timeout_s, self._con.interrupt)
            timer.start()
            try:
                df = self._con.execute(q).fetch_df()
            except Exception as e:  # noqa: BLE001
                raise self._classify(e) from e
            finally:
                timer.cancel()
        return QueryResult(df=df.head(max_rows), duration_ms=(time.perf_counter() - t0) * 1000)

    def health(self) -> dict[str, Any]:
        return {"backend": "duckdb", "path": str(self.db_path)}
