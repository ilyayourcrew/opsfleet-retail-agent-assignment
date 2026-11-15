"""Loads the data-access policy (config/policy.yaml) and entitlements (config/entitlements.yaml).

Both are re-read when the file changes (mtime check), so policy edits apply without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from retail_agent.warehouse.schema import TABLES


@dataclass(frozen=True)
class DataPolicy:
    k_min: int
    allowed_tables: tuple[str, ...]
    pii: dict[str, frozenset[str]]
    quasi: dict[str, frozenset[str]]
    person_id: dict[str, frozenset[str]]
    denied_functions: frozenset[str]

    @property
    def all_pii_columns(self) -> frozenset[str]:
        return frozenset().union(*self.pii.values()) if self.pii else frozenset()

    def safe_columns(self, table: str) -> list[str]:
        blocked = self.pii.get(table, frozenset())
        return [c for c in TABLES[table].column_names() if c not in blocked]

    def safe_schema(self) -> dict[str, dict[str, str]]:
        """Schema of the policy views (what the LLM's SQL is qualified against)."""
        out: dict[str, dict[str, str]] = {}
        for t in self.allowed_tables:
            types = {c.name: c.type for c in TABLES[t].columns}
            out[t] = {c: types[c] for c in self.safe_columns(t)}
        return out


@dataclass(frozen=True)
class Entitlement:
    user_id: str
    display_name: str
    role: str
    unrestricted: bool
    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)  # products column -> allowed values

    def describe(self) -> str:
        if self.unrestricted:
            return "all products"
        return "; ".join(f"{k} in ({', '.join(v)})" for k, v in self.filters.items())


class UnknownUserError(PermissionError):
    pass


class _Cached:
    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self._data: Any = None

    def load(self) -> Any:
        mtime = self.path.stat().st_mtime
        if self._mtime != mtime:
            self._data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            self._mtime = mtime
        return self._data


class PolicyStore:
    def __init__(self, config_dir: Path, k_override: int | None = None):
        self._policy = _Cached(config_dir / "policy.yaml")
        self._ent = _Cached(config_dir / "entitlements.yaml")
        self.k_override = k_override

    def policy(self) -> DataPolicy:
        raw = self._policy.load()
        tables = raw.get("tables", {})

        def cols(kind: str) -> dict[str, frozenset[str]]:
            return {t: frozenset((tables.get(t) or {}).get(kind, [])) for t in raw.get("allowed_tables", [])}

        return DataPolicy(
            k_min=int(self.k_override or raw.get("k_anonymity_min", 5)),
            allowed_tables=tuple(raw.get("allowed_tables", [])),
            pii=cols("pii"),
            quasi=cols("quasi"),
            person_id=cols("person_id"),
            denied_functions=frozenset(f.upper() for f in raw.get("denied_functions", [])),
        )

    def users(self) -> dict[str, Any]:
        return self._ent.load().get("users", {}) or {}

    def entitlement(self, user_id: str) -> Entitlement:
        u = self.users().get(user_id)
        if u is None:
            raise UnknownUserError(f"User '{user_id}' has no data entitlements.")
        prods = u.get("products")
        if prods == "*":
            return Entitlement(user_id, u.get("display_name", user_id), u.get("role", ""), True)
        filters = {k: tuple(str(x) for x in v) for k, v in (prods or {}).items() if v}
        allowed_cols = {"department", "category", "brand"}
        if not filters or set(filters) - allowed_cols:
            # fail closed on empty / malformed entitlements
            filters = {"department": ("__none__",)}
        return Entitlement(user_id, u.get("display_name", user_id), u.get("role", ""), False, filters)
