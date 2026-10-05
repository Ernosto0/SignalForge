"""Evidence strength, 0–10 (plan §8.1): deterministic, from stored evidence only.

Components, each 0–1, weighted by ``strength.weights`` in config/defaults.yaml:

- ``sources``: independent sources, log-scaled, saturating at ``source_saturation``
- ``diversity``: distinct source categories (unlisted domains count as one category)
- ``quality``: mean over independent sources of the best tier weight (snippet-only × factor)
- ``recency``: mean over independent sources of 1 if dated within ``recency_months``, the
  ``unknown_date`` credit if undated, else 0
- ``directness``: share of first-hand signals
"""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from signalforge.config import StrengthDefaults

UNLISTED = "unlisted"


@dataclass(frozen=True)
class SourceDoc:
    id: int
    source_category: str | None
    quality_tier: str | None
    snippet_only: bool
    published_at: datetime | None


@dataclass(frozen=True)
class SignalRef:
    document_id: int
    first_hand: bool


@dataclass(frozen=True)
class Strength:
    score: float  # 0–10
    independent_sources: int
    components: dict[str, float]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def evidence_strength(
    signals: Sequence[SignalRef],
    docs: Mapping[int, SourceDoc],
    units: Mapping[int, int],
    cfg: StrengthDefaults,
    now: datetime,
) -> Strength:
    """Strength of the evidence behind one set of signals (a problem cluster)."""
    if not signals:
        return Strength(0.0, 0, {name: 0.0 for name in cfg.weights})
    by_unit: dict[int, list[SourceDoc]] = defaultdict(list)
    for doc_id in sorted({s.document_id for s in signals}):
        by_unit[units.get(doc_id, doc_id)].append(docs[doc_id])
    n = len(by_unit)
    cluster_docs = [d for group in by_unit.values() for d in group]
    cutoff = now - timedelta(days=round(cfg.recency_months * 30.44))

    def quality(doc: SourceDoc) -> float:
        weight = cfg.tier_weights.get(doc.quality_tier or "", 0.0)
        return weight * (cfg.snippet_only_factor if doc.snippet_only else 1.0)

    def recency(doc: SourceDoc) -> float:
        if doc.published_at is None:
            return cfg.unknown_date
        return 1.0 if doc.published_at >= cutoff else 0.0

    components = {
        "sources": min(1.0, math.log1p(n) / math.log1p(cfg.source_saturation)),
        "diversity": min(
            1.0,
            len({d.source_category or UNLISTED for d in cluster_docs}) / cfg.diversity_saturation,
        ),
        "quality": sum(max(map(quality, g)) for g in by_unit.values()) / n,
        "recency": sum(max(map(recency, g)) for g in by_unit.values()) / n,
        "directness": sum(s.first_hand for s in signals) / len(signals),
    }
    score = 10 * sum(cfg.weights.get(name, 0.0) * value for name, value in components.items())
    return Strength(
        score=round(score, 2),
        independent_sources=n,
        components={name: round(value, 3) for name, value in components.items()},
    )
