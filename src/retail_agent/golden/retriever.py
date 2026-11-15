"""Hybrid retrieval of golden trios at query time.

* Lexical: BM25 over question + intent + tags + interpretation notes (no dependency, always on).
* Semantic: Gemini embeddings (cached per trio content hash) when available.
* Fusion: Reciprocal Rank Fusion (robust to the very different score scales).
A minimum lexical/semantic relevance is required, so unrelated trios are never injected.
In production the same logic runs as BigQuery `VECTOR_SEARCH` + a BM25/keyword index.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from retail_agent.golden.store import Trio, TrioStore

_STOP = set(
    ["a", "an", "the", "of", "for", "to", "in", "on", "and", "or", "by", "with", "what", "which", "how", "why", "is", "are", "was", "were", "do", "does", "did", "our", "we", "us", "me", "my", "i", "you", "show", "give", "tell", "please", "can", "could", "would", "about", "from", "than", "last", "this", "that", "these", "those", "it", "its", "be", "been", "at", "as", "per", "each", "all", "any"]
)


_SYNONYMS = {
    "monthly": "month", "yearly": "year", "annual": "year", "annually": "year", "quarterly": "quarter",
    "weekly": "week", "daily": "day", "sale": "revenue", "turnover": "revenue", "income": "revenue",
    "clients": "customer", "client": "customer", "buyer": "customer", "shopper": "customer", "user": "customer",
    "vs": "compare", "versus": "compare", "comparison": "compare",
}


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for t in toks:
        if t in _STOP or len(t) < 2:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(_SYNONYMS.get(t, t))
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.avgdl = sum(len(d) for d in docs) / max(len(docs), 1)
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for t in query:
                f = self.tf[i].get(t, 0)
                if not f:
                    continue
                s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avgdl))
            out.append(s)
        return out


@dataclass
class Hit:
    trio: Trio
    score: float  # fused RRF score
    lexical: float
    semantic: float | None


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=False))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


class GoldenRetriever:
    def __init__(
        self,
        store: TrioStore,
        embed: Callable[[list[str], str], list[list[float]]] | None = None,
        cache_path: Path | None = None,
        min_lexical: float = 1.0,
        min_semantic: float = 0.55,
    ):
        self.store = store
        self.embed = embed  # (texts, task_type) -> vectors
        self.cache_path = cache_path
        self.min_lexical = min_lexical
        self.min_semantic = min_semantic
        self._index_key: tuple[str, ...] | None = None
        self._bm25: BM25 | None = None
        self._vecs: dict[str, list[float]] = {}
        self.embedding_errors = 0

    def _ensure_index(self, trios: list[Trio]) -> None:
        key = tuple(t.content_hash() for t in trios)
        if key == self._index_key:
            return
        self._bm25 = BM25([tokenize(t.search_text()) for t in trios])
        self._index_key = key
        if self.embed:
            self._load_vectors(trios)

    def _load_vectors(self, trios: list[Trio]) -> None:
        cache: dict[str, list[float]] = {}
        if self.cache_path and self.cache_path.exists():
            cache = json.loads(self.cache_path.read_text())
        missing = [t for t in trios if t.content_hash() not in cache]
        if missing:
            try:
                vecs = self.embed([t.search_text() for t in missing], "RETRIEVAL_DOCUMENT")  # type: ignore[misc]
                for t, v in zip(missing, vecs, strict=True):
                    cache[t.content_hash()] = v
                if self.cache_path:
                    self.cache_path.write_text(json.dumps(cache))
            except Exception:  # noqa: BLE001 - embeddings are an optimisation, BM25 still works
                self.embedding_errors += 1
        self._vecs = {t.id: cache[t.content_hash()] for t in trios if t.content_hash() in cache}

    def search(self, question: str, k: int = 3) -> list[Hit]:
        trios = self.store.active()
        if not trios or not question.strip():
            return []
        self._ensure_index(trios)
        assert self._bm25 is not None
        lex = self._bm25.scores(tokenize(question))
        sem: list[float | None] = [None] * len(trios)
        if self.embed and self._vecs:
            try:
                qv = self.embed([question], "RETRIEVAL_QUERY")[0]
                sem = [(_cos(qv, self._vecs[t.id]) if t.id in self._vecs else None) for t in trios]
            except Exception:  # noqa: BLE001
                self.embedding_errors += 1
        rrf = [0.0] * len(trios)
        for scores in (lex, sem):
            if all(s is None for s in scores):
                continue
            order = sorted(range(len(trios)), key=lambda i: -(scores[i] or 0.0))
            for rank, i in enumerate(order):
                rrf[i] += 1.0 / (60 + rank + 1)
        hits = []
        for i in sorted(range(len(trios)), key=lambda i: -rrf[i]):
            relevant = lex[i] >= self.min_lexical or (sem[i] is not None and sem[i] >= self.min_semantic)
            if relevant:
                hits.append(Hit(trios[i], rrf[i], lex[i], sem[i]))
            if len(hits) >= k:
                break
        return hits


def format_for_prompt(hits: list[Hit]) -> str:
    """Render retrieved trios as few-shot 'how our analysts approached this' guidance."""
    if not hits:
        return ""
    parts = [
        "## Relevant analyst examples (golden knowledge)",
        "These are past questions answered by our human analysts. Reuse their metric definitions, filters "
        "and interpretation when they fit; adapt the SQL to the new question rather than copying blindly.",
    ]
    for n, h in enumerate(hits, 1):
        t = h.trio
        notes = "\n".join(f"  - {x}" for x in t.interpretation)
        parts.append(
            f"### Example {n} - {t.id} (intent: {t.intent})\nQuestion: {t.question}\n"
            f"Analyst interpretation:\n{notes}\nSQL:\n```sql\n{t.sql.strip()}\n```\n"
            f"Analyst report excerpt: {t.analyst_report.strip()[:600]}"
        )
    return "\n\n".join(parts)
