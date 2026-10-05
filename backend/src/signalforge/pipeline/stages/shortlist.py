"""shortlist stage (plan §4 Gate 1, §7.5): problem clusters → shortlist, deterministic.

A cluster passes when its evidence strength and its independent source count both reach the
configured bars; the strongest ``max_shortlisted`` passing clusters are shortlisted. Every cluster
gets a ``gate_trace`` saying which rule decided, so the "insufficient evidence" list in the
landscape report is explainable.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from signalforge.config import ShortlistDefaults
from signalforge.db.models import ProblemCluster
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult
from signalforge.providers.cache import cache_key


@dataclass(frozen=True)
class GateInput:
    id: int
    strength: float
    independent_sources: int


@dataclass(frozen=True)
class GateDecision:
    shortlisted: bool
    trace: list[dict[str, Any]]


def gate1(clusters: list[GateInput], cfg: ShortlistDefaults) -> dict[int, GateDecision]:
    """Decision per cluster id. Ties in strength are broken by source count, then id."""
    traces: dict[int, list[dict[str, Any]]] = {}
    passing: list[GateInput] = []
    for c in clusters:
        trace = [
            {
                "rule": "min_strength",
                "value": c.strength,
                "threshold": cfg.min_strength,
                "passed": c.strength >= cfg.min_strength,
            },
            {
                "rule": "min_independent_sources",
                "value": c.independent_sources,
                "threshold": cfg.min_independent_sources,
                "passed": c.independent_sources >= cfg.min_independent_sources,
            },
        ]
        traces[c.id] = trace
        if all(t["passed"] for t in trace):
            passing.append(c)
    passing.sort(key=lambda c: (-c.strength, -c.independent_sources, c.id))
    shortlisted = {c.id for c in passing[: cfg.max_shortlisted]}
    for position, c in enumerate(passing[cfg.max_shortlisted :], cfg.max_shortlisted + 1):
        traces[c.id].append(
            {
                "rule": "max_shortlisted",
                "value": position,
                "threshold": cfg.max_shortlisted,
                "passed": False,
            }
        )
    return {i: GateDecision(i in shortlisted, t) for i, t in traces.items()}


def failed_rules(trace: list[dict[str, Any]]) -> list[str]:
    return [t["rule"] for t in trace if not t["passed"]]


class Shortlist:
    name = "shortlist"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.shortlist
        with ctx.db.begin() as session:
            clusters = session.scalars(
                select(ProblemCluster).where(ProblemCluster.run_id == ctx.run_id)
            ).all()
            if not clusters:
                raise ValueError(f"run {ctx.run_id} has no problem clusters; run cluster first")
            decisions = gate1(
                [
                    GateInput(c.id, c.evidence_strength or 0.0, c.independent_source_count)
                    for c in clusters
                ],
                cfg,
            )
            for c in clusters:
                c.shortlisted = decisions[c.id].shortlisted
                c.gate_trace = decisions[c.id].trace
                # A new Gate-1 decision makes the second round stale; verify re-runs after this.
                c.verification = None
            rows = [(c.id, c.evidence_strength, c.independent_source_count) for c in clusters]

        failed: dict[str, int] = {}
        for d in decisions.values():
            for rule in failed_rules(d.trace):
                failed[rule] = failed.get(rule, 0) + 1
        shortlisted = sum(d.shortlisted for d in decisions.values())
        metrics = {
            "clusters": len(clusters),
            "shortlisted": shortlisted,
            "insufficient_evidence": len(clusters) - shortlisted,
            "failed_by_rule": failed,
        }
        input_hash = cache_key({"clusters": sorted(rows), "config": cfg.model_dump(mode="json")})
        return StageResult(metrics=metrics, input_hash=input_hash)
