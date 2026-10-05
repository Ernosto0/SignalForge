"""Reading a problem cluster after ``verify`` (pipeline/stages/verify.py).

``shortlist`` (Gate 1) owns ``signal_ids``, ``evidence_strength``, ``independent_source_count`` and
``strength``; ``verify`` never overwrites them, so re-running either stage is idempotent. What the
second round added lives in ``ProblemCluster.verification``. Later stages read a cluster through
these helpers, which combine the two.
"""

from datetime import datetime
from typing import Any

from signalforge.db.models import ProblemCluster


def verification(cluster: ProblemCluster) -> dict[str, Any]:
    return cluster.verification or {}


def cluster_signal_ids(cluster: ProblemCluster, *, with_counter: bool = False) -> list[int]:
    """The cluster's signals plus those verify added; counter-evidence only if asked for."""
    v = verification(cluster)
    added = list(v.get("signal_ids_added", []))
    if with_counter:
        added += v.get("counter_signal_ids", [])
    return sorted({*cluster.signal_ids, *added})


def current_strength(cluster: ProblemCluster) -> float:
    """Evidence strength after verify if it ran, else the Gate-1 value."""
    after = verification(cluster).get("strength_after")
    if after is not None:
        return float(after["score"])
    return cluster.evidence_strength or 0.0


def current_sources(cluster: ProblemCluster) -> int:
    after = verification(cluster).get("strength_after")
    if after is not None:
        return int(after["independent_sources"])
    return cluster.independent_source_count


def _quote_rank(q: dict[str, Any], tiers: dict[str, float]) -> tuple[Any, ...]:
    """Best evidence first: first-hand, source quality, exact match, full text, newest."""
    return (
        not q["first_hand"],
        -tiers.get(q["tier"] or "", 0.0),
        q["verified"] != "exact",
        q["snippet_only"],
        -(datetime.fromisoformat(q["published"]).timestamp() if q["published"] else 0.0),
        q["signal_id"],
    )


def pick_quotes(
    quotes: list[dict[str, Any]], units: dict[int, int], tiers: dict[str, float], n: int
) -> list[dict[str, Any]]:
    """Up to ``n`` strongest quotes, one per independent source while sources last."""
    ranked = sorted(quotes, key=lambda q: _quote_rank(q, tiers))
    chosen: list[dict[str, Any]] = []
    seen_units: set[int] = set()
    for q in ranked:
        unit = units.get(q["document_id"], q["document_id"])
        if unit not in seen_units:
            seen_units.add(unit)
            chosen.append(q)
    for q in ranked:  # fewer sources than slots: fill with further quotes
        if len(chosen) >= n:
            break
        if q not in chosen:
            chosen.append(q)
    return chosen[:n]
