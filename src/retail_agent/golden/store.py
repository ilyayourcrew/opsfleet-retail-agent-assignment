"""Golden knowledge "Trios": Question -> SQL -> Analyst report.

On disk (prototype) / in GCS (prod) each trio is one YAML object:

    data/golden/trios/<id>.yaml        status: active      (served at query time)
    data/golden/candidates/<id>.yaml   status: candidate   (mined, waiting for the automated gate)
    status: deprecated                 (kept for audit, never served)
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class _ReadableDumper(yaml.SafeDumper):
    """Trio files are read and edited by analysts (curation, veto), so multi-line text such as SQL and
    reports is written as YAML block literals (`sql: |`), never as one escaped, quoted string."""


def _str_representer(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_ReadableDumper.add_representer(str, _str_representer)


class Trio(BaseModel):
    id: str
    question: str
    intent: str = ""
    tags: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    sql: str
    interpretation: list[str] = Field(default_factory=list, description="How analysts read the question")
    analyst_report: str = ""
    author: str = "analytics-team"
    source: Literal["analyst", "mined"] = "analyst"
    status: Literal["active", "candidate", "deprecated"] = "active"
    quality_score: float = 1.0
    version: int = 1
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).date().isoformat())
    last_validated_at: str | None = None
    usage_count: int = 0

    def search_text(self) -> str:
        return " ".join([self.question, self.intent.replace("_", " "), " ".join(self.tags), " ".join(self.interpretation)])

    def content_hash(self) -> str:
        return hashlib.sha256((self.question + self.sql).encode()).hexdigest()[:16]

    def normalized_sql(self) -> str:
        return re.sub(r"\s+", " ", self.sql.strip().lower())


class TrioStore:
    def __init__(self, root: Path):
        self.root = root
        self.active_dir = root / "trios"
        self.candidate_dir = root / "candidates"
        self._cache: tuple[float, list[Trio]] | None = None

    def _dir_mtime(self) -> float:
        files = list(self.active_dir.glob("*.yaml"))
        return max([f.stat().st_mtime for f in files] + [self.active_dir.stat().st_mtime]) if files else 0.0

    def active(self) -> list[Trio]:
        """Active trios, reloaded automatically when the bucket changes (no restart needed)."""
        if not self.active_dir.exists():
            return []
        m = self._dir_mtime()
        if self._cache and self._cache[0] == m:
            return self._cache[1]
        trios = [t for t in self._load_dir(self.active_dir) if t.status == "active"]
        self._cache = (m, trios)
        return trios

    def candidates(self) -> list[Trio]:
        return self._load_dir(self.candidate_dir) if self.candidate_dir.exists() else []

    @staticmethod
    def _load_dir(d: Path) -> list[Trio]:
        out = []
        for f in sorted(d.glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data:
                out.append(Trio(**data))
        return out

    def write(self, trio: Trio, candidate: bool = False) -> Path:
        d = self.candidate_dir if candidate else self.active_dir
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{trio.id}.yaml"
        path.write_text(yaml.dump(trio.model_dump(), Dumper=_ReadableDumper, sort_keys=False, allow_unicode=True,
                                  width=110), encoding="utf-8")
        self._cache = None
        return path

    def remove_candidate(self, trio_id: str) -> None:
        p = self.candidate_dir / f"{trio_id}.yaml"
        if p.exists():
            p.unlink()
