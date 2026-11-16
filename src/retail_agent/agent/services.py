"""Dependency container: one place that wires every component for a process (CLI, tests, evals)."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

import pandas as pd

from retail_agent.config import Settings
from retail_agent.golden.retriever import GoldenRetriever
from retail_agent.golden.store import TrioStore
from retail_agent.memory.preferences import PreferenceStore
from retail_agent.observability.trace_store import TraceStore, TurnRegistry
from retail_agent.persona.registry import PersonaRegistry
from retail_agent.reports.store import ReportStore
from retail_agent.resilience import TTLCache
from retail_agent.safety.input_guard import InputGuard
from retail_agent.safety.pii import Pseudonymizer
from retail_agent.safety.policy import PolicyStore
from retail_agent.safety.sql_guard import SqlGuard
from retail_agent.warehouse.base import Warehouse


@dataclass
class Services:
    settings: Settings
    warehouse: Warehouse
    policies: PolicyStore
    pseudonymizer: Pseudonymizer
    trios: TrioStore
    retriever: GoldenRetriever
    reports: ReportStore
    prefs: PreferenceStore
    personas: PersonaRegistry
    traces: TraceStore
    turns: TurnRegistry
    input_guard: InputGuard
    result_cache: TTLCache = field(default_factory=lambda: TTLCache(max_items=256, ttl_s=600))
    session_results: dict[str, OrderedDict[str, pd.DataFrame]] = field(default_factory=dict)
    date_range_cache: dict[str, str] = field(default_factory=dict)

    def guard(self) -> SqlGuard:
        # built per call so edits to config/policy.yaml apply immediately
        return SqlGuard(self.policies.policy(), self.warehouse.physical_table, max_rows=self.settings.max_result_rows)

    def remember_result(self, session_id: str, query_id: str, df: pd.DataFrame) -> None:
        bucket = self.session_results.setdefault(session_id, OrderedDict())
        bucket[query_id] = df
        while len(bucket) > 10:
            bucket.popitem(last=False)

    @classmethod
    def build(cls, settings: Settings, warehouse: Warehouse | None = None, chaos_rate: float = 0.0) -> Services:
        from retail_agent.llm.factory import build_classifier
        from retail_agent.warehouse.factory import build_warehouse

        settings.ensure_dirs()
        embed = _build_embedder(settings)
        trios = TrioStore(settings.golden_dir)
        return cls(
            settings=settings,
            warehouse=warehouse or build_warehouse(settings, chaos_rate=chaos_rate),
            policies=PolicyStore(settings.config_dir, k_override=settings.k_anonymity_min),
            pseudonymizer=Pseudonymizer(settings.resolved_pseudonym_secret()),
            trios=trios,
            retriever=GoldenRetriever(trios, embed=embed, cache_path=settings.state_dir / "golden_embeddings.json"),
            reports=ReportStore(settings.app_db_path),
            prefs=PreferenceStore(settings.app_db_path),
            personas=PersonaRegistry(settings.app_db_path, settings.config_dir / "personas" / "executive_crisp.yaml"),
            traces=TraceStore(settings.app_db_path, settings.trace_log_path),
            turns=TurnRegistry(),
            input_guard=InputGuard(classifier=build_classifier(settings)),
        )


def _build_embedder(settings: Settings):
    if settings.llm_provider != "gemini":
        return None
    from google import genai
    from google.genai import types

    from retail_agent.llm.factory import _export_key
    from retail_agent.resilience import CircuitBreaker

    _export_key(settings)
    client = genai.Client()
    breaker = CircuitBreaker("embeddings", failure_threshold=2, reset_timeout_s=120)

    def embed(texts: list[str], task: str) -> list[list[float]]:
        def call():
            r = client.models.embed_content(
                model=settings.embedding_model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task, output_dimensionality=768,
                                                http_options=types.HttpOptions(timeout=10_000)),
            )
            return [e.values for e in r.embeddings]

        return breaker.call(call)

    return embed
